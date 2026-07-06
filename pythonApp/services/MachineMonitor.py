# services/MachineMonitor.py
import ctypes
import json
import logging
import socket
import time
from ctypes import c_char_p, c_int, c_void_p, create_string_buffer
from datetime import datetime, timedelta
from os import getenv
from typing import Optional

from domain.DoorType import DoorType
from kafka_service.kafkaservice import KafkaService
from services.DeviceMAnager import DeviceContext
from services.addAndAuthorizeUser import connect_to_device
from services.websocket import send_pointage, send_machine_status_from_ctx
from services.common import throttle_event

PLCOMPRO_URL = getenv("PLCOMPRO_URL")
if not PLCOMPRO_URL:
    raise RuntimeError("PLCOMPRO_URL non défini dans l'environnement")

pl = ctypes.CDLL(PLCOMPRO_URL)
pl.Connect.argtypes = [c_char_p]
pl.Connect.restype = c_void_p
pl.Disconnect.argtypes = [c_void_p]
pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
pl.GetRTLog.restype = c_int

kafka = KafkaService(getenv("KAFKA_BROKER"), "rt_c3_group")

ACCESS_GRANTED = {0}


def is_c3_handle_connected(handle: Optional[c_void_p]) -> bool:
    if not handle:
        return False
    try:
        buf = create_string_buffer(64)
        ret = pl.GetRTLog(handle, buf, 64)
        return ret >= 0
    except Exception as e:
        logging.debug(f"Connection test failed: {e}")
        return False


def check_device_tcp(ip: str, port: str, timeout: float = 2.0) -> bool:
    try:
        sock = socket.create_connection((ip, int(port)), timeout=timeout)
        sock.close()
        return True
    except (OSError, socket.timeout):
        return False


def attempt_c3_reconnection(ctx: DeviceContext, max_retries: int = 3) -> bool:
    ip, port = ctx.machine.addresseip, str(ctx.machine.port)

    for attempt in range(max_retries):
        try:
            logging.info(f"🔄 Reconnexion C3 {attempt + 1}/{max_retries} pour {ip}")

            with ctx.lock:
                if ctx.handle:
                    try:
                        pl.Disconnect(ctx.handle)
                    except Exception:
                        pass
                ctx.set_handle(None)

            new_handle = connect_to_device(ip, port, com_key=getattr(ctx.machine, "comKey", None))
            if new_handle and is_c3_handle_connected(new_handle):
                with ctx.lock:
                    ctx.set_handle(new_handle)
                logging.info(f"✅ C3 {ip} reconnecté avec succès")
                return True
            if new_handle:
                try:
                    pl.Disconnect(new_handle)
                except Exception:
                    pass

            time.sleep(5)
        except Exception as e:
            logging.error(f"❌ Échec reconnexion C3 tentative {attempt + 1} pour {ip}: {e}")
            time.sleep(5)

    logging.error(f"❌ Impossible de reconnecter C3 {ip} après {max_retries} tentatives")
    return False


def is_event(line: str) -> bool:
    f = line.strip().split(",")
    if len(f) < 7:
        return False

    try:
        pin = int(f[1])
        card_no = f[2]
        event_type = int(f[4])
    except (ValueError, IndexError):
        return False

    if event_type == 255:
        return False

    if pin == 0 and card_no == "0" and event_type == 0:
        return False

    return True


def monitor_machine(ctx: DeviceContext, stop_evt):
    ip, port = ctx.machine.addresseip, str(ctx.machine.port)
    BUF_SZ = 4096
    buf = create_string_buffer(BUF_SZ)
    TENANT = ctx.tenant
    BRANCH_ID = str(ctx.gymBranchId)

    CONNECTION_CHECK_INTERVAL = 5
    last_check_time = datetime.now()
    consecutive_failures = 0
    max_consecutive_failures = 2
    last_successful_read = datetime.now()
    READ_TIMEOUT = 300

    EVENT_SILENT_TIMEOUT = 180
    last_event_time = time.time()

    def _clean_exit(connected: bool = False, error: str = ""):
        with ctx.lock:
            if ctx.handle:
                try:
                    pl.Disconnect(ctx.handle)
                except Exception:
                    pass
                ctx.set_handle(None)
        ctx.adapter.connected = connected
        ctx.adapter.offline_since = time.time()
        if error:
            ctx.adapter.last_error = error
        send_machine_status_from_ctx(ctx)

    while not stop_evt.is_set():
        now = datetime.now()
        current_ts = time.time()

        if current_ts - last_event_time >= EVENT_SILENT_TIMEOUT:
            logging.error(
                "💀 C3 %s aucun événement depuis %.0fs (timeout=%ss), "
                "arrêt thread pour redémarrage watchdog",
                ip, current_ts - last_event_time, EVENT_SILENT_TIMEOUT
            )
            _clean_exit(connected=False, error=f"Silence événements ({EVENT_SILENT_TIMEOUT}s)")
            return

        if now - last_check_time > timedelta(seconds=CONNECTION_CHECK_INTERVAL):
            tcp_ok = check_device_tcp(ip, port)

            if tcp_ok and ctx.handle and not is_c3_handle_connected(ctx.handle):
                consecutive_failures += 1
                ctx.adapter.connected = False
                logging.warning(
                    f"⚠️ C3 {ip} connexion test failed ({consecutive_failures}/{max_consecutive_failures})"
                )
                if consecutive_failures >= max_consecutive_failures:
                    logging.error(f"🚨 C3 {ip} connexion perdue, le thread va s'arrêter (watchdog relancera)")
                    _clean_exit(connected=False, error=f"Perte de connexion après {consecutive_failures} échecs")
                    return
            elif not tcp_ok:
                logging.warning(f"⚠️ C3 {ip} TCP injoignable (socket check)")
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    logging.error(f"🚨 C3 {ip} TCP injoignable pendant {consecutive_failures} cycles, arrêt")
                    _clean_exit(connected=False, error=f"TCP injoignable ({consecutive_failures} cycles)")
                    return
            else:
                consecutive_failures = 0
            last_check_time = now

        if ctx.handle and now - last_successful_read > timedelta(seconds=READ_TIMEOUT):
            logging.warning(f"⏰ C3 {ip} timeout de lecture ({READ_TIMEOUT}s), le thread va s'arrêter")
            _clean_exit(connected=False, error=f"Timeout de lecture après {READ_TIMEOUT}s")
            return

        if ctx.handle is None:
            h = connect_to_device(ip, port, com_key=getattr(ctx.machine, "comKey", None))
            if h:
                with ctx.lock:
                    ctx.set_handle(h)
                last_successful_read = datetime.now()
                consecutive_failures = 0
                logging.info(f"🔌 C3 {ip} connecté")
                ctx.adapter.connected = True
                ctx.adapter.last_seen = time.time()
                ctx.adapter.online_since = time.time()
                ctx.adapter.offline_since = None
                ctx.adapter.last_error = ""
                send_machine_status_from_ctx(ctx)
            else:
                time.sleep(1)
                continue

        if not check_device_tcp(ip, port):
            time.sleep(1)
            continue

        try:
            ret = pl.GetRTLog(ctx.handle, buf, BUF_SZ)
            if ret > 0:
                last_successful_read = datetime.now()
                consecutive_failures = 0
                raw = buf.value.decode(errors="ignore")
                if not is_event(raw):
                    continue

                last_event_time = time.time()
                pin, dt, state, door_id, card_no = parse_c3_line(raw)

                if door_id == 1:
                    porte = DoorType.ENTREE if ctx.machine.door1 == 1 else DoorType.SORTIE
                elif door_id == 2:
                    porte = DoorType.ENTREE if ctx.machine.door2 == 1 else DoorType.SORTIE
                elif door_id == 3:
                    porte = DoorType.ENTREE if ctx.machine.door3 == 1 else DoorType.SORTIE
                elif door_id == 4:
                    porte = DoorType.ENTREE if ctx.machine.door4 == 1 else DoorType.SORTIE
                else:
                    porte = DoorType.ENTREE

                payload = make_rt_json(
                    machine_id=ctx.machine.id,
                    ip=ip,
                    mtype="C3",
                    pin=pin,
                    state_code=state,
                    dt=dt.strftime("%Y-%m-%d %H:%M:%S"),
                    door_id=door_id,
                    card_no=card_no,
                    gym_branch_id=BRANCH_ID,
                    porte_type=porte.value,
                )

                kafka.produce("rt_" + TENANT, payload)
                try:
                    send_pointage(json.loads(payload), BRANCH_ID)
                except Exception as ex:
                    logging.error("[WebSocket] Erreur envoi WS: %s", ex)

                ctx.adapter.last_seen = time.time()
                ctx.adapter.event_count += 1
                logging.info("📡 %s → %s", ip, payload)

            elif ret == 0:
                last_successful_read = datetime.now()
                time.sleep(2.0 if throttle_event.is_set() else 0.2)
            else:
                logging.warning(f"GetRTLog returned error {ret} for {ip}")
                _clean_exit(connected=False, error=f"GetRTLog error: {ret}")
                return

        except Exception as exc:
            logging.error("RT %s: %s", ctx.machine.alias, exc, exc_info=True)
            _clean_exit(connected=False, error=str(exc))
            return

    _clean_exit(connected=False)
    logging.warning("🔌 RT C3 arrêté %s", ip)


def make_rt_json(
    *,
    machine_id: int,
    ip: str,
    mtype: str,
    pin: int,
    state_code: int,
    dt: str,
    door_id: int,
    card_no: Optional[str],
    gym_branch_id: str,
    porte_type: str,
) -> str:
    payload = {
        "id_machine": machine_id,
        "ip_adress": ip,
        "machine_type": mtype,
        "is_access_valid": state_code in ACCESS_GRANTED,
        "pin": pin,
        "access_date_time": dt,
        "door_id": door_id,
        "gym_branch_id": gym_branch_id,
        "cardNo": card_no or "",
        "porte_type": porte_type,
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_c3_line(raw: str):
    f = raw.strip().split(",")
    if len(f) < 7:
        raise ValueError(f"Trame incomplète: {raw}")
    if f[4] == "255":
        raise ValueError(f"Record de statut porte/alarme, pas un événement temps réel: {raw}")

    try:
        f_time = f[0]
        dt = datetime.strptime(f_time, "%Y-%m-%d %H:%M:%S")
        pin = int(f[1])
        card_no = f[2] if f[2] != "0" else None
        door_id = int(f[3])
        event_type = int(f[4])
        entry_exit_status = int(f[5])
        verification_mode = int(f[6])

        return pin, dt, event_type, door_id, card_no
    except (ValueError, IndexError) as e:
        raise ValueError(f"Erreur de parsing: {raw} - {e}")
