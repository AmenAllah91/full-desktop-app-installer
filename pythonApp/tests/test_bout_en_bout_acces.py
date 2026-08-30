# -*- coding: utf-8 -*-
"""Recette BOUT EN BOUT des créneaux horaires et du choix des pointeuses.

Parcours réel d'un gérant : il crée un horaire, une activité, vend un
abonnement — et la porte s'ouvre au bon moment, sur la bonne machine.

    venv/Scripts/python.exe tests/test_bout_en_bout_acces.py            (phase 1)
    venv/Scripts/python.exe tests/test_bout_en_bout_acces.py --machines (phase 2)
    venv/Scripts/python.exe tests/test_bout_en_bout_acces.py --menage   (nettoyage)

DEUX PHASES, et ce n'est pas un confort : **un C3 ne délivre qu'une session à
la fois**. Tant que le pont tourne il la détient, et l'ouvrir depuis ici ferait
tomber les deux. La phase 1 travaille donc pont allumé (REST, Kafka, journal,
et la standalone qui tolère plusieurs sessions) ; la phase 2 lit les deux
appareils **pont arrêté**.

Conditions :
  - gym-management sur :8081, le pont sur :9998, le front peu importe
  - standalone 192.168.2.230 ET C3 192.168.1.205 allumés
  - la branche 1003 porte les deux, actives (voir --preparer du README)

Non destructif : tout ce qui est créé est supprimé par --menage, qui restitue
aussi l'état des adhérents d'essai. Les 6 adhérents historiques de la .230 et
la configuration manuelle du groupe 2 ne sont jamais touchés.

Codes de sortie : 0 tout passe, 1 échec, 2 conditions non réunies.
"""
import io
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harnais import Suite, verifier, egal, ROUGE, VERT, JAUNE, GRAS, RAZ  # noqa: E402

BACK = os.environ.get("GYM_BASE_URL", "http://localhost:8081")
PONT = "http://localhost:9998"
KC = "https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token"
BROKER = os.environ.get("KAFKA_BROKER", "54.38.35.221:9094")
TOPIC = "new_access_request_empire"

BRANCHE = 1003
STANDALONE = {"id": 2044, "ip": "192.168.2.230", "comkey": 123456}
C3 = {"id": 2043, "ip": "192.168.1.205"}
INACTIVE = 2033          # même IP que la standalone, mais statut Inactive

ADH_TZ = 3057            # partie 1 : horaire restreint, toutes les machines
ADH_MACHINE = 3052       # partie 2 : une seule machine
ADH_DEFAUT = 3051        # non-régression : horaire par défaut

PREFIXE = "E2E"          # tout ce que le script crée porte ce préfixe
ETAT = os.path.join(RACINE, "tests", "etat-bout-en-bout.json")

JOURS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
HORAIRE_MATIN = {"monday": "06:00-12:00", "tuesday": "06:00-12:00",
                 "wednesday": "06:00-12:00", "thursday": "06:00-12:00",
                 "friday": "06:00-12:00"}          # week-end fermé
HORAIRE_SOIR = {"monday": "17:00-22:00", "tuesday": "17:00-22:00"}
HORAIRE_ELARGI = {"monday": "05:30-13:00", "saturday": "08:00-12:00"}

DELAI = 90               # secondes accordées à Kafka + file de tâches

import requests  # noqa: E402

CTX = {}


# ─── Outils ──────────────────────────────────────────────────────────────

def joignable(hote, port, delai=3.0):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((hote, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def jeton():
    r = requests.post(KC, data={"grant_type": "password", "client_id": "front-app",
                                "username": "amktest2", "password": "amktest2"}, timeout=25)
    r.raise_for_status()
    return r.json()["access_token"]


def api(methode, chemin, **kw):
    kw.setdefault("timeout", 60)
    kw.setdefault("headers", {})["Authorization"] = "Bearer " + CTX["jeton"]
    return requests.request(methode, BACK + chemin, **kw)


def etat_charger():
    if os.path.exists(ETAT):
        return json.load(io.open(ETAT, encoding="utf-8"))
    return {"timezones": [], "activites": [], "abonnements": [], "creneaux": []}


def etat_noter(cle, valeur):
    e = etat_charger()
    e.setdefault(cle, [])
    if valeur not in e[cle]:
        e[cle].append(valeur)
    io.open(ETAT, "w", encoding="utf-8").write(json.dumps(e, indent=2))


# ─── Kafka ───────────────────────────────────────────────────────────────

def capter(action, duree=15):
    """Exécute `action` en écoutant le topic, et rend les messages publiés."""
    from confluent_kafka import Consumer
    recus, stop = [], threading.Event()

    def ecouter():
        c = Consumer({"bootstrap.servers": BROKER,
                      "group.id": "e2e-%d" % int(time.time() * 1000),
                      "auto.offset.reset": "latest"})
        c.subscribe([TOPIC])
        fin = time.time() + duree + 25
        while not stop.is_set() and time.time() < fin:
            m = c.poll(1.0)
            if m and not m.error():
                try:
                    recus.append(json.loads(m.value().decode("utf-8")))
                except Exception:
                    pass
        c.close()

    th = threading.Thread(target=ecouter, daemon=True)
    th.start()
    time.sleep(8)                      # laisse le consommateur rejoindre
    resultat = action()
    time.sleep(duree)
    stop.set()
    th.join(timeout=10)
    return resultat, recus


def message_de(msgs, branche=BRANCHE, pin=None, operation=None):
    for m in msgs:
        if str(m.get("gymBranchId")) != str(branche):
            continue
        if pin is not None and str(m.get("userPin")) != str(pin):
            continue
        if operation is not None and m.get("operation") != operation:
            continue
        return m
    return None


# ─── Lecture des pointeuses ──────────────────────────────────────────────

def zkem():
    import win32com.client
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(STANDALONE["comkey"])
    if not z.Connect_Net(STANDALONE["ip"], 4370):
        return None
    return z


def etat_standalone(pin):
    """Groupe, calendriers du groupe et validité d'un PIN sur la standalone."""
    from services.zkem_timezone import decoder_semaine
    z = zkem()
    if not z:
        return None
    try:
        ok, nom, mdp, priv, actif = z.SSR_GetUserInfo(1, str(pin))
        if not ok:
            return {"present": False}
        groupe = z.GetUserGroup(1, int(pin), 0)[1]
        okg, tz1, tz2, tz3, _, _ = z.SSR_GetGroupTZ(1, groupe, 0, 0, 0, 0, 0)
        cal = decoder_semaine(z.GetTZInfo(1, tz1, "")[1]) if (okg and tz1) else None
        okv, _, _, debut, fin = z.GetUserValidDate(1, str(pin))
        combos = [c for c in range(1, 11)
                  if [g for g in z.SSR_GetUnLockGroup(1, c, 0, 0, 0, 0, 0)[1:] if g] == [groupe]]
        return {"present": True, "nom": nom, "groupe": groupe, "calendrier": tz1,
                "horaire": cal, "validite": (debut, fin) if okv else None,
                "combinaisons": combos}
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


def etat_c3(pin):
    """Fiche user et autorisations d'un PIN sur le C3. EXIGE le pont arrêté."""
    import ctypes
    import logging
    logging.getLogger().setLevel(logging.ERROR)
    from services.adapters import PlcommAdapter, plcommpro
    from domain.AccessMachine import AccessMachine

    m = AccessMachine(id=C3["id"], alias="C3", addresseip=C3["ip"], port=4370,
                      statut="Active", type="C3", door1=1, door2=0, door3=0,
                      door4=0, porte_type=None, comKey=0)
    a = PlcommAdapter(m)
    if not a.connect():
        return None
    try:
        def lire(table, filtre=b""):
            buf = ctypes.create_string_buffer(262144)
            ret = plcommpro.GetDeviceData(a.handle, buf, 262144, table, b"*", filtre, b"")
            if ret < 0:
                return ""
            return buf.value.decode("utf-8", errors="ignore").strip()

        fiches = lire(b"user", ("Pin=%s\t" % pin).encode()).splitlines()[1:]
        autos = [l.split(",") for l in
                 lire(b"userauthorize", ("Pin=%s\t" % pin).encode()).splitlines()[1:]]
        cals = {}
        for l in lire(b"timezone").splitlines()[1:]:
            champs = l.split(",")
            cals[champs[0]] = champs
        return {"present": bool(fiches), "fiche": fiches[0] if fiches else None,
                "autorisations": autos, "calendriers": cals}
    finally:
        a.disconnect()


def purger_reliquats():
    """Efface ce qu'une execution interrompue aurait laisse.

    Sans ca, la creation echoue sur TIMEZONE_NAME_EXCEPTION et le rouge
    ressemble a une regression alors que c'est un reliquat.
    """
    import pymysql
    pw = None
    chemin = os.path.join(RACINE, "..", "..", ".env.bench")
    if os.path.exists(chemin):
        for l in io.open(chemin, encoding="utf-8"):
            if l.startswith("BENCH_DB_PASSWORD="):
                pw = l.split("=", 1)[1].strip()
    if pw:
        conn = pymysql.connect(host="54.38.35.221", user="root", password=pw,
                               database="gymapp_empire", charset="utf8mb4",
                               autocommit=True)
        cur = conn.cursor()
        for adh in (ADH_TZ, ADH_MACHINE, ADH_DEFAUT):
            cur.execute("DELETE FROM paie WHERE idAbonnement IN "
                        "(SELECT id FROM abonnement WHERE idAdherent=%s AND prixtot=10)", (adh,))
            cur.execute("DELETE FROM abonnement WHERE idAdherent=%s AND prixtot=10", (adh,))
        conn.close()

    r = api("GET", "/api/activites")
    if r.status_code == 200:
        for a in r.json():
            if (a.get("alias") or "").startswith(PREFIXE):
                api("DELETE", "/api/activites/%s" % a["id"])
    r = api("GET", "/api/timezones/all")
    if r.status_code == 200:
        for z in r.json():
            if (z.get("name") or "").startswith(PREFIXE):
                api("DELETE", "/api/timezones/%s" % z["id"])
    if os.path.exists(ETAT):
        os.remove(ETAT)


def groupes_des_tiers():
    """Groupe de chaque adherent DEJA present sur la standalone.

    Sert d'instantane avant/apres : la recette ne doit deplacer personne
    d'autre que ses propres cobayes.
    """
    z = zkem()
    if not z:
        return None
    try:
        z.ReadAllUserID(1)
        groupes = {}
        while True:
            r = z.SSR_GetAllUserInfo(1)
            if not r or not r[0]:
                break
            pin = str(r[1])
            if pin in (str(ADH_TZ), str(ADH_MACHINE), str(ADH_DEFAUT)):
                continue
            try:
                groupes[pin] = z.GetUserGroup(1, int(pin), 0)[1]
            except Exception:
                pass
        return groupes
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


def attendre(predicat, delai=DELAI, pas=5):
    fin = time.time() + delai
    dernier = None
    while time.time() < fin:
        dernier = predicat()
        if dernier:
            return dernier, True
        time.sleep(pas)
    return dernier, False


# ─── Création par l'API, comme le ferait le front ────────────────────────

def creer_timezone(nom, horaire):
    corps = {"name": nom, "description": "recette bout en bout"}
    for j in JOURS:
        corps[j] = horaire.get(j)
    r = api("POST", "/api/timezones", json=corps)
    r.raise_for_status()
    tz = r.json()
    etat_noter("timezones", tz["id"])
    return tz


def creer_activite(alias, timezone_id, machine_ids):
    corps = {"alias": alias, "description": "recette bout en bout",
             "prixMoix": 10, "prixSeance": 0, "couleur": "#336699", "col": None,
             "active": "oui", "prixtot": 0, "periode": 0, "ptfid": 0,
             "maxremise": 0, "imagePath": "",
             "timezoneId": timezone_id,
             "gymBranchIds": [BRANCHE],
             "accessMachineIds": machine_ids}
    r = api("POST", "/api/activites", json=corps)
    r.raise_for_status()
    act = r.json()
    etat_noter("activites", act["id"])
    return act


def creer_abonnement(adherent, activite):
    debut = datetime.now()
    fin = debut + timedelta(days=30)
    fmt = "%Y-%m-%dT%H:%M:%S"
    corps = {
        "abonnement": {
            "dateDebut": debut.strftime(fmt), "dateFin": fin.strftime(fmt),
            "nombreMois": 1, "validite": "Actif",
            "date_expiration": fin.strftime(fmt),
            "idAdherent": adherent, "idActivite": activite,
            "dateInscription": debut.strftime(fmt),
            "remise": 0, "prixtot": 10, "reste": 0, "gymBranchId": BRANCHE
        },
        "paie": {"montant": 10, "reste": 0, "type": "espece",
                 "typepaiement": "espece", "gymBranchId": BRANCHE,
                 "datePaiement": debut.strftime(fmt)},
        "isNewAbonnement": True, "gymBranchId": BRANCHE, "remise": 0
    }
    return api("POST", "/api/abonnements", json=corps)


# ═════════════════════════════════════════════════════════════════════════
# PHASE 1 — pont allumé : REST, Kafka, journal, standalone
# ═════════════════════════════════════════════════════════════════════════

suite = Suite("RECETTE BOUT EN BOUT — creneaux horaires et choix des pointeuses")


def controle_prealable(machines_directes=False):
    manquants = []
    print("%sControle prealable%s" % (GRAS, RAZ))
    print("-" * 76)
    for (h, p), quoi in ((("localhost", 8081), "gym-management"),
                         ((STANDALONE["ip"], 4370), "standalone " + STANDALONE["ip"]),
                         ((C3["ip"], 4370), "C3 " + C3["ip"])):
        if joignable(h, p):
            print("  %sok%s    %s" % (VERT, RAZ, quoi))
        else:
            print("  %sECHEC%s %s injoignable" % (ROUGE, RAZ, quoi))
            manquants.append(quoi)

    pont_la = joignable("localhost", 9998)
    if machines_directes:
        # Le C3 ne donne qu'une session : le pont doit avoir lache la sienne.
        if pont_la:
            print("  %sECHEC%s le pont tourne encore — il detient la session du C3"
                  % (ROUGE, RAZ))
            manquants.append("pont a arreter pour la phase --machines")
        else:
            print("  %sok%s    pont arrete (session C3 libre)" % (VERT, RAZ))
    else:
        if pont_la:
            print("  %sok%s    le pont repond sur 9998" % (VERT, RAZ))
        else:
            print("  %sECHEC%s le pont ne repond pas sur 9998" % (ROUGE, RAZ))
            manquants.append("pont")

    if manquants:
        print("\n%sConditions non reunies — aucun test execute%s" % (ROUGE, RAZ))
        for m in manquants:
            print("  - " + m)
        sys.exit(2)
    print()


@suite.test("PREPA — la branche 1003 porte bien deux pointeuses actives et une inactive")
def _():
    CTX["jeton"] = jeton()
    purger_reliquats()
    CTX["tiers_avant"] = groupes_des_tiers()
    print("     %s adherent(s) tiers releve(s) sur la standalone"
          % (len(CTX["tiers_avant"]) if CTX["tiers_avant"] else 0))
    r = api("GET", "/api/accessMachines/ByGymBranch/%d" % BRANCHE)
    verifier(r.status_code == 200, "lecture des machines de la branche (%s)" % r.status_code)
    par_id = {m["id"]: m for m in r.json()}
    egal(par_id.get(STANDALONE["id"], {}).get("statut"), "Active", "la standalone est active")
    egal(par_id.get(C3["id"], {}).get("statut"), "Active", "le C3 est actif")
    egal(par_id.get(C3["id"], {}).get("type"), "C3", "le C3 est bien typé C3")
    egal(par_id.get(INACTIVE, {}).get("statut"), "Inactive",
         "la troisième fiche reste inactive — elle sert de cas limite")

    d = requests.get(PONT + "/api/devices", timeout=15).json()["devices"]
    vues = {m["id"]: m for m in d}
    verifier(STANDALONE["id"] in vues and C3["id"] in vues,
             "le pont voit les deux machines actives — vu : %s" % sorted(vues))
    verifier(INACTIVE not in vues, "et ignore l'inactive")
    verifier(all(m["connected"] for m in d), "les deux sont connectées")


# ─── Partie 1 : timezone ─────────────────────────────────────────────────

@suite.test("P1 — creation d'une timezone : le back attribue un creneau, jamais le 1")
def _():
    tz = creer_timezone(PREFIXE + " matin", HORAIRE_MATIN)
    CTX["tz"] = tz
    verifier(tz.get("slot") is not None, "un créneau est attribué")
    verifier(tz["slot"] >= 2, "et ce n'est pas celui du défaut (reçu %s)" % tz["slot"])
    etat_noter("creneaux", tz["slot"])
    egal(tz["monday"], "06:00-12:00", "les horaires sont conservés")
    verifier(not tz.get("saturday"), "le samedi reste fermé")
    print("     créneau %s" % tz["slot"])


@suite.test("P1 — activite sans machine choisie, puis abonnement : give access part")
def _():
    act = creer_activite(PREFIXE + " activite TZ", CTX["tz"]["id"], [])
    CTX["act_tz"] = act
    egal(act["timezoneSlot"], CTX["tz"]["slot"], "l'activité porte le créneau")
    egal(sorted(act.get("gymBranchIds") or []), [BRANCHE], "et la branche d'essai")
    egal(list(act.get("accessMachineIds") or []), [],
         "aucune machine désignée — donc toutes celles de la branche")

    r, msgs = capter(lambda: creer_abonnement(ADH_TZ, act["id"]))
    verifier(r.status_code in (200, 201), "abonnement créé (%s) %s"
             % (r.status_code, r.text[:150]))
    CTX["msgs_p1"] = msgs
    m = message_de(msgs, pin=ADH_TZ, operation="ADD_USER")
    verifier(m is not None, "un ADD_USER a été publié pour l'adhérent %d — vu : %s"
             % (ADH_TZ, [x.get("operation") for x in msgs]))
    CTX["msg_p1"] = m


@suite.test("P1 — le message porte le creneau, l'horaire complet, et LES DEUX machines")
def _():
    m = CTX.get("msg_p1")
    if not m:
        raise AssertionError("aucun message capté à l'étape précédente")
    egal(m.get("timezoneSlot"), CTX["tz"]["slot"], "le créneau du message")
    egal(m.get("timezoneName"), CTX["tz"]["name"], "le nom, pour les journaux")
    h = m.get("weeklySchedule") or {}
    egal(list(h.keys()), JOURS, "les sept jours, dimanche en premier")
    egal(h.get("monday"), "06:00-12:00", "lundi")
    egal(h.get("saturday"), None, "samedi fermé")
    ids = sorted(x["id"] for x in (m.get("machines") or []))
    egal(ids, sorted([STANDALONE["id"], C3["id"]]),
         "les deux machines actives sont servies")
    verifier(INACTIVE not in ids, "la machine Inactive n'y est pas")


@suite.test("P1 — la STANDALONE porte le creneau, le groupe, la combinaison et la validite")
def _():
    slot = CTX["tz"]["slot"]
    etat, arrive = attendre(
        lambda: (lambda e: e if e and e.get("groupe") == slot else None)(etat_standalone(ADH_TZ)))
    verifier(arrive, "l'adhérent est dans le groupe %s en moins de %ss — lu : %s"
             % (slot, DELAI, etat))
    if not arrive:
        return
    CTX["standalone_p1"] = etat
    egal(etat["calendrier"], slot, "le groupe pointe sur son calendrier")
    egal(etat["horaire"].get("monday"), "06:00-12:00", "lundi sur la machine")
    egal(etat["horaire"].get("saturday"), None, "samedi fermé sur la machine")
    verifier(slot in etat["combinaisons"],
             "le groupe ouvre SEUL dans une combinaison — sans ça la porte reste "
             "fermée quel que soit le calendrier (lu : %s)" % etat["combinaisons"])
    verifier(etat["validite"] is not None, "une période de validité est posée")


# ─── Partie 2 : machines specifiques ─────────────────────────────────────

@suite.test("P2 — activite avec UNE SEULE machine sur les deux")
def _():
    act = creer_activite(PREFIXE + " activite machine", CTX["tz"]["id"],
                         [STANDALONE["id"]])
    CTX["act_machine"] = act
    egal(sorted(act.get("accessMachineIds") or []), [STANDALONE["id"]],
         "seule la standalone est désignée")

    r, msgs = capter(lambda: creer_abonnement(ADH_MACHINE, act["id"]))
    verifier(r.status_code in (200, 201), "abonnement créé (%s) %s"
             % (r.status_code, r.text[:150]))
    m = message_de(msgs, pin=ADH_MACHINE, operation="ADD_USER")
    verifier(m is not None, "un ADD_USER a été publié pour l'adhérent %d" % ADH_MACHINE)
    CTX["msg_p2"] = m
    if m:
        ids = sorted(x["id"] for x in (m.get("machines") or []))
        egal(ids, [STANDALONE["id"]],
             "UNE SEULE machine dans le message — c'est tout l'objet de la partie 2")


@suite.test("P2 — l'adherent est configure sur la standalone")
def _():
    slot = CTX["tz"]["slot"]
    etat, arrive = attendre(
        lambda: (lambda e: e if e and e.get("groupe") == slot else None)(etat_standalone(ADH_MACHINE)))
    verifier(arrive, "l'adhérent %d est dans le groupe %s — lu : %s"
             % (ADH_MACHINE, slot, etat))


# ─── Cas limites et non-regression ───────────────────────────────────────

@suite.test("LIMITE — deux timezones differentes pour un meme adherent : REFUSE")
def _():
    tz2 = creer_timezone(PREFIXE + " soir", HORAIRE_SOIR)
    CTX["tz2"] = tz2
    act2 = creer_activite(PREFIXE + " activite soir", tz2["id"], [])
    CTX["act_soir"] = act2

    r = creer_abonnement(ADH_TZ, act2["id"])
    egal(r.status_code, 400, "refus en 400 — reçu %s %s" % (r.status_code, r.text[:200]))
    verifier("horaire" in r.text.lower() or "activite" in r.text.lower(),
             "avec un message qui nomme le conflit — reçu : %s" % r.text[:200])
    print("     %s" % r.json().get("error", r.text[:160]))


@suite.test("LIMITE — la MEME timezone, elle, est acceptee")
def _():
    r = creer_abonnement(ADH_TZ, CTX["act_machine"]["id"])
    verifier(r.status_code in (200, 201),
             "deux activités au même horaire se cumulent (%s) %s"
             % (r.status_code, r.text[:150]))


@suite.test("NON-REGRESSION — horaire par defaut : groupe 1, aucun calendrier ecrit")
def _():
    r = api("GET", "/api/timezones/all")
    defaut = [z for z in r.json() if z.get("slot") == 1][0]
    act = creer_activite(PREFIXE + " activite defaut", defaut["id"], [])
    CTX["act_defaut"] = act

    avant = etat_standalone(ADH_DEFAUT)
    r, msgs = capter(lambda: creer_abonnement(ADH_DEFAUT, act["id"]))
    verifier(r.status_code in (200, 201), "abonnement créé (%s)" % r.status_code)
    m = message_de(msgs, pin=ADH_DEFAUT, operation="ADD_USER")
    if m:
        egal(m.get("timezoneSlot"), 1, "le message porte le créneau par défaut")

    etat, arrive = attendre(
        lambda: (lambda e: e if e and e.get("present") else None)(etat_standalone(ADH_DEFAUT)),
        delai=60)
    verifier(arrive, "l'adhérent est créé sur la machine — lu : %s" % etat)
    if arrive:
        egal(etat["groupe"], 1,
             "il reste dans le groupe 1 — comportement d'avant la fonctionnalité")


@suite.test("NON-REGRESSION — modifier la timezone n'a deplace personne")
def _():
    tz = CTX["tz"]
    corps = dict(tz)
    for j in JOURS:
        corps[j] = HORAIRE_ELARGI.get(j)
    r = api("PUT", "/api/timezones/%s" % tz["id"], json=corps)
    verifier(r.status_code == 200, "modification acceptée (%s) %s"
             % (r.status_code, r.text[:150]))

    etat, arrive = attendre(
        lambda: (lambda e: e if e and e.get("horaire", {}).get("monday") == "05:30-13:00" else None)(
            etat_standalone(ADH_TZ)))
    verifier(arrive, "le nouvel horaire est arrivé sur la machine — lu : %s" % etat)
    if arrive:
        egal(etat["groupe"], tz["slot"],
             "et l'adhérent n'a PAS changé de groupe — aucun accès repoussé")
        egal(etat["horaire"].get("saturday"), "08:00-12:00", "le samedi s'est ouvert")


@suite.test("NON-REGRESSION — le defaut reste verrouille")
def _():
    r = api("GET", "/api/timezones/all")
    d = [z for z in r.json() if z.get("slot") == 1][0]
    r = api("PUT", "/api/timezones/%s" % d["id"], json=dict(d, monday="09:00-10:00"))
    egal(r.status_code, 400, "modification refusée")
    verifier("LOCKED" in r.text, "avec TIMEZONE_DEFAULT_LOCKED — reçu %s" % r.text[:120])
    r = api("DELETE", "/api/timezones/%s" % d["id"])
    egal(r.status_code, 400, "suppression refusée")
    verifier("UNDELETABLE" in r.text,
             "avec TIMEZONE_DEFAULT_UNDELETABLE — reçu %s" % r.text[:120])


@suite.test("NON-REGRESSION — aucune tache abandonnee pendant le parcours")
def _():
    r = requests.get(PONT + "/api/test/queue", timeout=20)
    if r.status_code != 200:
        print("       (route /tasks indisponible : %s)" % r.status_code)
        return "ignore"
    taches = r.json().get("sqlite_tasks", [])
    echouees = [t for t in taches if t.get("status") not in ("COMPLETED", "PENDING")]
    verifier(not echouees, "aucune tâche en échec dans la file — vu : %s" % echouees[:3])


@suite.test("NON-REGRESSION — la recette n'a deplace aucun adherent tiers")
def _():
    """Compare a l'instantane pris AU DEBUT, pas a un etat suppose.

    Premiere version de ce test : « l'adherent 3040 doit rester en groupe 2 »,
    parce que c'est la configuration posee a la main sur l'ecran de la machine.
    C'etait faux — son activite active porte « Night timezone » (creneau 3),
    donc le premier give access le deplace LEGITIMEMENT vers le groupe 3.
    L'invariant reel n'est pas « il ne bouge jamais » mais « CETTE recette ne
    le fait pas bouger ».
    """
    avant = CTX.get("tiers_avant")
    verifier(avant, "un instantané a bien été pris au début du parcours")
    if not avant:
        return
    apres = groupes_des_tiers()
    verifier(apres is not None, "relecture des adhérents tiers")
    if apres is None:
        return
    deplaces = {p: (avant[p], apres[p]) for p in avant
                if p in apres and avant[p] != apres[p]}
    verifier(not deplaces,
             "aucun adhérent tiers n'a change de groupe — bouges : %s" % deplaces)


@suite.test("NON-REGRESSION — le calendrier 2 pose a la main est intact")
def _():
    from services.zkem_timezone import decoder_semaine
    z = zkem()
    verifier(z is not None, "connexion à la standalone")
    if not z:
        return
    try:
        egal(z.SSR_GetGroupTZ(1, 2, 0, 0, 0, 0, 0)[1], 2,
             "le groupe 2 pointe toujours sur le calendrier 2")
        egal(decoder_semaine(z.GetTZInfo(1, 2, "")[1]).get("tuesday"), "08:14-11:59",
             "le calendrier 2 n'a pas bougé — aucun créneau d'essai ne l'a écrasé")
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════
# PHASE 2 — pont arrêté : lecture directe des deux appareils
# ═════════════════════════════════════════════════════════════════════════

suite_machines = Suite("PHASE 2 — lecture directe des pointeuses (pont arrete)")


@suite_machines.test("STANDALONE — l'adherent de la partie 1 a le bon creneau")
def _():
    e = etat_standalone(ADH_TZ)
    verifier(e and e.get("present"), "l'adhérent %d existe sur la machine" % ADH_TZ)
    if not e or not e.get("present"):
        return
    print("     groupe=%s calendrier=%s validite=%s"
          % (e["groupe"], e["calendrier"], e["validite"]))
    verifier(e["groupe"] >= 2, "il est dans un groupe restreint, pas le 1")
    verifier(e["horaire"] and e["horaire"].get("monday"), "son calendrier porte un lundi")
    verifier(e["groupe"] in e["combinaisons"], "son groupe ouvre seul")


@suite_machines.test("C3 — l'adherent de la partie 1 y est autorise, avec le meme creneau")
def _():
    e = etat_c3(ADH_TZ)
    verifier(e is not None, "connexion au C3 (le pont doit être arrêté)")
    if not e:
        return
    CTX["c3_p1"] = e
    verifier(e["present"], "la fiche user existe sur le panneau")
    verifier(e["autorisations"], "des lignes userauthorize existent")
    slots = sorted({l[1] for l in e["autorisations"]})
    print("     autorisations : %s" % e["autorisations"])
    egal(len(slots), 1, "un SEUL créneau — userauthorize ne doit pas accumuler")
    attendu = str((etat_charger().get("creneaux") or ["?"])[-1])
    egal(slots[0], attendu, "et c'est bien le créneau d'essai, pas le 24/7 par défaut")
    cal = e["calendriers"].get(slots[0])
    verifier(cal is not None, "le calendrier correspondant existe dans la table timezone")
    if cal:
        # Colonnes : TimezoneId, Sun1..3, Mon1..3, ... L'entier vaut
        # debutHHMM * 10000 + finHHMM.
        egal(cal[4], "5301300", "lundi = 05:30-13:00 après la modification d'horaire")


@suite_machines.test("C3 — la recette n'a RIEN ecrit pour l'adherent de la partie 2")
def _():
    """Le coeur de la partie 2, formule correctement.

    Premiere version : « la table userauthorize doit etre vide pour ce PIN ».
    Faux — l'adherent existait deja sur ce panneau, avec de vieilles dates et le
    calendrier par defaut. Une fiche vide n'etait jamais l'invariant ; ce qui
    compte est que CE parcours n'ait pas touche le C3, l'activite ne designant
    que la standalone.

    C'est meme une preuve plus forte qu'une table vide : la fiche a garde son
    ancien contenu pendant que la standalone recevait le nouveau creneau.
    """
    creneaux = etat_charger().get("creneaux") or []
    verifier(creneaux, "le creneau d'essai a bien ete transmis par la phase 1")
    if not creneaux:
        return
    slot = str(creneaux[-1])

    e = etat_c3(ADH_MACHINE)
    verifier(e is not None, "connexion au C3")
    if not e:
        return
    print("     fiche=%r" % e["fiche"])
    print("     autorisations=%s (creneau d'essai : %s)" % (e["autorisations"], slot))

    portes_au_creneau = [l for l in e["autorisations"] if l[1] == slot]
    verifier(not portes_au_creneau,
             "aucune autorisation au creneau d'essai %s sur le C3 — l'activite "
             "ne designait que la standalone (lu : %s)" % (slot, e["autorisations"]))

    # Et la fiche user ne porte pas les dates de l'abonnement d'essai.
    if e["fiche"]:
        aujourdhui = datetime.now().strftime("%Y%m%d")
        verifier(aujourdhui not in e["fiche"],
                 "la fiche C3 ne porte pas les dates de l'abonnement d'essai — "
                 "elle est restee telle quelle (lu : %s)" % e["fiche"])


@suite_machines.test("STANDALONE — l'adherent de la partie 2 y est bien, lui")
def _():
    e = etat_standalone(ADH_MACHINE)
    verifier(e and e.get("present"), "présent sur la standalone")
    if e and e.get("present"):
        verifier(e["groupe"] >= 2, "dans un groupe restreint (%s)" % e["groupe"])


# ═════════════════════════════════════════════════════════════════════════

def menage():
    """Supprime tout ce que la recette a créé, dans l'ordre des dépendances."""
    CTX["jeton"] = jeton()
    e = etat_charger()
    print("%sMenage%s" % (GRAS, RAZ))
    print("-" * 76)

    import pymysql
    pw = None
    for l in io.open(os.path.join(RACINE, "..", "..", ".env.bench"), encoding="utf-8"):
        if l.startswith("BENCH_DB_PASSWORD="):
            pw = l.split("=", 1)[1].strip()
    conn = pymysql.connect(host="54.38.35.221", user="root", password=pw,
                           database="gymapp_empire", charset="utf8mb4", autocommit=True)
    cur = conn.cursor()

    for adh in (ADH_TZ, ADH_MACHINE, ADH_DEFAUT):
        cur.execute("DELETE FROM paie WHERE idAbonnement IN "
                    "(SELECT id FROM abonnement WHERE idAdherent=%s AND prixtot=10)", (adh,))
        n = cur.execute("DELETE FROM abonnement WHERE idAdherent=%s AND prixtot=10", (adh,))
        print("  adherent %s : %s abonnement(s) d'essai supprime(s)" % (adh, n))
        cur.execute("UPDATE adherents SET etatAcces='Non actif' WHERE id=%s", (adh,))

    for aid in e["activites"]:
        r = api("DELETE", "/api/activites/%s" % aid)
        print("  activite %s -> %s" % (aid, r.status_code))
    for tid in e["timezones"]:
        r = api("DELETE", "/api/timezones/%s" % tid)
        print("  timezone %s -> %s" % (tid, r.status_code))

    sauvegarde = os.path.join(RACINE, "..", "..", "etat-2043-avant-essai.json")
    if os.path.exists(sauvegarde):
        a = json.load(io.open(sauvegarde, encoding="utf-8"))
        cur.execute("""UPDATE access_machine SET alias=%s, addresseip=%s, statut=%s,
                       type=%s, comKey=%s WHERE id=2043""",
                    (a["alias"], a["addresseip"], a["statut"], a["type"], a["comKey"]))
        print("  fiche 2043 restituee : %s / %s / %s"
              % (a["alias"], a["addresseip"], a["statut"]))

    conn.close()
    if os.path.exists(ETAT):
        os.remove(ETAT)
    print("\n%sMenage termine.%s Les adherents d'essai restent a nettoyer sur les "
          "pointeuses si besoin (PIN %s, %s, %s)."
          % (VERT, RAZ, ADH_TZ, ADH_MACHINE, ADH_DEFAUT))


if __name__ == "__main__":
    import pythoncom
    pythoncom.CoInitialize()

    if "--menage" in sys.argv:
        menage()
        sys.exit(0)

    if "--machines" in sys.argv:
        controle_prealable(machines_directes=True)
        ok = suite_machines.executer()
        sys.exit(0 if ok else 1)

    controle_prealable()
    ok = suite.executer()
    print("\n%sPHASE 2%s : arreter le pont, puis relancer avec --machines" % (GRAS, RAZ))
    print("%sPUIS%s : badger physiquement (voir la fiche imprimee par le script)" % (GRAS, RAZ))
    sys.exit(0 if ok else 1)
