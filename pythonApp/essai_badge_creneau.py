# -*- coding: utf-8 -*-
"""
Essai PHYSIQUE d'un creneau horaire : la seule chose que le SDK ne peut pas
prouver. On pose une plage sur un jour, on affecte un adherent, et on va
badger avant puis apres l'heure.

    python essai_badge_creneau.py poser --pin 3040 --jour mardi --debut 09:35
    python essai_badge_creneau.py etat
    python essai_badge_creneau.py restaurer

L'etat d'origine de l'adherent (groupe ET privilege) est sauvegarde dans
essai_badge_creneau_sauvegarde.json avant toute ecriture. "restaurer" le
remet exactement comme il etait.

CE QUE FAIT LE PRIVILEGE 3, ET CE QU'IL NE FAIT PAS
---------------------------------------------------
Mesure du 2026-08-25 : un super-admin (privilege 3) EST soumis au controle
d'acces horaire comme tout le monde -- il ne passe PAS la porte hors de sa
plage. Ce que le privilege 3 lui donne, c'est l'acces au MENU de la
pointeuse, ou il peut modifier la configuration.

L'essai est donc concluant sans toucher au privilege, et le script n'y touche
pas par defaut. --retrograder existe pour le mettre a 0 le temps d'un essai
(il verifie alors qu'il reste un autre admin, pour ne pas verrouiller le
menu), mais ce n'est pas necessaire a la validite du test.

A noter tout de meme : zkem_adapter.add_user cree TOUS les adherents en
privilege 3, donc chaque membre du club peut ouvrir le menu de la pointeuse.
Sujet a traiter separement -- reserver le privilege 3 au personnel -- sans
rapport avec les creneaux horaires.

Ecritures interdites sur 192.168.1.201 et .202 -- voir
experience_timezone_standalone.py pour le detail du materiel.
"""

import argparse
import json
import os
import socket
import sys
from datetime import datetime

try:
    import win32com.client
except ImportError:
    print("pywin32 absent : pip install pywin32", file=sys.stderr)
    sys.exit(2)

from services.zkem_timezone import (
    JOURS, encoder_semaine, decoder_semaine, FERME, SLOT_MAX,
)

IP_DEFAUT = "192.168.2.230"
IP_LECTURE_SEULE = {"192.168.1.201", "192.168.1.202"}
PORT = 4370
COMKEY = 123456
MN = 1

SAUVEGARDE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "essai_badge_creneau_sauvegarde.json")

# Noms francais -> cles de l'entite Timezone cote back.
NOMS_JOURS = {
    "dimanche": "sunday", "lundi": "monday", "mardi": "tuesday",
    "mercredi": "wednesday", "jeudi": "thursday", "vendredi": "friday",
    "samedi": "saturday",
}
JOURS_FR = {v: k for k, v in NOMS_JOURS.items()}


def joignable(ip, port, delai=2.5):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def connecter(ip):
    if not joignable(ip, PORT):
        print("%s:%s injoignable -- pointeuse eteinte." % (ip, PORT), file=sys.stderr)
        sys.exit(2)
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(COMKEY)
    if not z.Connect_Net(ip, PORT):
        print("Connect_Net KO sur %s" % ip, file=sys.stderr)
        sys.exit(2)
    return z


def horloge(z):
    """L'heure de la MACHINE, la seule qui compte pour le controle d'acces."""
    ok, an, mois, jour, h, mi, s = z.GetDeviceTime(MN, 0, 0, 0, 0, 0, 0)
    if not ok:
        return None
    return datetime(an, mois, jour, h, mi, s)


def lire_adherent(z, pin):
    ok, nom, mdp, privilege, actif = z.SSR_GetUserInfo(MN, str(pin))
    if not ok:
        return None
    return {
        "pin": str(pin), "nom": nom, "privilege": privilege, "actif": actif,
        "groupe": z.GetUserGroup(MN, int(pin), 0)[1],
        "validite": z.GetUserValidDate(MN, str(pin))[3:5],
    }


def autres_admins(z, pin):
    """Combien d'autres utilisateurs peuvent encore entrer dans le menu."""
    z.ReadAllUserID(MN)
    n = 0
    while True:
        r = z.SSR_GetAllUserInfo(MN)
        if not r or not r[0]:
            break
        if str(r[1]) != str(pin) and r[4] >= 2:
            n += 1
    return n


# ---------------------------------------------------------------------------

def cmd_poser(z, args):
    jour = NOMS_JOURS.get(args.jour.strip().lower())
    if jour is None:
        print("jour inconnu : %r (attendus : %s)"
              % (args.jour, ", ".join(NOMS_JOURS)), file=sys.stderr)
        return 2

    etat = lire_adherent(z, args.pin)
    if etat is None:
        print("PIN %s inconnu de la machine." % args.pin, file=sys.stderr)
        return 2

    maintenant = horloge(z)
    print("=== Avant ===")
    print("  horloge machine : %s (%s)"
          % (maintenant, JOURS_FR[JOURS[(maintenant.weekday() + 1) % 7]]))
    print("  %s (pin %s) : groupe %s, privilege %s, validite %s -> %s"
          % (etat["nom"], etat["pin"], etat["groupe"], etat["privilege"],
             etat["validite"][0], etat["validite"][1]))

    # La periode de validite prime sur le calendrier : hors periode, la porte
    # reste fermee quoi qu'on fasse, et l'essai ne prouverait rien.
    if not etat["validite"][0]:
        print("\n/!\\ Cet adherent n'a AUCUNE periode de validite -- l'essai "
              "serait ininterpretable.", file=sys.stderr)
        return 1

    rétrograder = etat["privilege"] >= 2 and args.retrograder
    if rétrograder:
        restants = autres_admins(z, args.pin)
        print("  privilege %s = admin : donne l'acces au MENU. Sans effet "
              "sur le passage de porte." % etat["privilege"])
        if restants == 0:
            print("\n/!\\ C'est le SEUL admin de l'appareil : le retrograder "
                  "verrouillerait le menu de la pointeuse.\n    Relancez "
                  "sans --retrograder -- le privilege ne fausse pas "
                  "l'essai, il ne donne que l'acces au menu.",
                  file=sys.stderr)
            return 1
        print("  -> retrograde en privilege 0 pour la duree de l'essai "
              "(%s autre(s) admin(s) restent)." % restants)

    # Relancer "poser" pour decaler l'heure est le cas NORMAL : on rate la
    # fenetre "avant", on recommence cinq minutes plus tard. Ecraser la
    # sauvegarde a chaque fois enregistrerait l'etat DEJA MODIFIE (groupe
    # d'essai, privilege 0) et "restaurer" rendrait alors un adherent
    # definitivement rétrogradé. La premiere sauvegarde est donc la bonne, et
    # on n'y touche plus tant qu'on n'a pas restaure.
    ancienne = None
    if os.path.exists(SAUVEGARDE):
        try:
            with open(SAUVEGARDE, encoding="utf-8") as f:
                ancienne = json.load(f)
        except (ValueError, OSError):
            ancienne = None

    if ancienne and ancienne.get("avant", {}).get("pin") == str(args.pin):
        origine = ancienne["avant"]
        print("  sauvegarde existante conservee : groupe %s, privilege %s "
              "(essai deja en cours)" % (origine["groupe"], origine["privilege"]))
        # Le creneau peut avoir change d'un essai a l'autre : on garde trace
        # de tous ceux qu'on a touches, pour les liberer tous a la fin.
        slots = sorted(set(ancienne.get("slots", [ancienne["slot"]])) | {args.slot})
    else:
        origine = etat
        slots = [args.slot]

    with open(SAUVEGARDE, "w", encoding="utf-8") as f:
        json.dump({"ip": args.ip, "slot": args.slot, "slots": slots,
                   "avant": origine}, f, indent=2, ensure_ascii=False)
    print("  etat d'origine dans %s" % os.path.basename(SAUVEGARDE))

    # Tous les autres jours FERMES : c'est ce qui rend l'essai lisible. Un
    # seul jour ouvert, une seule heure de bascule.
    horaire = {jour: "%s-%s" % (args.debut, args.fin)}
    chaine = encoder_semaine(horaire)

    print("\n=== Ecriture (creneau %s) ===" % args.slot)
    z.EnableDevice(MN, False)
    try:
        etapes = [
            ("SetTZInfo", z.SetTZInfo(MN, args.slot, chaine)),
            ("SSR_SetGroupTZ", z.SSR_SetGroupTZ(MN, args.slot, args.slot, 0, 0, 0, 0)),
            ("SSR_SetUnLockGroup", z.SSR_SetUnLockGroup(MN, args.slot, args.slot, 0, 0, 0, 0)),
        ]
        if rétrograder:
            etapes.append(("SSR_SetUserInfo privilege=0",
                           z.SSR_SetUserInfo(MN, str(args.pin), etat["nom"], "", 0, True)))
        etapes.append(("SetUserGroup",
                       z.SetUserGroup(MN, int(args.pin), args.slot)))
        for nom, resultat in etapes:
            print("  %-32s %s" % (nom, "OK" if resultat else "ECHEC"))
            if not resultat:
                return 1
        z.RefreshData(MN)
    finally:
        z.EnableDevice(MN, True)

    return _resume(z, args, jour, maintenant)


def _resume(z, args, jour, maintenant):
    print("\n=== Relecture ===")
    relu = decoder_semaine(z.GetTZInfo(MN, args.slot, "")[1])
    for j in JOURS:
        print("  %-10s %s" % (JOURS_FR[j], relu.get(j) or "ferme"))
    apres = lire_adherent(z, args.pin)
    print("  pin %s : groupe %s, privilege %s"
          % (apres["pin"], apres["groupe"], apres["privilege"]))

    print("\n=== Ce qu'il faut observer ===")
    aujourdhui = JOURS[(maintenant.weekday() + 1) % 7]
    if aujourdhui != jour:
        print("  Nous sommes %s, le creneau est pose sur %s :"
              % (JOURS_FR[aujourdhui], JOURS_FR[jour]))
        print("  la porte doit rester FERMEE toute la journee.")
        return 0

    bascule = maintenant.replace(hour=int(args.debut[:2]),
                                 minute=int(args.debut[3:5]), second=0)
    reste = (bascule - maintenant).total_seconds()
    print("  AVANT %s  -> badge REFUSE" % args.debut)
    print("  APRES %s  -> badge ACCEPTE (jusqu'a %s)" % (args.debut, args.fin))
    if reste > 0:
        print("\n  Il reste %d min %d s pour faire l'essai 'avant'."
              % (reste // 60, reste % 60))
    else:
        print("\n  /!\\ Il est deja %s sur la machine : la fenetre 'avant' est"
              % maintenant.strftime("%H:%M:%S"))
        print("      passee. Pour la refaire, relancer avec une heure plus")
        print("      tardive, par exemple :")
        print("      python essai_badge_creneau.py poser --pin %s --jour %s "
              "--debut %02d:%02d" % (args.pin, args.jour,
                                     maintenant.hour,
                                     (maintenant.minute + 5) % 60))
    print("\n  Quand c'est fini : python essai_badge_creneau.py restaurer")
    print("\n  Si la porte s'ouvre AVANT l'heure, c'est le calendrier qui")
    print("  n'est pas applique : le privilege, lui, n'y change rien.")
    return 0


def cmd_etat(z, args):
    maintenant = horloge(z)
    print("horloge machine : %s (%s)"
          % (maintenant, JOURS_FR[JOURS[(maintenant.weekday() + 1) % 7]]))
    etat = lire_adherent(z, args.pin)
    if etat is None:
        print("PIN %s inconnu." % args.pin)
        return 1
    print("pin %s (%s) : groupe %s, privilege %s, validite %s -> %s"
          % (etat["pin"], etat["nom"], etat["groupe"], etat["privilege"],
             etat["validite"][0], etat["validite"][1]))
    groupe = etat["groupe"]
    ok, tz1, tz2, tz3, _, _ = z.SSR_GetGroupTZ(MN, groupe, 0, 0, 0, 0, 0)
    if not ok:
        print("/!\\ le groupe %s n'existe pas sur l'appareil -- acces refuse" % groupe)
        return 0
    print("groupe %s -> calendriers %s, %s, %s" % (groupe, tz1, tz2, tz3))
    for tz in (tz1, tz2, tz3):
        if tz:
            relu = decoder_semaine(z.GetTZInfo(MN, tz, "")[1])
            for j in JOURS:
                print("   TZ %-2d %-10s %s" % (tz, JOURS_FR[j], relu.get(j) or "ferme"))
    return 0


def cmd_restaurer(z, args):
    if not os.path.exists(SAUVEGARDE):
        print("Aucune sauvegarde (%s) -- rien a restaurer."
              % os.path.basename(SAUVEGARDE), file=sys.stderr)
        return 2
    with open(SAUVEGARDE, encoding="utf-8") as f:
        sauve = json.load(f)
    avant = sauve["avant"]
    slots = sauve.get("slots", [sauve["slot"]])

    print("=== Restauration de %s (pin %s) ===" % (avant["nom"], avant["pin"]))
    z.EnableDevice(MN, False)
    try:
        etapes = [
            ("privilege %s" % avant["privilege"],
             z.SSR_SetUserInfo(MN, avant["pin"], avant["nom"], "",
                               avant["privilege"], avant["actif"])),
            ("groupe %s" % avant["groupe"],
             z.SetUserGroup(MN, int(avant["pin"]), avant["groupe"])),
            # On rend le creneau d'essai : calendrier ferme sept jours, groupe
            # et combinaison remis a zero. Un groupe orphelin consommerait une
            # des dix combinaisons pour rien.
        ]
        for s_ in slots:
            etapes.append(
                ("creneau %d libere" % s_,
                 z.SetTZInfo(MN, s_, FERME * 7)
                 and z.SSR_SetGroupTZ(MN, s_, 0, 0, 0, 0, 0)
                 and z.SSR_SetUnLockGroup(MN, s_, 0, 0, 0, 0, 0)))
        for nom, resultat in etapes:
            print("  %-24s %s" % (nom, "OK" if resultat else "ECHEC"))
        z.RefreshData(MN)
    finally:
        z.EnableDevice(MN, True)

    apres = lire_adherent(z, avant["pin"])
    print("\n=== Relecture ===")
    print("  pin %s : groupe %s, privilege %s"
          % (apres["pin"], apres["groupe"], apres["privilege"]))
    os.remove(SAUVEGARDE)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default=IP_DEFAUT)
    sp = p.add_subparsers(dest="commande", required=True)

    q = sp.add_parser("poser", help="poser le creneau et y affecter l'adherent")
    q.add_argument("--pin", default="3040")
    q.add_argument("--jour", default="mardi")
    q.add_argument("--debut", default="09:35", metavar="HH:MM")
    q.add_argument("--fin", default="23:59", metavar="HH:MM")
    q.add_argument("--slot", type=int, default=7,
                   help="creneau a utiliser (1..%d), 7 par defaut" % SLOT_MAX)
    q.add_argument("--retrograder", action="store_true",
                   help="passer l'adherent en privilege 0 le temps de l'essai "
                        "(lui retire l'acces au menu ; sans effet sur la porte)")

    q = sp.add_parser("etat", help="etat de l'adherent et de son calendrier")
    q.add_argument("--pin", default="3040")

    sp.add_parser("restaurer", help="remettre l'adherent et le creneau d'origine")

    args = p.parse_args()

    if args.commande != "etat" and args.ip in IP_LECTURE_SEULE:
        print("%s est en lecture seule." % args.ip, file=sys.stderr)
        return 2
    for champ in ("debut", "fin"):
        val = getattr(args, champ, None)
        if val:
            try:
                datetime.strptime(val, "%H:%M")
            except ValueError:
                print("--%s attend HH:MM (recu %r)" % (champ, val), file=sys.stderr)
                return 2

    z = connecter(args.ip)
    try:
        return {"poser": cmd_poser, "etat": cmd_etat,
                "restaurer": cmd_restaurer}[args.commande](z, args) or 0
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
