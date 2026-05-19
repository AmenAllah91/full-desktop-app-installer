import json
import time
from datetime import datetime
from typing import Union

import pythoncom
import pywintypes
import win32com.client
import ctypes
import logging

from domain import AccessMachine
from services.machinestatus.machineStatus import MachineStatus
from kafka_service.kafkaservice import KafkaService
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


def safe_disconnect(zk):
    try:
        if zk is not None:
            zk.Disconnect()
    except Exception:
        pass


class ZkemEvents:
    def __init__(self, m: AccessMachine, ip: str, tenant: str, gym_branch_id: str, status: MachineStatus):
        self.ip = ip
        self.m = m
        self.tenant = tenant
        self.gym_branch_id = gym_branch_id
        self.status = status

        from os import getenv
        self.kafka = KafkaService(getenv("KAFKA_BROKER"), f"group_rt_{tenant}")

    def OnAttTransactionEx(self, enroll, is_invalid, state, verify, Y, M, D, h, m, s, workcode):
        self.status.increment_event()

        ts = datetime(Y, M, D, h, m, s).strftime("%Y-%m-%d %H:%M:%S")
        payload = make_rt_json(
            machine_id=self.m.id,
            ip=self.ip,
            mtype="STANDALONE_NEW_FIRMWARE",
            pin=int(enroll),
            state_code=int(state),
            dt=ts,
            door_id=1,
            card_no=None,
            gym_branch_id=self.gym_branch_id,
            porte_type=self.m.porte_type or ""
        )

        print(payload)
        self.kafka.produce("rt_" + self.tenant, payload)

        try:
            send_pointage(json.loads(payload), str(self.gym_branch_id))
        except Exception as ex:
            print(f"[WebSocket] Erreur envoi WS: {ex}")


# ------------------------------------------------------------------ #
# 2) Thread de surveillance temps-réel — version best practice
# ------------------------------------------------------------------ #
def monitor_zkem(
    machine: AccessMachine,
    ip: str,
    port: int,
    machine_number: int = 1,
    stop_evt=None,
    tenant: str = "empire",
    gym_branch_id: str = "0"
):
    status = MachineStatus(machine, ip, port, gym_branch_id)

    pythoncom.CoInitialize()
    base = None
    event_sink = None
    reconnect_delay = 5.0
    heartbeat_interval = 3.0
    last_heartbeat = 0.0

    try:
        while stop_evt is None or not stop_evt.is_set():
            try:
                base = win32com.client.Dispatch("zkemkeeper.ZKEM")

                com_key = getattr(machine, "comKey", 0) or 0
                if com_key:
                    try:
                        base.SetCommPassword(int(com_key))
                        logging.info("🔑 ComKey appliqué pour monitor %s", ip)
                    except Exception as e:
                        logging.warning("⚠️ SetCommPassword failed for monitor %s: %s", ip, e)

                if not base.Connect_Net(ip, port):
                    err = zkem_last_error(base)
                    raise RuntimeError(f"Connect_Net failed err={err}")

                def _build_event_class(m, ip_addr, tenant_ref, gym_ref, status_ref):
                    class _Events(ZkemEvents):
                        def __init__(self):
                            super().__init__(m, ip_addr, tenant_ref, gym_ref, status_ref)
                    return _Events

                EventCls = _build_event_class(machine, ip, tenant, gym_branch_id, status)
                event_sink = win32com.client.WithEvents(base, EventCls)

                EVENT_MASK = 0xFFFF
                if not base.RegEvent(machine_number, EVENT_MASK):
                    err = zkem_last_error(base)
                    raise RuntimeError(f"RegEvent failed err={err}")

                status.mark_connected(reason="connected")
                logging.info("🟢 RTLog ZKEM connecté %s:%s", ip, port)

                while stop_evt is None or not stop_evt.is_set():
                    pythoncom.PumpWaitingMessages()

                    now = time.monotonic()
                    if now - last_heartbeat >= heartbeat_interval:
                        last_heartbeat = now
                        if not status.heartbeat_ok(timeout=1.5):
                            raise RuntimeError("heartbeat failed")

                    time.sleep(0.05)

                break

            except Exception as ex:
                status.mark_disconnected(error=str(ex), reason="connection_lost")
                logging.warning("🔌 RTLog ZKEM problème %s:%s -> %s", ip, port, ex)

                safe_disconnect(base)
                base = None
                event_sink = None

                if stop_evt is not None and stop_evt.is_set():
                    break

                # Interruptible sleep — wakes early on stop or reconnect request
                elapsed = 0.0
                while elapsed < reconnect_delay:
                    if stop_evt is not None and stop_evt.is_set():
                        break
                    if status.consume_reconnect_request():
                        logging.info("🔄 Reconnect requested for %s, retrying now", ip)
                        break
                    time.sleep(0.5)
                    elapsed += 0.5

    finally:
        safe_disconnect(base)
        status.mark_disconnected(reason="monitor_stopped")
        pythoncom.CoUninitialize()
        logging.warning("🔌 RTLog ZKEM déconnecté %s", ip)