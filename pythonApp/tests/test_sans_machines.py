"""Tests du pont NE NÉCESSITANT NI MACHINES NI pythonApp démarré.

Logique pure, exécutable n'importe où : ils protègent les règles de parsing
et de filtrage sur lesquelles reposent l'import d'empreintes et le ciblage
par branche.

    venv\\Scripts\\python.exe tests\\test_sans_machines.py
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnais import Suite, verifier, egal  # noqa: E402

suite = Suite("PONT — logique pure (aucune machine requise)")


# ─── Filtre de branche des actions d'empreinte ───────────────────────────
#
# Réplique de la règle de main.py::process_fingerprint_actions. Un message
# sans gymBranchId doit rester diffusé à toutes les branches (compatibilité
# avec les postes non mis à jour) ; avec gymBranchId, seul le poste concerné
# doit le traiter.

def branche_traite(message_branche, poste_branche):
    cible = message_branche
    if cible is not None and str(cible) != str(poste_branche):
        return False
    return True


@suite.test("message sans gymBranchId -> traite par toutes les branches")
def _():
    verifier(branche_traite(None, "1003"), "un message sans branche doit etre traite")


@suite.test("message cible sur la branche du poste -> traite")
def _():
    verifier(branche_traite("1003", "1003"), "meme branche, doit traiter")


@suite.test("message cible sur une AUTRE branche -> ignore")
def _():
    verifier(not branche_traite("1004", "1003"), "autre branche, ne doit pas traiter")


@suite.test("comparaison int/str tolerante (1003 == '1003')")
def _():
    verifier(branche_traite(1003, "1003"), "le type ne doit pas changer le resultat")


# ─── Parsing templatev10 (C3 / PullSDK) ──────────────────────────────────
#
# Le SDK renvoie tantôt du CSV avec entête, tantôt du clé=valeur. Une
# régression ici casserait l'import d'empreintes depuis une pointeuse C3.

def _parser():
    from services.adapters import PlcommAdapter
    return PlcommAdapter.__dict__["_parse_templatev10_rows"]


@suite.test("parse le format CSV avec entete")
def _():
    parse = _parser()
    brut = ("Size,UID,Pin,FingerID,Valid,Template,Resverd,EndTag\r\n"
            "1198,12,3040,1,1,QUJDRA==,,\r\n"
            "1198,12,3040,2,1,RUZHSA==,,")
    lignes = parse(None, brut)
    egal(len(lignes), 2, "deux gabarits attendus")
    egal(lignes[0]["Pin"], "3040", "Pin mal extrait")
    egal(lignes[0]["FingerID"], "1", "FingerID mal extrait")
    egal(lignes[1]["Template"], "RUZHSA==", "Template mal extrait")


@suite.test("parse le format cle=valeur separe par tabulations")
def _():
    parse = _parser()
    brut = "Pin=3040\tFingerID=1\tTemplate=QUJDRA==\r\nPin=3040\tFingerID=2\tTemplate=RUZHSA=="
    lignes = parse(None, brut)
    egal(len(lignes), 2, "deux gabarits attendus")
    egal(lignes[0]["Pin"], "3040", "Pin mal extrait en KV")
    egal(lignes[1]["FingerID"], "2", "FingerID mal extrait en KV")


@suite.test("entree vide -> liste vide, pas d'exception")
def _():
    parse = _parser()
    egal(parse(None, ""), [], "chaine vide")
    egal(parse(None, None), [], "None")


@suite.test("CSV sans entete (1re ligne numerique) -> entete par defaut")
def _():
    parse = _parser()
    brut = "1198,12,3040,1,1,QUJDRA==,,"
    lignes = parse(None, brut)
    egal(len(lignes), 1, "une ligne attendue")
    egal(lignes[0]["Pin"], "3040", "repli sur l'entete connu")


# ─── Recherche d'un PIN par numéro de carte ──────────────────────────────
#
# get_pin_by_card demande désormais au panneau de filtrer, au lieu de rapatrier
# toute la table `user`. Mesuré chez vikingsgym le 20/08/2026 sur 7379 fiches :
# 0,24 s et 112 octets contre 3,21 s et 291 Ko, soit 13x. Cette lecture se fait
# sous ctx.lock — pendant tout ce temps le thread temps réel ne lit rien.
#
# La règle de correspondance est partagée entre la lecture filtrée et le repli
# sur table complète : si les deux divergeaient, une même carte donnerait deux
# résultats selon la taille du club. C'est ce que ces tests verrouillent.

TABLE = ("UID,CardNo,Pin,Password,Group,StartTime,EndTime,Name,SuperAuthorize\r\n"
         "3690,0,51308,,0,20230224,20230523,,0\r\n"
         "1201,7809258,2287,,0,20260101,20261231,Ali,0\r\n"
         "1202,0009281720,3040,,0,20260101,20261231,Sonia,0\r\n"
         "1203,10460966,0,,0,20260101,20261231,Sans pin,0")


def _regle():
    from services.adapters import PlcommAdapter
    return PlcommAdapter._pin_depuis_table


@suite.test("carte presente -> son PIN")
def _():
    egal(_regle()(TABLE, "7809258")[0], "2287", "PIN mal retrouve")


@suite.test("zeros de tete ignores des DEUX cotes")
def _():
    # Le backend envoie « 0008581861 », le panneau stocke tantot avec, tantot
    # sans. La comparaison doit se faire sur la valeur depouillee.
    egal(_regle()(TABLE, "9281720")[0], "3040", "les zeros du panneau doivent etre ignores")


@suite.test("carte absente -> None, avec le nombre de fiches examinees")
def _():
    pin, examinees = _regle()(TABLE, "999999")
    verifier(pin is None, "une carte absente ne doit rien rendre")
    egal(examinees, 4, "le compte de fiches sert au message de log")


@suite.test("fiche sans PIN exploitable (Pin=0) -> ignoree")
def _():
    verifier(_regle()(TABLE, "10460966")[0] is None,
             "un Pin a 0 ne doit jamais etre rendu")


@suite.test("en-tete illisible -> None, pas d'exception")
def _():
    verifier(_regle()("nimporte,quoi\r\n1,2", "7809258")[0] is None,
             "un en-tete sans CardNo/Pin ne doit pas faire lever d'erreur")


@suite.test("ligne tronquee -> ignoree sans casser le balayage")
def _():
    brut = ("UID,CardNo,Pin,Password,Group,StartTime,EndTime,Name,SuperAuthorize\r\n"
            "9999,tronquee\r\n"
            "1201,7809258,2287,,0,20260101,20261231,Ali,0")
    egal(_regle()(brut, "7809258")[0], "2287",
         "une ligne mal formee ne doit pas masquer les suivantes")


# ─── Horloge des panneaux ────────────────────────────────────────────────
#
# Rien n'a jamais remis un C3 à l'heure : 171 s de retard chez vikingsgym,
# 140 s sur le banc qui n'a subi aucune coupure. C'est l'horodatage DU PANNEAU
# qui devient l'heure de pointage, donc tout le parc enregistre faux.
#
# L'encodage ZKTeco est le point sensible : un mois ou un jour mal placé
# décalerait les pointages de plusieurs semaines. La partie SDK elle-même a été
# vérifiée sur un C3 réel le 20/08/2026 (+140 s -> +0,8 s).

def _horloge():
    from services import MachineMonitor as mm
    return mm


class _FauxCtxHorloge:
    def __init__(self, handle=None):
        from threading import RLock
        self.lock = RLock()
        self.handle = handle
        self.machine = type("M", (), {"addresseip": "192.168.1.205", "port": 4370})()


@suite.test("encodage ZKTeco de l'heure : aller-retour exact")
def _():
    from datetime import datetime
    mm = _horloge()
    for dt in (datetime(2026, 8, 20, 16, 55, 43),
               datetime(2026, 1, 1, 0, 0, 0),
               datetime(2026, 12, 31, 23, 59, 59),
               datetime(2026, 2, 28, 12, 30, 15)):
        egal(mm._decoder_datetime(mm._encoder_datetime(dt)), dt,
             f"l'heure doit survivre a l'aller-retour ({dt})")


@suite.test("valeur relevee sur un C3 reel -> heure attendue")
def _():
    # Garde-fou contre une inversion mois/jour : cet entier a ete lu sur le
    # panneau du banc, l'heure correspondante est connue.
    from datetime import datetime
    egal(_horloge()._decoder_datetime(856094223),
         datetime(2026, 8, 20, 11, 57, 3), "decodage d'une valeur reelle")


@suite.test("sans session ouverte -> None, aucune exception")
def _():
    verifier(_horloge().synchroniser_horloge(_FauxCtxHorloge(handle=None)) is None,
             "un panneau sans session ne doit pas faire lever d'erreur")


@suite.test("cadence : controle au 1er passage, puis plus avant 12 h")
def _():
    import time
    mm = _horloge()
    vrai = mm.synchroniser_horloge
    appels = []
    mm.synchroniser_horloge = lambda ctx: appels.append(1) or 3.0
    mm._horloge_prochain.clear()
    try:
        ctx = _FauxCtxHorloge(handle=1)
        egal(mm.synchroniser_horloge_si_besoin(ctx), 3.0, "1er passage : controle")
        verifier(mm.synchroniser_horloge_si_besoin(ctx) is None, "2e passage : ignore")
        egal(len(appels), 1, "un seul controle effectif attendu")
        restant = mm._horloge_prochain[ctx.machine.addresseip] - time.time()
        verifier(11.9 * 3600 < restant <= 12 * 3600,
                 f"prochain controle dans ~12 h (obtenu {restant / 3600:.1f} h)")
    finally:
        mm.synchroniser_horloge = vrai
        mm._horloge_prochain.clear()


@suite.test("horloge illisible -> nouvelle tentative dans 1 h, pas dans 12")
def _():
    import time
    mm = _horloge()
    vrai = mm.synchroniser_horloge
    mm.synchroniser_horloge = lambda ctx: None
    mm._horloge_prochain.clear()
    try:
        ctx = _FauxCtxHorloge(handle=1)
        verifier(mm.synchroniser_horloge_si_besoin(ctx) is None, "lecture ratee -> None")
        restant = mm._horloge_prochain[ctx.machine.addresseip] - time.time()
        verifier(0.9 * 3600 < restant <= 3600,
                 f"un panneau illisible doit etre retente dans ~1 h (obtenu {restant / 3600:.1f} h)")
    finally:
        mm.synchroniser_horloge = vrai
        mm._horloge_prochain.clear()


# ─── Découpage du tampon temps réel (evenements_du_tampon) ───────────────
#
# GetRTLog colle plusieurs trames dans un même tampon quand deux badgeages
# tombent entre deux lectures. Le tampon était parsé comme une trame unique :
# le thread temps réel mourait sur un ValueError et le watchdog le relançait
# 10 s plus tard, deux pointages perdus. Vu chez vikingsgym le 19/08/2026.

def _tampon():
    from services.MachineMonitor import evenements_du_tampon
    return evenements_du_tampon


# Trame réelle relevée en production, telle que le SDK l'a rendue.
TAMPON_REEL = ("2026-08-19 19:35:26,4148,8612806,2,29,1,6\r\n"
               "2026-08-19 19:35:29,4148,8612806,1,29,0,6")


@suite.test("deux evenements colles -> les DEUX sont lus (regression 19/08)")
def _():
    ev = _tampon()(TAMPON_REEL)
    egal(len(ev), 2, "les deux pointages doivent survivre")
    egal(ev[0][0], 4148, "pin du 1er evenement")
    egal(ev[1][0], 4148, "pin du 2e evenement")
    egal(ev[0][3], 2, "door_id du 1er evenement")
    egal(ev[1][3], 1, "door_id du 2e evenement")
    egal(ev[0][1].strftime("%H:%M:%S"), "19:35:26", "horodatage du 1er")
    egal(ev[1][1].strftime("%H:%M:%S"), "19:35:29", "horodatage du 2e")


@suite.test("un tampon colle ne leve plus d'exception")
def _():
    # Le coeur de la regression : c'est l'exception, pas la perte d'un
    # pointage, qui tuait le thread.
    try:
        _tampon()(TAMPON_REEL)
    except Exception as e:
        raise AssertionError(f"aucune exception ne doit sortir : {type(e).__name__}: {e}")


@suite.test("evenement unique -> toujours lu")
def _():
    ev = _tampon()("2026-08-19 19:35:26,4148,8612806,2,29,1,6")
    egal(len(ev), 1, "un evenement seul doit rester lu")
    egal(ev[0][0], 4148, "pin mal extrait")


@suite.test("trame illisible -> seule celle-la est perdue")
def _():
    brut = ("2026-08-19 19:35:26,4148,8612806,2,29,1,6\r\n"
            "PAS-UNE-DATE,4148,8612806,1,29,0,6\r\n"
            "2026-08-19 19:35:31,4149,8612807,1,29,0,6")
    ev = _tampon()(brut)
    egal(len(ev), 2, "les trames valides doivent survivre a leur voisine cassee")
    egal([e[0] for e in ev], [4148, 4149], "mauvais evenements conserves")


@suite.test("tampon vide ou blanc -> liste vide, pas d'exception")
def _():
    egal(_tampon()(""), [], "chaine vide")
    egal(_tampon()(None), [], "None")
    egal(_tampon()("\r\n\r\n"), [], "lignes vides")


@suite.test("statut de porte (event_type 255) -> ignore, pas d'erreur")
def _():
    brut = ("2026-08-19 19:35:26,0,0,1,255,0,0\r\n"
            "2026-08-19 19:35:29,4148,8612806,1,29,0,6")
    ev = _tampon()(brut)
    egal(len(ev), 1, "seul le vrai pointage doit ressortir")
    egal(ev[0][0], 4148, "le pointage doit etre celui de l'adherent")


# ─── Événements du panneau sans porteur (Pin=0) ──────────────────────────
#
# is_access_valid vaut event_type == 0 : tout autre code ressort en « ENTREE
# refusée » sur l'écran du client. Le filtre ne rejetait Pin=0 que lorsque
# event_type valait 0 lui aussi, donc les événements propres au panneau —
# porte, bouton, alarme — s'affichaient comme des refus, sans nom ni photo.
# Relevé chez vikingsgym du 18 au 21/08/2026 : 43 lignes fantômes sur 612,
# soit 57 % de tous les refus affichés.

@suite.test("Pin=0 sans carte -> ecarte, quel que soit l'event_type")
def _():
    from services.MachineMonitor import is_event
    for code in (0, 5, 20, 23, 27):
        verifier(not is_event("2026-08-21 14:22:01,0,0,1,%d,1,6" % code),
                 "event_type %d avec Pin=0 et CardNo=0 doit etre ecarte" % code)
    verifier(not is_event("2026-08-21 14:22:01,0,,1,5,1,6"),
             "champ carte vide : meme traitement que CardNo=0")


@suite.test("Pin=0 AVEC une carte -> conserve (carte inconnue presentee)")
def _():
    # Information utile : quelqu'un a presente une carte que le panneau ne
    # connait pas. Chez vikingsgym, les 3 occurrences portaient les cartes
    # d'adherents dont l'ADD_USER etait reste bloque dans la file.
    from services.MachineMonitor import is_event
    verifier(is_event("2026-08-20 08:19:03,0,9334952,1,27,1,6"),
             "une carte inconnue presentee doit rester visible")


@suite.test("un adherent reel n'est jamais ecarte, meme sans carte")
def _():
    # Les employes de vikingsgym entrent par empreinte : Pin renseigne,
    # CardNo=0. Ces 42 passages releves en production doivent survivre au
    # filtre — c'est precisement ce que la condition ne doit pas attraper.
    from services.MachineMonitor import is_event
    verifier(is_event("2026-08-21 10:00:00,40004,0,1,0,1,6"),
             "entree par empreinte d'un employe (Pin renseigne, sans carte)")
    verifier(is_event("2026-08-21 14:38:51,3058,14736594,1,11,1,6"),
             "event_type 11 sur un adherent reel")


# ─── Réutilisation de session C3 (ensure_c3_session) ─────────────────────
#
# Le pont reconnectait avant CHAQUE commande, sur la croyance qu'une session
# C3 ne sert qu'un appel. L'expérience du 20/08/2026 l'a infirmée (613 appels
# sans échec). Ces tests verrouillent le nouveau comportement : emprunter la
# session en cours, et ne la renouveler qu'après un échec avéré.

class _FauxCtx:
    """Contexte minimal : ce que ensure_c3_session touche réellement."""

    def __init__(self, handle=None):
        from threading import RLock
        self.lock = RLock()
        self.handle = handle
        self.machine = type("M", (), {"addresseip": "192.168.1.205", "port": 4370})()
        self.poses = []

    def set_handle(self, h):
        self.handle = h
        self.poses.append(h)


def _ensure(faux, force=False, connexion=None):
    """Appelle ensure_c3_session en neutralisant le SDK et le journal."""
    from services import MachineMonitor as mm
    vrai_connect, vrai_journal = mm.connect_to_device, mm._record_renewal
    appels = {"connect": 0}

    def connect_espion(ip, port):
        appels["connect"] += 1
        return connexion

    mm.connect_to_device = connect_espion
    mm._record_renewal = lambda ip, ok: None
    try:
        return mm.ensure_c3_session(faux, force_reconnect=force), appels
    finally:
        mm.connect_to_device = vrai_connect
        mm._record_renewal = vrai_journal


@suite.test("session existante -> reutilisee, AUCUNE reconnexion")
def _():
    faux = _FauxCtx(handle=1234)
    ok, appels = _ensure(faux)
    verifier(ok, "une session vivante doit etre acceptee")
    egal(appels["connect"], 0, "le SDK ne doit pas etre rappele")
    egal(faux.handle, 1234, "le handle en cours ne doit pas changer")
    egal(faux.poses, [], "aucun nouveau handle ne doit etre publie")


@suite.test("aucune session -> une seule connexion, handle publie")
def _():
    faux = _FauxCtx(handle=None)
    ok, appels = _ensure(faux, connexion=777)
    verifier(ok, "une connexion reussie doit rendre True")
    egal(appels["connect"], 1, "une seule connexion attendue")
    egal(faux.handle, 777, "le nouveau handle doit etre publie dans le contexte")


@suite.test("connexion impossible -> False, sans handle fantome")
def _():
    faux = _FauxCtx(handle=None)
    ok, _ = _ensure(faux, connexion=None)
    verifier(not ok, "un echec de connexion doit rendre False")
    egal(faux.handle, None, "aucun handle ne doit etre pose en cas d'echec")


@suite.test("force_reconnect -> renouvellement reel, session en cours ignoree")
def _():
    from services import MachineMonitor as mm
    faux = _FauxCtx(handle=1234)
    vrai = mm.attempt_c3_reconnection
    vus = []
    mm.attempt_c3_reconnection = lambda ctx, max_retries=3: vus.append(max_retries) or True
    try:
        verifier(mm.ensure_c3_session(faux, force_reconnect=True), "doit rendre True")
    finally:
        mm.attempt_c3_reconnection = vrai
    egal(vus, [2], "le renouvellement doit passer par attempt_c3_reconnection")


# ─── Encodage des gabarits ───────────────────────────────────────────────

@suite.test("aller-retour base64 d'un gabarit binaire")
def _():
    gabarit = bytes(range(256)) * 4
    encode = base64.b64encode(gabarit).decode("utf-8")
    egal(base64.b64decode(encode), gabarit, "le gabarit doit survivre a l'aller-retour")


@suite.test("base64 invalide -> erreur detectable (route /fingerprint/push -> 400)")
def _():
    try:
        base64.b64decode("pas du base64 !!!", validate=True)
        raise AssertionError("aurait du lever")
    except AssertionError:
        raise
    except Exception:
        pass  # comportement attendu


# ─── Créneaux horaires ZKTeco (services/zkem_timezone.py) ────────────────
#
# L'encodage est la pièce la plus facile à casser sans s'en apercevoir : une
# chaîne mal formée est acceptée par SetTZInfo et produit une porte qui ne
# s'ouvre jamais, ou pire, qui s'ouvre tout le temps. Format relevé sur
# SenseFace 3A le 2026-08-25 : 56 caractères, 7 jours × HHMM+HHMM, DIMANCHE
# EN PREMIER.

from services.zkem_timezone import (  # noqa: E402
    encoder_semaine, decoder_semaine, slot_valide, HoraireInvalide,
    HORAIRE_24_7, SLOT_DEFAUT, SLOT_MAX, FERME,
)


@suite.test("horaire 24/7 -> les 56 caracteres attendus")
def _():
    horaire = {j: "00:00-23:59" for j in
               ["sunday", "monday", "tuesday", "wednesday",
                "thursday", "friday", "saturday"]}
    egal(encoder_semaine(horaire), HORAIRE_24_7, "le 24/7 doit etre canonique")
    egal(len(HORAIRE_24_7), 56, "un calendrier fait 56 caracteres")


@suite.test("un jour absent est FERME, pas ouvert")
def _():
    # Le piege qui compte : si l'absence se traduisait en 00:00-23:59, une
    # activite du samedi ouvrirait la porte les sept jours.
    chaine = encoder_semaine({"saturday": "08:00-12:00"})
    egal(len(chaine), 56, "56 caracteres quoi qu'il arrive")
    egal(chaine[:48], "00000000" * 6, "les six premiers jours doivent etre fermes")
    egal(chaine[48:], "08001200", "le samedi est le 7e bloc")


@suite.test("dimanche est le PREMIER bloc")
def _():
    # Verifie contre une configuration relevee a la main sur la 192.168.2.230 :
    # calendrier 2, mardi 08:14-11:59, les six autres jours a 24/7.
    horaire = {j: "00:00-23:59" for j in
               ["sunday", "monday", "wednesday", "thursday", "friday", "saturday"]}
    horaire["tuesday"] = "08:14-11:59"
    chaine = encoder_semaine(horaire)
    egal(chaine,
         "00002359" "00002359" "08141159" "00002359"
         "00002359" "00002359" "00002359",
         "le mardi doit tomber sur le 3e bloc (dimanche en premier)")


@suite.test("aller-retour encodage / decodage")
def _():
    horaire = {"monday": "06:00-12:00", "friday": "16:30-22:45"}
    relu = decoder_semaine(encoder_semaine(horaire))
    egal(relu["monday"], "06:00-12:00", "lundi conserve")
    egal(relu["friday"], "16:30-22:45", "vendredi conserve")
    egal(relu["sunday"], None, "un jour non cite se relit ferme")


@suite.test("horaire malforme -> le jour se ferme, la chaine reste bien formee")
def _():
    # Ce test exigeait autrefois une exception. Il a change avec le correctif du
    # 2026-08-26 : lever faisait rejeter la SEMAINE entiere, et appliquer_timezone
    # retombait alors sur le creneau par defaut, c'est-a-dire 24h/24. Fermer le
    # jour fautif est le repli sur ; ce qui doit rester vrai, c'est qu'on n'ecrit
    # jamais une chaine bancale sur la pointeuse.
    for mauvais in [{"monday": "06:00"}, {"monday": "6h-12h"},
                    {"monday": "25:00-26:00"}, {"monday": "06:00-1200"}]:
        chaine = encoder_semaine(mauvais)
        egal(len(chaine), 56, "56 caracteres quoi qu'il arrive (%r)" % mauvais)
        egal(chaine, FERME * 7, "tous les jours fermes pour %r" % mauvais)


@suite.test("un horaire qui n'est pas un dict reste une erreur franche")
def _():
    # La tolerance porte sur les JOURS, pas sur la forme du message : recevoir
    # autre chose qu'un dict signale un bug de serialisation, pas une saisie.
    for mauvais in ["06:00-12:00", ["lundi"], 42]:
        try:
            encoder_semaine(mauvais)
            raise AssertionError("aurait du lever pour %r" % (mauvais,))
        except HoraireInvalide:
            pass


@suite.test("un jour ILLISIBLE est ferme, et ne fait PAS tomber la semaine")
def _():
    # La charge utile REELLE du front : il ecrivait le libelle traduit pour un
    # jour ferme, pas null. Un seul de ces jours faisait rejeter tout l'horaire,
    # et appliquer_timezone retombait alors sur le creneau par defaut — donc
    # 24h/24. Un gerant qui posait une restriction obtenait une porte ouverte.
    horaire = {"sunday": "Pas de timezone", "monday": "Pas de timezone",
               "tuesday": "No Timezone", "wednesday": "09:15-23:58",
               "thursday": "Pas de timezone", "friday": "n'importe quoi",
               "saturday": "Pas de timezone"}
    relu = decoder_semaine(encoder_semaine(horaire))
    egal(relu["wednesday"], "09:15-23:58", "le jour lisible est conserve")
    egal(relu["monday"], None, "un libelle traduit vaut ferme")
    egal(relu["friday"], None, "toute valeur illisible vaut ferme")
    verifier(encoder_semaine(horaire) != HORAIRE_24_7,
             "et surtout : la semaine ne devient JAMAIS un 24/7 par accident")


@suite.test("C3 : meme regle, un champ illisible ferme le jour")
def _():
    from services.c3_timezone import encoder_timezone
    horaire = {"monday": "Pas de timezone", "wednesday": "09:15-23:58"}
    champs = dict(c.split("=") for c in encoder_timezone(3, horaire).split("	"))
    egal(champs["WedTime1"], "9152358", "mercredi encode")
    egal(champs["MonTime1"], "0", "lundi ferme, sans faire echouer la ligne")


@suite.test("slot_valide borne a 1..10 (10 combinaisons sur l'appareil)")
def _():
    egal(slot_valide(1), SLOT_DEFAUT, "le creneau 1 est le defaut")
    egal(slot_valide("5"), 5, "une chaine numerique est acceptee")
    egal(slot_valide(SLOT_MAX), 10, "le creneau 10 est le dernier utilisable")
    for hors in [0, 11, -1, None, "", "abc"]:
        egal(slot_valide(hors), None, "%r doit etre rejete" % (hors,))


# ─── Encodage C3 (services/c3_timezone.py) ───────────────────────────────
#
# Le C3 code une plage sur UN entier : débutHHMM × 10000 + finHHMM, 0 = fermé.
# Relevé en écriture/relecture sur le panneau 192.168.1.205 le 2026-08-25.

from services.c3_timezone import (  # noqa: E402
    encoder_intervalle, encoder_timezone,
)


@suite.test("C3 : une plage tient dans un entier debutHHMM*10000+finHHMM")
def _():
    egal(encoder_intervalle("06:00-12:00"), 6001200, "06:00-12:00")
    egal(encoder_intervalle("07:30-11:00"), 7301100, "07:30-11:00")
    # 00:00-23:59 donne 2359 : c'est la valeur d'usine du calendrier 1, ce qui
    # confirme la formule sur une donnee qu'on n'a pas ecrite nous-memes.
    egal(encoder_intervalle("00:00-23:59"), 2359, "le 24/7 doit valoir 2359")


@suite.test("C3 : jour absent, vide ou None -> 0 (ferme)")
def _():
    for rien in (None, "", "   "):
        egal(encoder_intervalle(rien), 0, "%r doit donner 0" % (rien,))


@suite.test("C3 : les 21 champs de la semaine sont TOUS ecrits, zeros compris")
def _():
    # SetDeviceData met a jour champ par champ : un champ omis garde sa valeur
    # precedente. Retrecir un horaire laisserait sinon l'ancien creneau ouvert.
    champs = dict(c.split("=") for c in encoder_timezone(7, {"monday": "06:00-12:00"}).split("\t"))
    egal(champs["TimezoneId"], "7", "l'identifiant de creneau")
    egal(len(champs), 22, "TimezoneId + 7 jours x 3 intervalles")
    egal(champs["MonTime1"], "6001200", "lundi")
    egal(champs["TueTime1"], "0", "un jour non cite est ferme")
    egal(champs["MonTime2"], "0", "les intervalles 2 et 3 sont remis a zero")
    verifier(not any(c.startswith("Hol") for c in champs),
             "les jours feries ne doivent pas etre ecrits : YoGym ne les gere pas")


@suite.test("C3 : horaire malforme -> HoraireInvalide")
def _():
    for mauvais in ["06:00", "6h-12h", "25:00-26:00"]:
        try:
            encoder_intervalle(mauvais)
            raise AssertionError("aurait du lever pour %r" % mauvais)
        except HoraireInvalide:
            pass


if __name__ == "__main__":
    sys.exit(0 if suite.executer() else 1)
