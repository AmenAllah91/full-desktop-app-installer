import json
import socket
from datetime import datetime
import pythoncom, win32com.client, ctypes, logging, time
from typing import Union
import pywintypes

from domain import AccessMachine
from kafka_service.kafkaservice import KafkaService
from services.MachineMonitor import make_rt_json, POINTAGE_TOPIC
from services.websocket import send_pointage
from services.machine_status import mark_connected, mark_disconnected, mark_event


# ------------------------------------------------------------------ #
# Helper : lecture compatible GetLastError (v1 ou v2)
# ------------------------------------------------------------------ #
def zkem_last_error(zk) -> Union[int, str]:
    """
    Lecture robuste du code d'erreur, toutes versions SDK.
    """
    try:                                    # firmware récent
        return int(zk.GetLastError())
    except (TypeError, pywintypes.com_error):
        err = ctypes.c_long()
        try:                                # firmware ancien
            zk.GetLastError(err)
            return err.value
        except Exception:
            return "?"


# ------------------------------------------------------------------ #
# 1) Classe réceptrice d’événements COM
# ------------------------------------------------------------------ #
class ZkemEvents:
    def __init__(self, m: AccessMachine, ip: str, tenant: str, gym_branch_id: str):
        self.ip = ip
        self.m = m
        self.tenant = tenant
        self.gym_branch_id = gym_branch_id
        # Ici tu peux créer le producer Kafka UNE SEULE FOIS pour l'instance
        from os import getenv
        self.kafka = KafkaService(getenv("KAFKA_BROKER"), f"group_rt_{tenant}")

    def OnAttTransactionEx(self, enroll, is_invalid,
                           state, verify,
                           Y, M, D, h, m, s, workcode):
        ts = datetime(Y, M, D, h, m, s).strftime("%Y-%m-%d %H:%M:%S")
        # Validité = IsInValid (0 = accepté), PAS AttState : AttState est le type
        # de pointage (0=check-in, 1=check-out, ...) — l'utiliser marquait les
        # sorties "non autorisé" alors que la machine avait accepté l'accès.
        payload = make_rt_json(
            machine_id = self.m.id,
            ip = self.ip,
            mtype = "STANDALONE_NEW_FIRMWARE",
            pin = int(enroll),
            state_code = int(is_invalid),
            dt = ts,
            door_id = 1,
            card_no = None,
            gym_branch_id = self.gym_branch_id,
            porte_type=self.m.porte_type or "",
            tenant=self.tenant
        )
        print(payload)
        mark_event(self.m)
        self.kafka.produce(POINTAGE_TOPIC, payload)
        try:
            # gym_branch_id réel (était codé en dur à "1003" : les pointages des
            # autres branches n'apparaissaient jamais dans la sidebar).
            send_pointage(json.loads(payload), self.gym_branch_id)
        except Exception as ex:
            print(f"[WebSocket] Erreur envoi WS: {ex}")


# ------------------------------------------------------------------ #
# 2) Thread de surveillance temps-réel — version sans conflit
# ------------------------------------------------------------------ #
def _tcp_alive(ip: str, port, timeout: float = 3.0) -> bool:
    """Sonde TCP légère : détecte une machine devenue injoignable, car le pump
    COM ne signale jamais la perte de connexion (les événements s'arrêtent
    silencieusement)."""
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def monitor_zkem(machine: AccessMachine, ip: str, port: int,
                 machine_number: int = 1, stop_evt=None,
                 tenant: str = "empire", gym_branch_id: str = "0"):
    """Boucle de surveillance temps réel avec reconnexion automatique.

    Avant : un échec de Connect_Net au démarrage tuait le thread définitivement
    (machine hors ligne au lancement = plus jamais surveillée).
    """
    RETRY_DELAY = 30  # s entre deux tentatives de connexion

    pythoncom.CoInitialize()
    try:
        while stop_evt is None or not stop_evt.is_set():
            base = None
            try:
                base = win32com.client.Dispatch("zkemkeeper.ZKEM")

                com_key = getattr(machine, "comKey", None) or 0
                if com_key:
                    try:
                        base.SetCommPassword(int(com_key))
                    except Exception as ex:
                        logging.warning("⚠️ SetCommPassword impossible pour %s : %s", ip, ex)

                if not base.Connect_Net(ip, port):
                    err = zkem_last_error(base)
                    logging.error("ZKEM connect KO %s:%s err=%s (retry dans %ss)", ip, port, err, RETRY_DELAY)
                    mark_disconnected(machine, f"Connect_Net KO err={err}", "error")
                    time.sleep(RETRY_DELAY)
                    continue

                def _build_event_class(m, ip_addr, tenant, gym_branch_id):
                    class _Events(ZkemEvents):
                        def __init__(self):
                            super().__init__(m, ip_addr, tenant, gym_branch_id)
                    return _Events

                EventCls = _build_event_class(machine, ip, tenant, gym_branch_id)
                win32com.client.WithEvents(base, EventCls)

                EVENT_MASK = 0xFFFF
                if not base.RegEvent(machine_number, EVENT_MASK):
                    err = zkem_last_error(base)
                    logging.error("RegEvent KO %s err=%s (retry dans %ss)", ip, err, RETRY_DELAY)
                    mark_disconnected(machine, f"RegEvent KO err={err}", "error")
                    base.Disconnect()
                    time.sleep(RETRY_DELAY)
                    continue

                logging.info("🟢 RTLog ZKEM connecté %s:%s", ip, port)
                mark_connected(machine, "connected")

                PROBE_INTERVAL = 30  # s
                last_probe = time.time()
                while stop_evt is None or not stop_evt.is_set():
                    pythoncom.PumpWaitingMessages()
                    time.sleep(0.05)  # Réactivité meilleure

                    if time.time() - last_probe > PROBE_INTERVAL:
                        last_probe = time.time()
                        if not _tcp_alive(ip, port):
                            raise RuntimeError("Machine injoignable (sonde TCP)")

            except Exception as ex:
                logging.error("RTLog ZKEM erreur %s : %s (retry dans %ss)", ip, ex, RETRY_DELAY)
                mark_disconnected(machine, str(ex), "error")
                time.sleep(RETRY_DELAY)
            finally:
                try:
                    if base is not None:
                        base.Disconnect()
                except Exception:
                    pass
    finally:
        pythoncom.CoUninitialize()
        mark_disconnected(machine, "Monitoring arrêté", "disconnected")
        logging.warning("🔌 RTLog ZKEM déconnecté %s", ip)
