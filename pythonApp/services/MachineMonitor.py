# services/MachineMonitor.py
import ctypes, logging, time
from ctypes import *
from typing import Optional
import json, logging
from datetime import datetime, timedelta

from domain.DoorType import DoorType
from kafka_service.kafkaservice import KafkaService
from services.DeviceMAnager import DeviceContext
from services.addAndAuthorizeUser import connect_to_device

# ------------------------------------------------------------------ #
# 1) PLComm DLL
# ------------------------------------------------------------------ #
from os import getenv

from services.websocket import send_pointage

PLCOMPRO_URL = getenv("PLCOMPRO_URL")
pl = ctypes.CDLL(PLCOMPRO_URL)

pl.Connect.argtypes = [c_char_p]
pl.Connect.restype = c_void_p
pl.Disconnect.argtypes = [c_void_p]
pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
pl.GetRTLog.restype = c_int

# ------------------------------------------------------------------ #
# 2) Kafka (un seul producteur global)
# ------------------------------------------------------------------ #
kafka = KafkaService(getenv("KAFKA_BROKER"), "rt_c3_group")


# ------------------------------------------------------------------ #
# 3) Connection monitoring utilities
# ------------------------------------------------------------------ #
def is_c3_handle_connected(handle):
    """Test if C3 handle is still connected"""
    if handle is None:
        return False

    try:
        # Test with a lightweight operation
        # Try to get RTLog with timeout to test connection
        BUF_SZ = 64  # Small buffer for connection test
        test_buf = create_string_buffer(BUF_SZ)

        # This should return quickly (0 or positive) if connected
        # Will throw exception or return negative if disconnected
        ret = pl.GetRTLog(handle, test_buf, BUF_SZ)
        return ret >= 0  # 0 = no data, positive = data, negative = error

    except Exception as e:
        logging.debug(f"Connection test failed: {e}")
        return False


def attempt_c3_reconnection(ctx: DeviceContext, max_retries=3):
    """Attempt to reconnect C3 device"""
    ip, port = ctx.machine.addresseip, str(ctx.machine.port)

    for attempt in range(max_retries):
        try:
            logging.info(f"🔄 Reconnexion C3 {attempt + 1}/{max_retries} pour {ip}")

            # Disconnect old handle if exists
            with ctx.lock:
                if ctx.handle:
                    try:
                        pl.Disconnect(ctx.handle)
                    except:
                        pass
                ctx.set_handle(None)

            # Try to establish new connection
            new_handle = connect_to_device(ip, port)
            if new_handle:
                # Test the new connection
                if is_c3_handle_connected(new_handle):
                    with ctx.lock:
                        ctx.set_handle(new_handle)
                    logging.info(f"✅ C3 {ip} reconnecté avec succès")
                    return True
                else:
                    # Connection failed, clean up
                    try:
                        pl.Disconnect(new_handle)
                    except:
                        pass

            time.sleep(5)  # Wait before next attempt

        except Exception as e:
            logging.error(f"❌ Échec reconnexion C3 tentative {attempt + 1} pour {ip}: {e}")
            time.sleep(5)

    logging.error(f"❌ Impossible de reconnecter C3 {ip} après {max_retries} tentatives")
    return False


# ------------------------------------------------------------------ #
# 4) Filtre heartbeat
# ------------------------------------------------------------------ #
def is_event(line: str) -> bool:
    """True si `line` est un véritable passage ou alarme."""
    f = line.strip().split(",")
    if len(f) < 7:
        return False  # trame incomplète
    door = int(f[6])
    if door == 200:  # heartbeat firmware récent
        return False
    if door == 0:  # heartbeat ancien firmware
        return False
    return True


# ------------------------------------------------------------------ #
# 5) Enhanced Thread de monitoring C3 with connection monitoring
# ------------------------------------------------------------------ #
def monitor_machine(ctx: DeviceContext, stop_evt):
    """Boucle temps-réel pour un contrôleur C3 / inBio avec monitoring de connexion."""
    ip, port = ctx.machine.addresseip, str(ctx.machine.port)
    BUF_SZ = 4096
    buf = create_string_buffer(BUF_SZ)
    TENANT = ctx.tenant
    BRANCH_ID = ctx.gymBranchId

    # Connection monitoring variables
    CONNECTION_CHECK_INTERVAL = 15  # Check every 30 seconds
    last_check_time = datetime.now()
    consecutive_failures = 0
    max_consecutive_failures = 3
    last_successful_read = datetime.now()
    READ_TIMEOUT = 60  # Consider connection dead if no successful read for 60 seconds


    while not stop_evt.is_set():

        # ░░ Periodic connection health check ░░
        now = datetime.now()
        if now - last_check_time > timedelta(seconds=CONNECTION_CHECK_INTERVAL):
            if ctx.handle and not is_c3_handle_connected(ctx.handle):
                consecutive_failures += 1
                logging.warning(
                    f"⚠️ C3 {ip} connexion test failed (échec {consecutive_failures}/{max_consecutive_failures})")

                if consecutive_failures >= max_consecutive_failures:
                    logging.error(f"🚨 C3 {ip} connexion perdue, tentative de reconnexion")

                    # Force reconnection
                    if attempt_c3_reconnection(ctx):
                        consecutive_failures = 0
                        last_successful_read = datetime.now()
                        logging.info(f"✅ C3 {ip} reconnecté après test périodique")
                    else:
                        logging.error(f"❌ Échec reconnexion périodique C3 {ip}")
            else:
                consecutive_failures = 0  # Reset on successful check

            last_check_time = now

        # ░░ Check for read timeout ░░
        if ctx.handle and now - last_successful_read > timedelta(seconds=READ_TIMEOUT):
            logging.warning(f"⏰ C3 {ip} timeout de lecture ({READ_TIMEOUT}s), tentative de reconnexion")
            if attempt_c3_reconnection(ctx):
                last_successful_read = datetime.now()
                consecutive_failures = 0
            continue

        # ░░ connexion ░░
        if ctx.handle is None:
            h = connect_to_device(ip, port)
            if h:
                with ctx.lock:
                    ctx.set_handle(h)  # handle partagé
                last_successful_read = datetime.now()
                consecutive_failures = 0
                logging.info(f"🔌 C3 {ip} connecté")
            else:
                time.sleep(1)
                continue  # retente

        # ░░ lecture RTLog ░░
        try:
            ret = pl.GetRTLog(ctx.handle, buf, BUF_SZ)
            if ret > 0:
                last_successful_read = datetime.now()
                consecutive_failures = 0
                raw = buf.value.decode()
                if not is_event(raw):
                    continue

                print(raw)
                # --------- parsing Pull-SDK ---------
                # Format : pin,time,verified,event,inout,param,door
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
                    porte =  DoorType.ENTREE

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
                    porte_type=porte.value
                )
                kafka.produce("rt_" + TENANT, payload)
                try:
                    send_pointage(json.loads(payload),"1003")
                except Exception as ex:
                    print(f"[WebSocket] Erreur envoi WS: {ex}")
                logging.info("📡 %s → %s", ip, payload)

            elif ret == 0:
                # No data, but connection is OK
                time.sleep(0.2)
            else:
                # Negative return value indicates error
                logging.warning(f"GetRTLog returned error {ret} for {ip}")
                raise Exception(f"GetRTLog error: {ret}")

        except Exception as exc:
            # crash GetRTLog ou perte réseau : on ferme puis on retente
            logging.error("RT %s: %s", ctx.machine.alias, exc, exc_info=True)

            with ctx.lock:
                try:
                    if ctx.handle:
                        pl.Disconnect(ctx.handle)
                finally:
                    ctx.set_handle(None)  # forcera reconnexion

            # Try immediate reconnection
            if attempt_c3_reconnection(ctx):
                last_successful_read = datetime.now()
                consecutive_failures = 0
                logging.info(f"✅ C3 {ip} reconnecté après erreur")
            else:
                time.sleep(1)

    # ░░ arrêt demandé ░░
    with ctx.lock:
        if ctx.handle:
            pl.Disconnect(ctx.handle)
        ctx.set_handle(None)
    logging.warning("🔌 RT C3 arrêté %s", ip)


def make_rt_json(*,
                 machine_id: int,
                 ip: str,
                 mtype: str,
                 pin: int,
                 state_code: int,
                 dt: str,
                 door_id: int,
                 card_no: Optional[str],
                 gym_branch_id: str,
                 porte_type:str) -> str:
    payload = {
        "id_machine": machine_id,
        "ip_adress": ip,
        "machine_type": mtype,
        "is_access_valid": state_code in (0, 29, 6),
        "pin": pin,
        "access_date_time": dt,
        "door_id": door_id,
        "gym_branch_id": gym_branch_id,
        "cardNo": card_no or "",
        "porte_type": porte_type
    }
    return json.dumps(payload, ensure_ascii=False)




def parse_c3_line(raw: str):
    """
    Parses a ZKTeco C3 Pull-SDK log line.
    Based on ZKTeco documentation format:
    Time,Pin(Employee No.),Card No,Door No,Event type code,Entry/Exit status,Verification mode

    Example: 2025-07-15 22:27:17,3035,10471248,1,0,1,6
    """
    f = raw.strip().split(',')
    if len(f) < 6:
        raise ValueError(f"Trame incomplète: {raw}")
    if len(f) > 4 and f[4] == '255':
        raise ValueError(f"Record de statut porte/alarme, pas un événement temps réel: {raw}")

    try:
        f_time = f[0]
        dt = datetime.strptime(f_time, "%Y-%m-%d %H:%M:%S")
        pin = int(f[1])
        card_no = f[2] if f[2] != '0' else None
        door_id = int(f[3])
        event_type = int(f[4])
        entry_exit_status = int(f[5]) if len(f) > 5 else 2
        verification_mode = int(f[6]) if len(f) > 6 else 200

        return pin, dt, event_type, door_id, card_no

    except (ValueError, IndexError) as e:
        raise ValueError(f"Erreur de parsing: {raw} - {e}")