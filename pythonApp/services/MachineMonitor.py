# services/MachineMonitor.py
import ctypes
import json
import logging
import socket
import threading
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
pl.GetDeviceParam.argtypes = [c_void_p, c_char_p, c_int, c_char_p]
pl.GetDeviceParam.restype = c_int
pl.PullLastError.restype = c_int

# GetRTLog distingue deux retours qu'il ne faut surtout pas confondre :
#
#   0  → aucun événement en attente. Cas nominal, la session est vivante.
#  -2  → session expirée. Mesuré sur un C3 réel : après un Connect, GetRTLog
#        répond pendant ~3,5 s (14 appels à 200 ms) puis renvoie -2 de façon
#        définitive. La session ne revient jamais d'elle-même et plus aucune
#        commande n'aboutit — GetDeviceData et SetDeviceData renvoient -2 à leur
#        tour. C'est la cause des pointages muets ET des give access en échec.
#
# Les deux avaient été rangés ensemble dans un « rien à lire » bénin : c'était
# l'erreur. PullLastError vaut bien 0 sur un -2, mais cela signifie seulement que
# le SDK ne remonte pas d'erreur applicative — pas que la session est vivante.
SESSION_EXPIRED = -2


def _pull_last_error():
    """Vrai code d'erreur du Pull SDK."""
    try:
        return pl.PullLastError()
    except Exception:
        return "?"


# Le renouvellement de session est un événement de ROUTINE : une session C3 vit
# ~3,5 s, donc chaque panneau se reconnecte une dizaine de fois par minute. En
# INFO, cela produisait quatre lignes toutes les cinq secondes et noyait tout le
# reste du journal.
#
# Les succès passent donc en DEBUG. Pour ne pas perdre le signal pour autant, un
# résumé est émis par panneau toutes les RENEW_SUMMARY_INTERVAL secondes : une
# ligne au lieu de plusieurs centaines, en WARNING dès qu'un échec est survenu.
RENEW_SUMMARY_INTERVAL = 300
_renew_stats: dict = {}
_renew_lock = threading.Lock()


def _record_renewal(ip: str, ok: bool) -> None:
    now = time.time()
    with _renew_lock:
        st = _renew_stats.setdefault(ip, {"ok": 0, "fail": 0, "since": now})
        st["ok" if ok else "fail"] += 1
        if now - st["since"] < RENEW_SUMMARY_INTERVAL:
            return
        elapsed = now - st["since"]
        okc, failc = st["ok"], st["fail"]
        _renew_stats[ip] = {"ok": 0, "fail": 0, "since": now}

    minutes = max(1, round(elapsed / 60))
    if failc:
        logging.warning("🔄 C3 %s : %s renouvellements de session en %s min, "
                        "dont %s en échec", ip, okc + failc, minutes, failc)
    else:
        logging.info("🔄 C3 %s : %s renouvellements de session en %s min, "
                     "aucun échec", ip, okc, minutes)

kafka = KafkaService(getenv("KAFKA_BROKER"), "rt_c3_group")
KAFKA_POINTAGE_TOPIC = getenv("KAFKA_TOPIC", "rt_pointage")

ACCESS_GRANTED = {0}


# Il n'y a plus de sonde de vivacité séparée. Toutes celles essayées ici (GetRTLog
# puis GetDeviceParam) consommaient un appel SDK sur une session qui n'en sert
# qu'un nombre limité avant d'expirer : la sonde précipitait donc la panne qu'elle
# prétendait détecter. GetRTLog, appelé cinq fois par seconde par la boucle temps
# réel, est le seul indicateur de santé nécessaire.


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
            logging.debug("🔄 Reconnexion C3 %s/%s pour %s",
                          attempt + 1, max_retries, ip)

            with ctx.lock:
                if ctx.handle:
                    try:
                        pl.Disconnect(ctx.handle)
                    except Exception:
                        pass
                ctx.set_handle(None)

            new_handle = connect_to_device(ip, port)
            # Pas de sonde de vérification ici : sur un C3, le premier appel qui
            # suit un Connect est le seul qui passe à coup sûr. Le consommer pour
            # tester la session revenait à la gâcher pour la commande qui suit.
            # Le SDK a déjà validé la session en retournant un handle.
            if new_handle:
                with ctx.lock:
                    ctx.set_handle(new_handle)
                logging.debug("✅ C3 %s reconnecté", ip)
                _record_renewal(ip, ok=True)
                return True

            time.sleep(5)
        except Exception as e:
            logging.error(f"❌ Échec reconnexion C3 tentative {attempt + 1} pour {ip}: {e}")
            time.sleep(5)

    logging.error(f"❌ Impossible de reconnecter C3 {ip} après {max_retries} tentatives")
    _record_renewal(ip, ok=False)
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

    consecutive_failures = 0
    last_successful_read = datetime.now()
    READ_TIMEOUT = 300


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

        # Pas de suicide sur silence d'événements : une salle vide n'est pas un
        # panneau en panne. Ce garde-fou coupait le thread toutes les 180 s dès
        # que personne ne badgeait, et le watchdog le relançait indéfiniment.
        # Retiré côté ZKEM auparavant, il subsistait ici.

        # AUCUNE sonde TCP ici. Un C3 ne délivre qu'un seul handle à la fois :
        # ouvrir une socket parallèle sur son port SDK fait tomber la session en
        # cours. check_device_tcp a été introduit le 2026-07-06 (commit a2a39e5,
        # « fix pointage thread ») et appelé jusqu'à cinq fois par seconde —
        # c'est l'origine des pertes de connexion et des -2 sur les commandes.
        # Les installations antérieures à ce commit n'ont pas le problème.
        #
        # La santé de la session s'évalue sur le handle existant, sans ouvrir
        # quoi que ce soit.
        # Plus de sonde de santé périodique ici. GetRTLog, appelé cinq fois par
        # seconde juste en dessous, détecte lui-même la session expirée (-2) et la
        # renouvelle : la sonde n'apprenait rien de plus. Elle était même nuisible,
        # car chaque GetDeviceParam consommait un appel sur une session qui n'en
        # sert qu'un petit nombre avant d'expirer.

        if ctx.handle and now - last_successful_read > timedelta(seconds=READ_TIMEOUT):
            logging.warning(f"⏰ C3 {ip} timeout de lecture ({READ_TIMEOUT}s), le thread va s'arrêter")
            _clean_exit(connected=False, error=f"Timeout de lecture après {READ_TIMEOUT}s")
            return

        if ctx.handle is None:
            h = connect_to_device(ip, port)
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

        # Pas de sonde TCP ici : cette boucle tourne ~5 fois par seconde, et
        # check_device_tcp ouvre puis ferme une connexion à chaque appel. Sous
        # Windows chaque socket reste ~120 s en TIME_WAIT : on maintenait donc en
        # permanence des centaines de connexions vers le panneau (constaté sur un
        # C3 réel). Inutile de toute façon : GetRTLog signale de lui-même la perte
        # du lien, et le renouvellement de session sur -2, plus bas, assure la
        # reprise.
        try:
            # Le handle C3 est PARTAGÉ avec le thread de la file de tâches, qui
            # prend déjà ctx.lock autour de ses opérations. Sans ce verrou ici,
            # les deux threads entrent dans le SDK en même temps sur le même
            # handle : le panneau répond alors -2 à la commande en cours
            # (GetDeviceData pour la recherche par carte, donc tout ADD_USER).
            # Section réduite au seul appel SDK — le traitement du pointage
            # (Kafka, WebSocket) reste hors verrou.
            with ctx.lock:
                ret = pl.GetRTLog(ctx.handle, buf, BUF_SZ)
            if ret > 0:
                last_successful_read = datetime.now()
                consecutive_failures = 0
                raw = buf.value.decode(errors="ignore")
                if not is_event(raw):
                    continue


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
                    tenant=TENANT,
                )

                kafka.produce(KAFKA_POINTAGE_TOPIC, payload)
                try:
                    send_pointage(json.loads(payload), BRANCH_ID)
                except Exception as ex:
                    logging.error("[WebSocket] Erreur envoi WS: %s", ex)

                ctx.adapter.last_seen = time.time()
                ctx.adapter.event_count += 1
                logging.info("📡 %s → %s", ip, payload)

            elif ret == 0:
                # Aucun événement en attente : c'est le cas nominal, pas une panne.
                last_successful_read = datetime.now()
                time.sleep(2.0 if throttle_event.is_set() else 0.2)

            elif ret == SESSION_EXPIRED:
                # Session expirée — À RENOUVELER, surtout pas à ignorer.
                #
                # Mesuré sur un C3 réel : une session répond à GetRTLog pendant
                # ~3,5 s (14 appels), puis renvoie -2 définitivement. Elle ne
                # revient jamais d'elle-même, et plus aucune commande ne passe.
                #
                # La version de juin renouvelait la session sans le savoir : elle
                # traitait tout retour négatif comme une panne, tuait le thread, et
                # le watchdog reconnectait. En prenant -2 pour un « rien à lire »
                # bénin, ce renouvellement a disparu — d'où les pointages muets et
                # les give access en échec.
                #
                # On renouvelle donc sur place : moins brutal que de tuer le
                # thread, et attempt_c3_reconnection déconnecte avant de
                # reconnecter, ce qui respecte la règle du handle unique.
                if not attempt_c3_reconnection(ctx, max_retries=2):
                    _clean_exit(connected=False, error="session C3 non renouvelable")
                    return
                last_successful_read = datetime.now()
                consecutive_failures = 0
                time.sleep(2.0 if throttle_event.is_set() else 0.2)

            else:
                logging.warning("GetRTLog erreur %s pour %s (PullLastError=%s)",
                                ret, ip, _pull_last_error())
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
    tenant: str = "",
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
        "tenant": tenant,
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
