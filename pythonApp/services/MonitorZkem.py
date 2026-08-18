import json
import socket
from datetime import datetime
import pythoncom, win32com.client, ctypes, logging, time
from typing import Union
import pywintypes

from domain import AccessMachine
from config import KAFKA_TOPIC
from kafka_service.kafkaservice import KafkaService
from services.MachineMonitor import make_rt_json
from services.websocket import send_pointage, send_machine_status
from services.common import zk_sdk_lock, tcp_reachable


# ------------------------------------------------------------------ #
# Helper : detection robuste d'un comKey valide
# ------------------------------------------------------------------ #
def has_comkey(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        value = value.strip()
        return value not in ("", "0")
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ #
# Helper : lecture compatible GetLastError (v1 ou v2)
# ------------------------------------------------------------------ #
def zkem_last_error(zk) -> Union[int, str]:
    """Délègue à l'implémentation unique de zkem_adapter (une seule à maintenir,
    et une seule à corriger le jour où l'on saura pourquoi le SDK refuse
    GetLastError)."""
    from services.zkem_adapter import zkem_last_error as _impl
    return _impl(zk)


# ------------------------------------------------------------------ #
# 1) Classe réceptrice d'événements COM
# ------------------------------------------------------------------ #
class ZkemEvents:
    def __init__(self, m: AccessMachine, ip: str, tenant: str, gym_branch_id: str):
        self.ip = ip
        self.m = m
        self.tenant = tenant
        self.gym_branch_id = gym_branch_id
        from os import getenv
        self.kafka = KafkaService(getenv("KAFKA_BROKER"), f"group_rt_{tenant}")
        self.last_event_time = time.time()

    def OnAttTransactionEx(self, enroll, is_invalid,
                           state, verify,
                           Y, M, D, h, m, s, workcode):
        self.last_event_time = time.time()
        ts = datetime(Y, M, D, h, m, s).strftime("%Y-%m-%d %H:%M:%S")
        payload = make_rt_json(
            machine_id = self.m.id,
            ip = self.ip,
            mtype = "STANDALONE_NEW_FIRMWARE",
            pin = int(enroll),
            state_code = int(state),
            dt = ts,
            door_id = 1,
            card_no = None,
            gym_branch_id = self.gym_branch_id,
            porte_type=self.m.porte_type or "",
            tenant=self.tenant,
        )
        logging.info(payload)
        self.kafka.produce(KAFKA_TOPIC, payload)
        try:
            send_pointage(json.loads(payload), self.gym_branch_id)
        except Exception as ex:
            logging.error("[WebSocket] Erreur envoi WS: %s", ex)

        try:
            from services.DeviceMAnager import DeviceManager
            ctx = DeviceManager.get(self.m.id)
            if ctx:
                ctx.adapter.event_count += 1
                ctx.adapter.last_seen = time.time()
        except Exception:
            pass


# ------------------------------------------------------------------ #
# 2) Thread de surveillance temps-réel — version robuste
# ------------------------------------------------------------------ #
def monitor_zkem(machine: AccessMachine, ip: str, port: int,
                 machine_number: int = 1, stop_evt=None,
                 tenant: str = "empire", gym_branch_id: str = "0"):
    pythoncom.CoInitialize()
    base = None
    try:
        # Machine éteinte : on sort tout de suite, sans jamais prendre le verrou
        # SDK. Le watchdog relancera ce thread avec un backoff croissant, et le
        # reste du parc continue de fonctionner normalement pendant ce temps.
        if not tcp_reachable(ip, port):
            logging.warning("⚠️ ZKEM %s:%s injoignable (TCP), thread non démarré", ip, port)
            return

        # Toute la mise en place touche le SDK : elle doit être sérialisée avec
        # les opérations venant des threads Flask, sous peine de violation
        # d'accès dans la DLL (voir zk_sdk_lock).
        with zk_sdk_lock:
            try:
                base = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
            except Exception as ex:
                logging.error(
                    "❌ gencache.EnsureDispatch a échoué pour zkemkeeper.ZKEM (%s) — "
                    "les événements temps réel ne seront pas détectés. Essayez de "
                    "vider le cache gencache (%%TEMP%%/gen_py) puis relancez.", ex
                )
                base = win32com.client.Dispatch("zkemkeeper.ZKEM")

            com_key = getattr(machine, "comKey", None)
            if has_comkey(com_key):
                try:
                    base.SetCommPassword(int(com_key))
                    logging.info("ComKey détecté et appliqué pour %s : %s", ip, com_key)
                except Exception as ex:
                    logging.warning("⚠️ SetCommPassword impossible pour %s : %s", ip, ex)
            else:
                logging.info("Aucun comKey défini pour %s, on ne l'applique pas.", ip)

            connected = base.Connect_Net(ip, port)
            if not connected:
                err = zkem_last_error(base)

        if not connected:
            logging.error("ZKEM connect KO %s:%s err=%s", ip, port, err)
            return

        def _build_event_class(m, ip_addr, tenant, gym_branch_id):
            class _Events(ZkemEvents):
                def __init__(self):
                    super().__init__(m, ip_addr, tenant, gym_branch_id)
            return _Events

        EventCls = _build_event_class(machine, ip, tenant, gym_branch_id)
        # WithEvents instancie lui-même la classe : c'est CETTE instance qui reçoit
        # OnAttTransactionEx. Un EventCls() créé à la main ne recevrait jamais aucun
        # événement (et doublerait le KafkaService). On garde la référence : elle
        # maintient le handler en vie et porte last_event_time.
        EVENT_MASK = 0xFFFF
        with zk_sdk_lock:
            events_instance = win32com.client.WithEvents(base, EventCls)
            registered = base.RegEvent(machine_number, EVENT_MASK)
            if not registered:
                err = zkem_last_error(base)
                base.Disconnect()

        if not registered:
            logging.error("RegEvent KO %s err=%s", ip, err)
            return

        logging.info("🟢 RTLog ZKEM connecté %s:%s", ip, port)
        try:
            from services.DeviceMAnager import DeviceManager
            ctx = DeviceManager.get(machine.id)
            if ctx:
                ctx.adapter.connected = True
                ctx.adapter.last_seen = time.time()
                ctx.adapter.online_since = time.time()
                ctx.adapter.offline_since = None
                ctx.adapter.last_error = ""
                send_machine_status(machine, ctx.adapter)
        except Exception:
            pass

        # Santé de la session : joignabilité TCP (10s) + renouvellement RegEvent (300s).
        # L'absence de pointage n'est PAS un critère de santé — une salle peut être
        # vide plusieurs heures sans que la machine ait le moindre problème.
        _zk_check_interval = 10
        _zk_last_check = time.time()
        _zk_reg_event_interval = 300
        _zk_last_reg_event = time.time()

        while stop_evt is None or not stop_evt.is_set():
            now = time.time()
            if now - _zk_last_check >= _zk_check_interval:
                _zk_last_check = now
                try:
                    sock = socket.create_connection((ip, port), timeout=2)
                    sock.close()
                except (OSError, socket.timeout):
                    logging.warning("⚠️ ZKEM %s:%s injoignable, arrêt du thread (watchdog relancera)", ip, port)
                    try:
                        from services.DeviceMAnager import DeviceManager
                        ctx = DeviceManager.get(machine.id)
                        if ctx:
                            ctx.adapter.connected = False
                            ctx.adapter.offline_since = time.time()
                            ctx.adapter.last_error = "Machine injoignable (TCP)"
                            send_machine_status(machine, ctx.adapter)
                    except Exception:
                        pass
                    break

            if now - _zk_last_reg_event >= _zk_reg_event_interval:
                _zk_last_reg_event = now
                try:
                    with zk_sdk_lock:
                        renewed = base.RegEvent(machine_number, EVENT_MASK)
                        err = None if renewed else zkem_last_error(base)
                    if not renewed:
                        logging.warning("⚠️ ZKEM %s renouvellement RegEvent échoué err=%s, arrêt", ip, err)
                        try:
                            from services.DeviceMAnager import DeviceManager
                            ctx = DeviceManager.get(machine.id)
                            if ctx:
                                ctx.adapter.connected = False
                                ctx.adapter.offline_since = time.time()
                                ctx.adapter.last_error = f"RegEvent renouvellement échoué: {err}"
                                send_machine_status(machine, ctx.adapter)
                        except Exception:
                            pass
                        break
                    logging.info("♻️ ZKEM %s RegEvent renouvelé", ip)
                except Exception as ex:
                    logging.error("💥 ZKEM %s exception pendant renouvellement RegEvent: %s", ip, ex)
                    break

            # La pompe déclenche OnAttTransactionEx, qui réentre dans le SDK :
            # elle doit donc être sérialisée elle aussi. Section très courte,
            # exécutée toutes les 50 ms — la contention reste négligeable.
            with zk_sdk_lock:
                pythoncom.PumpWaitingMessages()
            time.sleep(0.05)
    finally:
        if base is not None:
            with zk_sdk_lock:
                try:
                    base.RegEvent(machine_number, 0)
                except Exception:
                    pass
                try:
                    base.Disconnect()
                except Exception:
                    pass
        pythoncom.CoUninitialize()
        logging.warning("🔌 RTLog ZKEM déconnecté %s", ip)
