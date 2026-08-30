# -*- coding: utf-8 -*-
"""Dit si un adherent passerait la porte MAINTENANT, et pourquoi.

    python verdict_badge.py                 (adherent 3040, standalone du banc)
    python verdict_badge.py --pin 3057
    python verdict_badge.py --suivre        (repete jusqu'a ce que le verdict change)

Compare trois choses que rien ne rapproche autrement :

  - ce que le gerant a saisi          -> la base, via l'API
  - ce que la pointeuse porte         -> lecture SDK directe
  - l'heure de la POINTEUSE           -> la seule qui decide, pas celle du poste

Un ecart entre les deux premieres lignes veut dire que la propagation n'est pas
arrivee ; un ecart d'horloge explique un refus qu'on croirait injustifie.
"""
import argparse
import datetime
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BACK = os.environ.get("GYM_BASE_URL", "http://localhost:8081")
KC = "https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token"
IP = os.environ.get("IP_POINTEUSE", "192.168.2.230")
COMKEY = 123456
JOURS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def joignable(hote, port, delai=2.5):
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
    """Jeton du banc. Identifiants par l'environnement — rien en clair ici."""
    import requests
    utilisateur = os.environ.get("BANC_USER")
    motdepasse = os.environ.get("BANC_PASSWORD")
    if not utilisateur or not motdepasse:
        return None                   # on lira la machine, pas la base
    r = requests.post(KC, data={"grant_type": "password", "client_id": "front-app",
                                "username": utilisateur, "password": motdepasse}, timeout=25)
    r.raise_for_status()
    return r.json()["access_token"]


def lire_machine(pin):
    import win32com.client
    from services.zkem_timezone import decoder_semaine
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(COMKEY)
    if not z.Connect_Net(IP, 4370):
        return None
    try:
        ok, an, mo, jo, h, mi, s = z.GetDeviceTime(1, 0, 0, 0, 0, 0, 0)
        maintenant = datetime.datetime(an, mo, jo, h, mi, s)
        groupe = z.GetUserGroup(1, int(pin), 0)[1]
        okg, tz1, _, _, _, _ = z.SSR_GetGroupTZ(1, groupe, 0, 0, 0, 0, 0)
        horaire = decoder_semaine(z.GetTZInfo(1, tz1, "")[1]) if (okg and tz1) else {}
        okv, _, _, debut, fin = z.GetUserValidDate(1, str(pin))
        combos = [c for c in range(1, 11)
                  if [g for g in z.SSR_GetUnLockGroup(1, c, 0, 0, 0, 0, 0)[1:] if g] == [groupe]]
        okp, nom, mdp, privilege, actif = z.SSR_GetUserInfo(1, str(pin))
        return {"maintenant": maintenant, "groupe": groupe, "calendrier": tz1,
                "horaire": horaire, "validite": (debut, fin) if okv else None,
                "combinaisons": combos, "present": okp, "nom": nom, "actif": actif}
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


def verdict(etat):
    """Rend (passe, explication). L'ordre suit celui de la pointeuse."""
    if not etat.get("present"):
        return False, "l'adherent n'existe pas sur cette pointeuse"
    if not etat.get("actif"):
        return False, "la fiche est desactivee sur la pointeuse"

    if etat["validite"]:
        debut, fin = etat["validite"]
        try:
            d = datetime.datetime.strptime(debut, "%Y-%m-%d %H:%M:%S")
            f = datetime.datetime.strptime(fin, "%Y-%m-%d %H:%M:%S")
            if not (d <= etat["maintenant"] <= f):
                return False, "hors periode de validite (%s -> %s)" % (debut, fin)
        except ValueError:
            pass                      # format inattendu : on ne bloque pas dessus

    if etat["groupe"] not in etat["combinaisons"]:
        return False, ("le groupe %s n'ouvre seul dans AUCUNE combinaison — "
                       "la porte reste fermee quel que soit le calendrier"
                       % etat["groupe"])

    jour = JOURS[etat["maintenant"].weekday()]
    plage = etat["horaire"].get(jour)
    if not plage:
        return False, "%s est ferme dans le calendrier %s" % (jour, etat["calendrier"])

    d, f = plage.split("-")
    dh, dm = map(int, d.split(":"))
    fh, fm = map(int, f.split(":"))
    ouverture = etat["maintenant"].replace(hour=dh, minute=dm, second=0)
    fermeture = etat["maintenant"].replace(hour=fh, minute=fm, second=59)

    if etat["maintenant"] < ouverture:
        reste = (ouverture - etat["maintenant"]).total_seconds()
        return False, "ouverture dans %d min %d s (a %s)" % (reste // 60, reste % 60, d)
    if etat["maintenant"] > fermeture:
        return False, "la fenetre %s est passee" % plage
    return True, "dans la fenetre %s" % plage


def afficher(pin, token):
    import requests
    etat = lire_machine(pin)
    if etat is None:
        print("pointeuse %s injoignable" % IP)
        return None

    # Ce que le gerant a saisi, pour reperer une propagation en retard.
    en_base = None
    try:
        if token is None:
            raise RuntimeError("pas de jeton : BANC_USER / BANC_PASSWORD absents")
        r = requests.get(BACK + "/api/timezones/all",
                         headers={"Authorization": "Bearer " + token}, timeout=20)
        if r.status_code == 200:
            for z in r.json():
                if z.get("slot") == etat["calendrier"]:
                    en_base = {j: z.get(j) for j in JOURS if z.get(j)}
                    break
    except Exception:
        pass

    sur_machine = {k: v for k, v in etat["horaire"].items() if v}
    passe, pourquoi = verdict(etat)

    print("pin %s (%r)  groupe %s -> calendrier %s"
          % (pin, etat["nom"], etat["groupe"], etat["calendrier"]))
    print("  saisi en base   : %s" % (en_base if en_base is not None
                                     else "(non lu — back arrete, ou BANC_USER/BANC_PASSWORD absents)"))
    print("  sur la machine  : %s" % sur_machine)
    if en_base is not None and en_base != sur_machine:
        print("  /!\\ ECART : la modification n'est pas encore descendue sur la pointeuse")
    validite = ("%s -> %s" % etat["validite"]) if etat["validite"] else "aucune"
    print("  validite        : %s" % validite)
    print("  horloge MACHINE : %s" % etat["maintenant"].strftime("%Y-%m-%d %H:%M:%S"))
    print("  VERDICT         : %s — %s" % ("PASSE" if passe else "REFUSE", pourquoi))
    return passe


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pin", default="3040")
    p.add_argument("--suivre", action="store_true",
                   help="repete toutes les 15 s jusqu'a ce que le verdict change")
    args = p.parse_args()

    if not joignable(IP, 4370):
        print("pointeuse %s injoignable — allumee ?" % IP, file=sys.stderr)
        return 2

    import pythoncom
    pythoncom.CoInitialize()
    token = jeton()

    premier = afficher(args.pin, token)
    if not args.suivre or premier is None:
        return 0

    import time
    print("\nsuivi... (Ctrl+C pour arreter)")
    while True:
        time.sleep(15)
        print()
        courant = afficher(args.pin, token)
        if courant is not None and courant != premier:
            print("\n>>> LE VERDICT A CHANGE <<<")
            return 0


if __name__ == "__main__":
    sys.exit(main())
