# -*- coding: utf-8 -*-
"""Test de CHAÎNE COMPLÈTE : back → Kafka → pont → pointeuse physique.

C'est le seul test qui prouve l'objectif métier de bout en bout : un gérant
modifie les horaires d'une timezone dans l'application, et le calendrier change
réellement sur la pointeuse, sans qu'aucun accès adhérent ne soit repoussé.

Conditions requises :
  - gym-management démarré en local sur :8081
      ./mvnw.cmd spring-boot:run -Dspring-boot.run.jvmArguments="-Dspring.devtools.restart.enabled=false"
  - le pont démarré sur la branche 1003 (dont la machine 2044 est la .230)
      YOGYM_BASE_URL=https://integration.yo-club.app python main.py empire 1003
  - la standalone 192.168.2.230 allumée

Non destructif : la timezone créée est supprimée, et le créneau qu'elle a
occupé est rendu à son état d'usine (24h/24). La configuration manuelle du
groupe 2 de la machine n'est jamais touchée — l'essai se fait sur un créneau
libre, attribué par le back lui-même.

Codes de sortie : 0 tout passe, 1 échec, 2 conditions non réunies.
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harnais import Suite, verifier, egal, ROUGE, VERT, GRAS, RAZ  # noqa: E402

BACK = os.environ.get("GYM_BASE_URL", "http://localhost:8081")
KC = "https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token"
IP_MACHINE = "192.168.2.230"
PORT = 4370
COMKEY = 123456
PONT = ("127.0.0.1", 9998)

# Ce que le gérant saisit dans l'application.
HORAIRE_INITIAL = {"monday": "06:00-12:00", "tuesday": "06:00-12:00"}
HORAIRE_MODIFIE = {"monday": "07:30-11:00", "saturday": "09:00-13:00"}

DELAI_PROPAGATION = 90  # secondes accordées à la chaîne Kafka + file de tâches

CTX = {}
suite = Suite("CHAINE COMPLETE : back -> Kafka -> pont -> pointeuse")


def _joignable(hote, port, delai=3.0):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((hote, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def controle_prealable():
    manquants = []
    print(f"{GRAS}Contrôle préalable{RAZ}")
    print("-" * 76)
    for (hote, port), quoi in (((BACK.split("//")[1].split(":")[0],
                                 int(BACK.rsplit(":", 1)[1])), "gym-management"),
                               (PONT, "le pont (pythonApp)"),
                               ((IP_MACHINE, PORT), "la standalone " + IP_MACHINE)):
        if _joignable(hote, port):
            print(f"  {VERT}ok{RAZ}    {quoi} répond sur {hote}:{port}")
        else:
            print(f"  {ROUGE}ECHEC{RAZ} {quoi} ne répond pas sur {hote}:{port}")
            manquants.append("%s injoignable (%s:%s)" % (quoi, hote, port))
    if manquants:
        print(f"\n{ROUGE}Conditions non réunies — aucun test exécuté{RAZ}")
        for m in manquants:
            print("  - " + m)
        sys.exit(2)
    print()


def _jeton():
    import requests
    r = requests.post(KC, data={"grant_type": "password", "client_id": "front-app",
                                "username": "amktest2", "password": "amktest2"}, timeout=25)
    r.raise_for_status()
    return r.json()["access_token"]


def _lire_calendrier(slot):
    """Relit un calendrier sur la pointeuse, indépendamment du pont."""
    import win32com.client
    from services.zkem_timezone import decoder_semaine
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(COMKEY)
    if not z.Connect_Net(IP_MACHINE, PORT):
        return None
    try:
        ok, chaine = z.GetTZInfo(1, slot, "")
        return decoder_semaine(chaine) if ok else None
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


def _attendre(slot, jour, attendu, delai=DELAI_PROPAGATION):
    """Attend que la pointeuse porte la valeur voulue, ou rend le dernier état lu."""
    fin = time.time() + delai
    dernier = None
    while time.time() < fin:
        dernier = _lire_calendrier(slot)
        if dernier and dernier.get(jour) == attendu:
            return dernier, True
        time.sleep(5)
    return dernier, False


# ─── Le parcours ─────────────────────────────────────────────────────────

NOM_ESSAI = "TZ chaine complete"


def _purger(h):
    """Supprime un reliquat d'un essai precedent : le test doit etre rejouable.

    Sans ca, une execution interrompue laisse la timezone en place et la
    suivante echoue sur TIMEZONE_NAME_EXCEPTION — un faux rouge qui ressemble a
    une regression.
    """
    import requests
    r = requests.get(BACK + "/api/timezones/all", headers=h, timeout=30)
    if r.status_code != 200:
        return
    for z in r.json():
        if z.get("name") == NOM_ESSAI:
            requests.delete(BACK + "/api/timezones/%s" % z["id"], headers=h, timeout=30)
            print("     reliquat supprime : timezone %s (creneau %s)"
                  % (z["id"], z.get("slot")))


@suite.test("le back crée la timezone et lui attribue un créneau libre")
def _():
    import requests
    h = {"Authorization": "Bearer " + _jeton()}
    CTX["h"] = h
    _purger(h)
    corps = dict({"name": NOM_ESSAI, "description": "essai bout en bout",
                  "sunday": None, "wednesday": None, "thursday": None,
                  "friday": None, "saturday": None}, **HORAIRE_INITIAL)
    r = requests.post(BACK + "/api/timezones", json=corps, headers=h, timeout=30)
    verifier(r.status_code in (200, 201),
             "création acceptée — reçu %s %s" % (r.status_code, r.text[:120]))
    tz = r.json()
    CTX["tz"] = tz
    verifier(tz.get("slot") is not None and tz["slot"] >= 2,
             "un créneau libre a été attribué (%s)" % tz.get("slot"))
    CTX["etat_initial"] = _lire_calendrier(tz["slot"])
    print("     créneau %s, état machine avant : %s"
          % (tz["slot"], CTX["etat_initial"]))


@suite.test("modifier la timezone dans l'application change le calendrier SUR LA POINTEUSE")
def _():
    import requests
    tz, h = CTX["tz"], CTX["h"]
    corps = dict(tz, **HORAIRE_MODIFIE)
    corps["tuesday"] = None          # on retire mardi, il doit se refermer
    r = requests.put(BACK + "/api/timezones/%s" % tz["id"], json=corps,
                     headers=h, timeout=30)
    verifier(r.status_code == 200,
             "modification acceptée — reçu %s %s" % (r.status_code, r.text[:120]))

    jours, arrive = _attendre(tz["slot"], "monday", "07:30-11:00")
    verifier(arrive,
             "le nouvel horaire du lundi est arrivé sur la machine en moins de "
             "%ss — dernier état lu : %s" % (DELAI_PROPAGATION, jours))
    if arrive:
        egal(jours.get("saturday"), "09:00-13:00", "le samedi ouvert est arrivé aussi")
        egal(jours.get("tuesday"), None,
             "le mardi retiré s'est bien refermé — les 7 jours sont réécrits")


@suite.test("aucun adhérent n'a été touché : la config manuelle du groupe 2 est intacte")
def _():
    # Le message UPDATE_TIMEZONE ne porte aucun utilisateur. On le vérifie sur
    # la configuration posée à la main sur l'écran de la machine (groupe 2,
    # mardi 08:14-11:59), qui ne doit pas avoir bougé d'un caractère.
    import win32com.client
    from services.zkem_timezone import decoder_semaine
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(COMKEY)
    verifier(z.Connect_Net(IP_MACHINE, PORT), "connexion à la machine")
    try:
        egal(z.SSR_GetGroupTZ(1, 2, 0, 0, 0, 0, 0)[1], 2,
             "le groupe 2 pointe toujours sur le calendrier 2")
        egal(decoder_semaine(z.GetTZInfo(1, 2, "")[1]).get("tuesday"), "08:14-11:59",
             "le calendrier 2 posé à la main n'a pas bougé")
        egal(z.GetUserGroup(1, 3040, 0)[1], 2,
             "l'adhérent 3040 est resté dans son groupe")
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


@suite.test("ménage : timezone supprimée et créneau rendu à l'état d'usine")
def _():
    import requests
    import win32com.client
    from services.zkem_timezone import HORAIRE_24_7
    tz, h = CTX["tz"], CTX["h"]

    r = requests.delete(BACK + "/api/timezones/%s" % tz["id"], headers=h, timeout=30)
    verifier(r.status_code in (200, 204), "timezone supprimée (%s)" % r.status_code)

    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(COMKEY)
    if z.Connect_Net(IP_MACHINE, PORT):
        try:
            z.EnableDevice(1, False)
            z.SetTZInfo(1, tz["slot"], HORAIRE_24_7)
            z.SSR_SetGroupTZ(1, tz["slot"], 0, 0, 0, 0, 0)
            z.SSR_SetUnLockGroup(1, tz["slot"], 0, 0, 0, 0, 0)
            z.RefreshData(1)
        finally:
            z.EnableDevice(1, True)
            z.Disconnect()
    jours = _lire_calendrier(tz["slot"])
    egal(jours.get("monday"), "00:00-23:59",
         "le créneau %s est rendu ouvert 24h/24" % tz["slot"])


if __name__ == "__main__":
    import pythoncom
    pythoncom.CoInitialize()
    controle_prealable()
    sys.exit(0 if suite.executer() else 1)
