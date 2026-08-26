"""Resynchronisation des créneaux — SUR MACHINE RÉELLE, chaîne complète.

    BANC_USER=... BANC_PASSWORD=... venv\\Scripts\\python.exe tests\\test_resynchronisation.py

Conditions requises, nommées une par une si elles manquent (sortie 2) :

  - gym-management sur localhost:8081
  - le pont démarré (il consomme Kafka et écrit sur les pointeuses)
  - la standalone 192.168.2.230 allumée

Ce que ce parcours prouve, et qu'aucun test hors ligne ne peut prouver :

  le bouton « resynchroniser » de l'écran des créneaux répare réellement une
  pointeuse dont le calendrier a divergé. On fait diverger l'appareil pour de
  bon — écriture SDK directe d'un calendrier faux, exactement l'état dans
  lequel se trouve une pointeuse restée éteinte pendant un changement
  d'horaires — puis on appelle l'endpoint et on regarde l'appareil.

Il vérifie aussi que le pont a bien RETENU l'état voulu, car c'est lui qui
rattrape les machines absentes au-delà de la rétention Kafka. Sans cette
mémoire, la réparation ne marcherait que pour les pointeuses allumées au bon
moment — soit précisément le cas qui n'a jamais posé problème.

Non destructif : le créneau est rendu à la valeur lue au départ, y compris si
un test échoue en cours de route.
"""
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnais import Suite, verifier, egal  # noqa: E402

BACK = os.environ.get("GYM_BASE_URL", "http://localhost:8081")
PONT = "http://localhost:9998"
KC = "https://login-int.yogym.co/realms/empire/protocol/openid-connect/token"
IP = os.environ.get("IP_POINTEUSE", "192.168.2.230")
COMKEY = 123456
DB = os.path.join(os.environ.get("APPDATA", ""), "desktop-app", "task_queue.db")

# Laissé au pont pour consommer Kafka, dépiler et écrire. Mesuré à ~2 s sur le
# banc ; large de côté, car un échec ici serait pris pour une régression.
DELAI_PROPAGATION = 45

JOURS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


# ─── Conditions ──────────────────────────────────────────────────────────

def _joignable(hote, port, delai=3):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((hote, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _conditions():
    """Retourne la liste de ce qui manque. Vide = on peut tester."""
    manques = []
    try:
        import requests
        requests.get(f"{PONT}/health", timeout=5)
    except Exception:
        manques.append(f"le pont ne repond pas sur {PONT}")
    try:
        import requests
        requests.get(f"{BACK}/api/timezones/all", timeout=8)
    except Exception:
        manques.append(f"gym-management ne repond pas sur {BACK}")
    if not _joignable(IP, 4370):
        manques.append(f"la pointeuse {IP} est injoignable (allumee ?)")
    if not os.environ.get("BANC_USER") or not os.environ.get("BANC_PASSWORD"):
        manques.append("BANC_USER / BANC_PASSWORD absents de l'environnement")
    if not os.path.exists(DB):
        manques.append(f"base locale du pont introuvable : {DB}")
    return manques


# ─── Accès ───────────────────────────────────────────────────────────────

def _jeton():
    import requests
    r = requests.post(KC, data={
        "grant_type": "password", "client_id": "front-app",
        "username": os.environ["BANC_USER"],
        "password": os.environ["BANC_PASSWORD"]}, timeout=25)
    r.raise_for_status()
    return r.json()["access_token"]


def _entete(token):
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


def _timezones(token):
    import requests
    r = requests.get(f"{BACK}/api/timezones/all", headers=_entete(token), timeout=20)
    r.raise_for_status()
    return r.json()


class Pointeuse:
    """Lecture/écriture directe des calendriers, sans passer par le pont."""

    def __enter__(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self.z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
        self.z.SetCommPassword(COMKEY)
        if not self.z.Connect_Net(IP, 4370):
            raise RuntimeError(f"connexion impossible a {IP}")
        return self

    def __exit__(self, *_):
        try:
            self.z.Disconnect()
        except Exception:
            pass

    def lire_brut(self, slot):
        return self.z.GetTZInfo(1, slot, "")[1]

    def lire(self, slot):
        from services.zkem_timezone import decoder_semaine
        return {k: v for k, v in decoder_semaine(self.lire_brut(slot)).items() if v}

    def ecrire_brut(self, slot, brut):
        ok = self.z.SetTZInfo(1, slot, brut)
        self.z.RefreshData(1)
        return ok


def _attendre(predicat, delai=DELAI_PROPAGATION, pas=3):
    """Attend qu'une condition observée SUR LA POINTEUSE devienne vraie."""
    limite = time.time() + delai
    dernier = None
    while time.time() < limite:
        with Pointeuse() as p:
            dernier = predicat(p)
        if dernier:
            return dernier
        time.sleep(pas)
    return dernier


# ─── Le parcours ─────────────────────────────────────────────────────────

manques = _conditions()
if manques:
    print("Conditions non reunies — aucun test execute :")
    for m in manques:
        print("  -", m)
    sys.exit(2)

TOKEN = _jeton()
CIBLE = None          # la timezone non-defaut sur laquelle on travaille
BRUT_ORIGINE = None   # son calendrier tel que la pointeuse le portait au depart

for tz in _timezones(TOKEN):
    if tz.get("slot") and tz["slot"] != 1:
        CIBLE = tz
        break

if CIBLE is None:
    print("Conditions non reunies — aucun test execute :")
    print("  - aucune timezone non-defaut sur ce tenant ; en creer une d'abord")
    sys.exit(2)

with Pointeuse() as p:
    BRUT_ORIGINE = p.lire_brut(CIBLE["slot"])

# Un calendrier volontairement faux : tout ferme sauf un lundi absurde. Aucune
# chance de coincider avec l'horaire attendu, donc aucun faux positif.
FAUX = "0000000001000200" + "00000000" * 5


suite = Suite(f"RESYNCHRONISATION — pointeuse {IP}, creneau {CIBLE['slot']} "
              f"({CIBLE.get('name')})")


@suite.test("l'endpoint annonce les creneaux qu'il diffuse")
def _():
    import requests
    r = requests.post(f"{BACK}/api/timezones/resynchroniser",
                      headers=_entete(TOKEN), json={}, timeout=40)
    egal(r.status_code, 200, f"reponse inattendue : {r.text[:200]}")
    corps = r.json()

    attendus = len([t for t in _timezones(TOKEN) if t.get("slot") and t["slot"] != 1])
    egal(corps.get("creneauxDiffuses"), attendus,
         "tous les creneaux non-defaut doivent partir")
    egal(corps.get("echecs"), [], "aucun echec attendu avec Kafka disponible")


@suite.test("le creneau par defaut n'est JAMAIS diffuse")
def _():
    import requests
    corps = requests.post(f"{BACK}/api/timezones/resynchroniser",
                          headers=_entete(TOKEN), json={}, timeout=40).json()
    toutes = _timezones(TOKEN)

    verifier(any(t.get("slot") == 1 for t in toutes),
             "le tenant doit avoir sa timezone par defaut")
    egal(corps.get("creneauxDiffuses"), len(toutes) - 1,
         "le creneau 1 correspond a l'etat d'usine : l'ecrire serait du bruit")


@suite.test("une pointeuse qui a DIVERGE est reparee — le coeur du sujet")
def _():
    attendu = {j: CIBLE.get(j) for j in JOURS if CIBLE.get(j)}

    with Pointeuse() as p:
        p.ecrire_brut(CIBLE["slot"], FAUX)
    with Pointeuse() as p:
        verifier(p.lire(CIBLE["slot"]) != attendu,
                 "la pointeuse doit bien avoir diverge avant qu'on repare")

    import requests
    requests.post(f"{BACK}/api/timezones/resynchroniser",
                  headers=_entete(TOKEN), json={}, timeout=40)

    obtenu = _attendre(lambda p: p.lire(CIBLE["slot"]) == attendu)
    verifier(obtenu, f"la pointeuse doit revenir a {attendu} "
                     f"en moins de {DELAI_PROPAGATION} s")


@suite.test("le pont a RETENU l'etat voulu — ce qui rattrape les machines eteintes")
def _():
    import sqlite3
    conn = sqlite3.connect(DB)
    try:
        lignes = dict(conn.execute(
            "SELECT slot, weekly_schedule FROM timezone_state").fetchall())
    finally:
        conn.close()

    verifier(CIBLE["slot"] in lignes,
             f"le creneau {CIBLE['slot']} doit figurer dans timezone_state : "
             f"sans lui, une pointeuse absente plus de 24 h resterait perimee")

    horaire = json.loads(lignes[CIBLE["slot"]]) if lignes[CIBLE["slot"]] else {}
    attendu = {j: CIBLE.get(j) for j in JOURS if CIBLE.get(j)}
    egal({k: v for k, v in (horaire or {}).items() if v}, attendu,
         "l'etat retenu doit etre celui saisi en base, pas une approximation")


@suite.test("la revision avance a chaque diffusion")
def _():
    import requests
    import sqlite3

    def revision():
        conn = sqlite3.connect(DB)
        try:
            return conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM timezone_state").fetchone()[0]
        finally:
            conn.close()

    avant = revision()
    requests.post(f"{BACK}/api/timezones/resynchroniser",
                  headers=_entete(TOKEN), json={}, timeout=40)

    limite = time.time() + 20
    while time.time() < limite and revision() == avant:
        time.sleep(2)

    verifier(revision() > avant,
             "sans revision qui bouge, une machine deja vue ne serait jamais "
             "reecrite au redemarrage suivant")


if __name__ == "__main__":
    succes = suite.executer()

    # Rendre la pointeuse a son etat de depart, echec ou pas.
    try:
        with Pointeuse() as p:
            p.ecrire_brut(CIBLE["slot"], BRUT_ORIGINE)
        print(f"\nCreneau {CIBLE['slot']} rendu a son etat initial.")
    except Exception as ex:
        print(f"\n/!\\ restauration du creneau {CIBLE['slot']} impossible : {ex}")
        print(f"    valeur d'origine a remettre a la main : {BRUT_ORIGINE!r}")

    sys.exit(0 if succes else 1)
