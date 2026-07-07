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
    try:
        return int(zk.GetLastError())
    except (TypeError, pywintypes.com_error):
        err = ctypes.c_long()
        try:
            zk.GetLastError(err)
            return err.value
        except Exception:
            return "?"


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
            send_pointage(json.loads(payload), "1003")
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

        if not base.Connect_Net(ip, port):
            err = zkem_last_error(base)
            logging.error("ZKEM connect KO %s:%s err=%s", ip, port, err)
            return

        def _build_event_class(m, ip_addr, tenant, gym_branch_id):
            class _Events(ZkemEvents):
                def __init__(self):
                    super().__init__(m, ip_addr, tenant, gym_branch_id)
            return _Events

        EventCls = _build_event_class(machine, ip, tenant, gym_branch_id)
        events_instance = EventCls()
        win32com.client.WithEvents(base, EventCls)

        EVENT_MASK = 0xFFFF
        if not base.RegEvent(machine_number, EVENT_MASK):
            err = zkem_last_error(base)
            logging.error("RegEvent KO %s err=%s", ip, err)
            base.Disconnect()
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

        _zk_check_interval = 10
        _zk_last_check = time.time()
        _zk_last_event_check = time.time()
        _zk_reg_event_interval = 300
        _zk_last_reg_event = time.time()
        _zk_event_timeout = 120

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

            if now - _zk_last_event_check >= _zk_event_timeout:
                _zk_last_event_check = now
                if hasattr(events_instance, 'last_event_time'):
                    elapsed_since_event = now - events_instance.last_event_time
                    if elapsed_since_event >= _zk_event_timeout:
                        logging.error(
                            "💀 ZKEM %s aucun événement reçu depuis %.0fs (timeout=%ss), "
                            "arrêt thread pour redémarrage watchdog",
                            ip, elapsed_since_event, _zk_event_timeout
                        )
                        try:
                            from services.DeviceMAnager import DeviceManager
                            ctx = DeviceManager.get(machine.id)
                            if ctx:
                                ctx.adapter.connected = False
                                ctx.adapter.offline_since = time.time()
                                ctx.adapter.last_error = f"Aucun événement depuis {elapsed_since_event:.0f}s"
                                send_machine_status(machine, ctx.adapter)
                        except Exception:
                            pass
                        break

            if now - _zk_last_reg_event >= _zk_reg_event_interval:
                _zk_last_reg_event = now
                try:
                    if not base.RegEvent(machine_number, EVENT_MASK):
                        err = zkem_last_error(base)
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

            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)
    finally:
        if base is not None:
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
