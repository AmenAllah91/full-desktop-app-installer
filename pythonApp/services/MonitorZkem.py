import json
from datetime import datetime
import pythoncom, win32com.client, ctypes, logging, time
from typing import Union
import pywintypes

from domain import AccessMachine
from services.MachineMonitor import make_rt_json
from services.websocket import send_pointage


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
        # self.kafka = KafkaService(getenv("KAFKA_BROKER"), f"group_rt_{tenant}")

    def OnAttTransactionEx(self, enroll, is_invalid,
                           state, verify,
                           Y, M, D, h, m, s, workcode):
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
            porte_type=self.m.porte_type or ""
        )
        print(payload)
        # self.kafka.produce("rt_" + self.tenant, payload)
        try:
            send_pointage(json.loads(payload), "1003")  # convertit string JSON → dict
        except Exception as ex:
            print(f"[WebSocket] Erreur envoi WS: {ex}")

# ------------------------------------------------------------------ #
# 2) Thread de surveillance temps-réel — version sans conflit
# ------------------------------------------------------------------ #
def monitor_zkem(machine: AccessMachine, ip: str, port: int,
                 machine_number: int = 1, stop_evt=None,
                 tenant: str = "empire", gym_branch_id: str = "0"):
    pythoncom.CoInitialize()
    try:
        # gencache.EnsureDispatch (et non Dispatch) est indispensable ici :
        # WithEvents ne peut pas lier OnAttTransactionEx sur un objet en
        # dispatch dynamique — la connexion et RegEvent réussissent quand
        # même, mais aucun événement temps réel n'est jamais reçu.
        try:
            base = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
        except Exception as ex:
            logging.error(
                "❌ gencache.EnsureDispatch a échoué pour zkemkeeper.ZKEM (%s) — "
                "les événements temps réel ne seront pas détectés. Essayez de "
                "vider le cache gencache (%%TEMP%%/gen_py) puis relancez.", ex
            )
            base = win32com.client.Dispatch("zkemkeeper.ZKEM")

        com_key = getattr(machine, "comKey", None) or 0
        if com_key:
            try:
                base.SetCommPassword(int(com_key))
            except Exception as ex:
                logging.warning("⚠️ SetCommPassword impossible pour %s : %s", ip, ex)

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
        win32com.client.WithEvents(base, EventCls)

        EVENT_MASK = 0xFFFF
        if not base.RegEvent(machine_number, EVENT_MASK):
            err = zkem_last_error(base)
            logging.error("RegEvent KO %s err=%s", ip, err)
            base.Disconnect()
            return

        logging.info("🟢 RTLog ZKEM connecté %s:%s", ip, port)

        while stop_evt is None or not stop_evt.is_set():
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)  # Réactivité meilleure
    finally:
        try:
            base.Disconnect()
        except Exception:
            pass
        pythoncom.CoUninitialize()
        logging.warning("🔌 RTLog ZKEM déconnecté %s", ip)