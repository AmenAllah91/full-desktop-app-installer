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
pl.SetDeviceParam.argtypes = [c_void_p, c_char_p]
pl.SetDeviceParam.restype = c_int
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

# Trace de diagnostic — DÉSACTIVÉE. Passer à True pour la rallumer.
#
# Affiche la trame BRUTE de chaque événement retenu :
#
#     🔬 TRAME 192.168.1.205 | 2026-08-21 14:48:27,3056,4757514,1,0,1,6
#                               horodatage,Pin,CardNo,DoorID,EventType,InOut,Verified
#
# À quoi elle sert : `is_access_valid` vaut `event_type == 0`, et ce type n'est
# journalisé nulle part ailleurs. Un pointage rapporté « refusé » ne dit donc
# pas POURQUOI il l'a été. Rallumer cette trace est le moyen le plus court de
# le savoir quand un client décrit un comportement inexplicable à la porte.
#
# Ce qu'elle a permis de trouver le 2026-08-21 : des adhérents valides
# ressortaient une fois sur deux en « refusée » quand on badgeait vite. Les
# trames ont montré un EventType 20 corrélé à 11/11 avec un écart ≤ 2 s entre
# deux passages, y compris entre cartes DIFFÉRENTES — puis la lecture des
# paramètres du panneau a donné `Door1Intertime = 3`, l'intervalle minimum
# imposé par le lecteur. Ni un bug du pont, ni un problème d'abonnement.
#
# Coût quand elle est active : une ligne INFO par badge.
TRACE_TRAMES = False


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


def ensure_c3_session(ctx: DeviceContext, force_reconnect: bool = False) -> bool:
    """Garantit une session utilisable SANS en détruire une qui fonctionne.

    Remplace la reconnexion préventive qui précédait CHAQUE commande. Celle-ci
    reposait sur deux mesures de juin :

      - « une session C3 vit ~3,5 s, soit 14 appels de GetRTLog » ;
      - « seule la première commande qui suit un Connect aboutit ».

    Les deux sont infirmées par l'expérience du 20/08/2026 sur un C3 sain
    (experience_session_c3.py, 192.168.1.205) : 613 appels, aucun échec.

      A. temps réel seul ....... 238 GetRTLog en 60 s, 0 échec
      B. commandes seules ...... 10 GetDeviceData d'affilée, 0 échec
      C. les deux entrelacés ... 358 + 17 appels en 90 s sur le MÊME handle
                                 et le MÊME verrou, 0 échec

    La phase C reproduit exactement le motif de production. Renouveler la
    session « au cas où » ne protégeait donc de rien, et coûtait cher : chez
    vikingsgym, 1887 cycles de reconnexion en trois jours pour 259 tâches, en
    plus des ~9 renouvellements par minute du thread temps réel — soit une
    session neuve toutes les 6 secondes, 24h/24, sur un panneau dont la table
    de sockets est minuscule.

    La reprise reste entière : sur échec, l'appelant rappelle avec
    force_reconnect=True, et la boucle temps réel continue de renouveler sur
    -2. On cesse seulement de reconnecter sans raison.

    ⚠️ Mesuré sur UN SEUL panneau sain. Si un C3 expirait réellement ses
    sessions, le chemin de reprise ci-dessus le rattrape — au prix d'un essai
    perdu, pas d'une panne.
    """
    if force_reconnect:
        return attempt_c3_reconnection(ctx, max_retries=2)

    # Verrou pris sur toute la fonction : sans quoi deux threads pourraient
    # constater l'absence de handle en même temps et ouvrir chacun le leur,
    # ce qui fait tomber les DEUX connexions sur un C3. Les appelants le
    # détiennent déjà (RLock), la portée est donc inchangée en pratique.
    with ctx.lock:
        if ctx.handle:
            return True

        ip, port = ctx.machine.addresseip, str(ctx.machine.port)
        handle = connect_to_device(ip, port)
        if not handle:
            _record_renewal(ip, ok=False)
            return False

        ctx.set_handle(handle)
        _record_renewal(ip, ok=True)
        return True


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

    # Pin=0 SANS carte : le panneau n'a identifié personne. Ce sont ses propres
    # événements — porte, bouton, alarme — et non des passages d'adhérents.
    # La condition ne rejetait que le cas `event_type == 0`, donc tout autre
    # code passait et ressortait en « ENTREE refusée » sur l'écran du client,
    # sans nom ni photo. Relevé chez vikingsgym du 18 au 21/08/2026 :
    # **43 lignes fantômes sur 612**, soit 57 % de tous les refus affichés.
    #
    # Pin=0 AVEC une carte est en revanche une information utile : une carte
    # inconnue a été présentée. Elle est conservée. Les 3 occurrences du même
    # relevé portaient les cartes d'adhérents dont l'ADD_USER était resté
    # bloqué dans la file — le panneau ne les connaissait donc pas encore.
    if pin == 0 and card_no in ("0", ""):
        return False

    return True


# ─── Horloge des panneaux ────────────────────────────────────────────────
#
# Rien, dans YoGym, n'a jamais remis un C3 à l'heure. Les panneaux dérivent
# donc librement depuis leur installation, et c'est LEUR horodatage qui est
# enregistré comme heure de pointage. Mesuré le 20/08/2026 : 171 s de retard
# chez vikingsgym (dérive +18 s/jour, relevée sur trois jours de log) et 140 s
# sur le banc — un panneau qui n'a subi aucune coupure. Ce n'est donc pas un
# accident de terrain, c'est systémique : tout le parc est concerné.
#
# La référence est l'horloge du PC. Elle n'est pas parfaite — le service
# W32Time est à l'arrêt sur les postes constatés, elle tourne donc en roue
# libre elle aussi — mais un quartz de PC dérive de l'ordre de la minute par
# mois, contre ~9 minutes pour un C3.
HORLOGE_SEUIL = 30          # s d'écart en deçà desquels on ne touche à rien
HORLOGE_INTERVALLE = 12 * 3600
HORLOGE_REESSAI = 3600      # panneau illisible : on retente dans 1 h, pas dans 12
_horloge_prochain = {}      # ip -> quand recontrôler (epoch)


def _decoder_datetime(v: int) -> datetime:
    """Entier ZKTeco -> datetime."""
    s = v % 60; v //= 60
    mi = v % 60; v //= 60
    h = v % 24; v //= 24
    d = v % 31 + 1; v //= 31
    mo = v % 12 + 1; v //= 12
    return datetime(v + 2000, mo, d, h, mi, s)


def _encoder_datetime(dt: datetime) -> int:
    """datetime -> entier ZKTeco."""
    return ((((dt.year - 2000) * 12 * 31 + (dt.month - 1) * 31 + (dt.day - 1))
             * 24 + dt.hour) * 60 + dt.minute) * 60 + dt.second


def synchroniser_horloge(ctx: DeviceContext) -> Optional[float]:
    """Lit l'horloge du panneau et la recale si elle a dérivé.

    Retourne l'écart mesuré en secondes (positif = panneau en retard), ou None
    si l'horloge n'a pas pu être lue. Aucune écriture tant que l'écart reste
    sous HORLOGE_SEUIL : un panneau déjà juste ne reçoit rien.

    Vérifié sur un C3 réel le 20/08/2026 : +140 s avant, +0,8 s après relecture.
    """
    ip = ctx.machine.addresseip
    with ctx.lock:
        if not ctx.handle:
            return None

        buf = create_string_buffer(256)
        if pl.GetDeviceParam(ctx.handle, buf, 256, b"DateTime") < 0:
            logging.debug("Horloge %s illisible (PullLastError=%s)", ip, _pull_last_error())
            return None

        brut = buf.value.decode(errors="ignore").strip()
        valeur = brut.split("=", 1)[1] if "=" in brut else brut
        try:
            panneau = _decoder_datetime(int(valeur))
        except (ValueError, OverflowError) as exc:
            logging.warning("⚠️ Horloge %s illisible : %r (%s)", ip, brut, exc)
            return None

        maintenant = datetime.now()
        ecart = (maintenant - panneau).total_seconds()
        if abs(ecart) <= HORLOGE_SEUIL:
            return ecart

        # On réencode l'instant présent, pas `maintenant` : les deux appels SDK
        # ont pris quelques centaines de millisecondes.
        if pl.SetDeviceParam(
                ctx.handle,
                f"DateTime={_encoder_datetime(datetime.now())}".encode("utf-8")) < 0:
            logging.warning("⚠️ Horloge %s : recalage refusé par le panneau "
                            "(PullLastError=%s), écart %+.0f s",
                            ip, _pull_last_error(), ecart)
            return ecart

    logging.info("🕐 Horloge %s recalée : %+.0f s d'écart corrigé", ip, ecart)
    return ecart


def synchroniser_horloge_si_besoin(ctx: DeviceContext) -> Optional[float]:
    """Contrôle l'horloge au premier passage, puis toutes les 12 h.

    Appelée depuis refresh_machines_loop, qui parcourt déjà les machines toutes
    les 5 min et sait déjà si un C3 a une session ouverte — au démarrage le
    thread temps réel n'a pas encore de handle, et rien ne serait lisible.
    Le premier contrôle a donc lieu dans les 5 minutes qui suivent le démarrage.
    """
    ip = ctx.machine.addresseip
    if time.time() < _horloge_prochain.get(ip, 0):
        return None

    ecart = synchroniser_horloge(ctx)
    _horloge_prochain[ip] = time.time() + (
        HORLOGE_INTERVALLE if ecart is not None else HORLOGE_REESSAI)
    return ecart


def evenements_du_tampon(raw: str, ip: str = ""):
    """Découpe un tampon GetRTLog en événements exploitables.

    GetRTLog rend PLUSIEURS événements dans un même tampon quand deux badgeages
    tombent entre deux lectures : les trames sont alors collées, séparées par
    \\r\\n. Le tampon était traité comme une trame unique — is_event() passait
    (deux trames de 7 champs font 14 champs, donc ≥ 7), puis parse_c3_line()
    faisait split(",") sur le tout et tentait int() sur un champ à cheval sur
    les deux lignes. Vu en production chez vikingsgym le 19/08/2026 à 19:38 :

        ValueError: invalid literal for int() with base 10:
        '6\\r\\n2026-08-19 19:35:29'

    L'exception remontait jusqu'au try de la boucle temps réel, qui appelle
    _clean_exit() et return : le thread mourait, le watchdog le relançait 10 s
    plus tard, et les DEUX pointages étaient perdus.

    Le parsing de chaque ligne est isolé : une trame illisible ne coûte plus
    que CE pointage, ni le thread ni les pointages voisins.
    """
    evenements = []
    for ligne in (raw or "").splitlines():
        ligne = ligne.strip()
        if not ligne or not is_event(ligne):
            continue
        if TRACE_TRAMES:
            # Champs : horodatage,Pin,CardNo,DoorID,EventType,InOutState,Verified
            logging.info("🔬 TRAME %s | %s", ip, ligne)
        try:
            evenements.append(parse_c3_line(ligne))
        except ValueError as exc:
            logging.warning("⚠️ Trame temps réel ignorée sur %s : %s", ip, exc)
    return evenements


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
                # Le panneau a répondu : la session est vivante, et c'est tout ce
                # que `connected` a besoin de savoir. À rafraîchir ICI et pas
                # seulement dans la boucle ci-dessous : evenements_du_tampon
                # écarte les trames d'état de porte (255), très majoritaires sur
                # un panneau au repos. Un C3 qui n'émet que celles-là serait
                # affiché « déconnecté » au bout de 15 s alors que sa session
                # n'a jamais faibli.
                ctx.adapter.last_seen = time.time()

                for pin, dt, state, door_id, card_no in evenements_du_tampon(raw, ip):
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
                # Le panneau vient de répondre : c'est ce qui atteste que la
                # session est vivante, et c'est désormais la SEULE source du
                # drapeau `connected` publié au front (voir _c3_session_vivante,
                # qui remplace la sonde TCP). Sans ce rafraîchissement, une salle
                # sans badgeage passerait pour déconnectée au bout de 15 s.
                ctx.adapter.last_seen = time.time()
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
