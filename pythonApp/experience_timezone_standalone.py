# -*- coding: utf-8 -*-
"""
Experience : affecter un utilisateur a des plages horaires sur une pointeuse
STANDALONE ZKTeco (zkemkeeper), avec periode de validite ET calendriers.

Format des calendriers et etat d'usine releves le 2026-08-25 par LECTURE de la
SenseFace 3A 192.168.1.201 (SN VGU6245000120, firmware Ver 6.60). Les pointeuses
192.168.1.201 et .202 sont en production d'essai : le script y refuse toute
ecriture. Les experimentations se font sur 192.168.2.230 (--ip par defaut).
Pas de pointeuse alimentee = pas de bug : sonder d'abord.

La chaine complete, telle que la machine la resout :

    utilisateur --(SetUserGroup)--> groupe --(SSR_SetGroupTZ)--> jusqu'a 3
    calendriers --(SetTZInfo)--> plages horaires par jour
                                        +
    groupe present dans une combinaison de deverrouillage (SSR_SetUnLockGroup)
                                        +
    date du jour dans la periode de validite (SetUserValidDate)
                                        => la porte s'ouvre

Les cinq conditions sont ET-logique. Il en manque une, la machine refuse sans
rien dire de plus qu'un refus de verification.

Etat d'usine constate (a ne pas casser) : TOUS les utilisateurs sont en
groupe 1, le groupe 1 pointe sur le calendrier 1, et le calendrier 1 est
ouvert 00:00-23:59 les sept jours. C'est exactement le "timezone par defaut"
du modele metier -- l'existant est donc deja conforme, aucune migration de
donnees machine n'est necessaire pour les clients actuels.

Ordre des jours : CONFIRME dimanche-d'abord, pas deduit. La 192.168.2.230
portait une configuration posee a la main depuis l'ecran ("mardi 08:14-11:59"
sur le calendrier 2) ; le decodage dimanche-d'abord la restitue exactement.

NE PAS UTILISER SetUserTZStr / SetUserTZs. Mesure du 2026-08-25 sur ce
firmware : la fonction renvoie True dans tous les cas mais n'ecrit rien si la
chaine contient un 0 (donc des qu'on veut moins de 4 calendriers), et quand
elle ecrit, le 4e emplacement est ecrase par une autre valeur ('1:5:6:7' relu
'1:5:6:1'). Pire, la remise a zero ':::' laisse un residu ':::1' -- soit le
calendrier 1, ouvert 24/7 : un echec silencieux ouvre l'acces en grand au lieu
de le fermer. Le seul moyen trouve pour effacer ce residu est de supprimer
puis recreer l'utilisateur. Les calendriers passent donc par le GROUPE, jamais
par l'utilisateur -- c'est aussi le chemin valide a la main sur l'ecran.

Usage
-----
    python experience_timezone_standalone.py lire
    python experience_timezone_standalone.py lire --ip 192.168.1.201

    python experience_timezone_standalone.py poser \
        --pin 99001 --nom "Test TZ" \
        --calendrier "2:lun=06:00-12:00,mar=06:00-12:00,mer=06:00-12:00" \
        --calendrier "3:sam=00:00-23:59,dim=00:00-23:59" \
        --groupe 5 --comb 5 \
        --debut 2026-08-25 --fin 2026-12-31

    python experience_timezone_standalone.py verifier --pin 99001
    python experience_timezone_standalone.py restaurer --pin 99001
    python experience_timezone_standalone.py sonde-jours --calendrier-sonde 9

Codes de sortie : 0 tout va bien, 1 echec d'une operation, 2 conditions non
reunies (pointeuse injoignable, SDK absent).
"""

import argparse
import socket
import sys
from datetime import datetime

try:
    import win32com.client
except ImportError:
    print("pywin32 absent : pip install pywin32", file=sys.stderr)
    sys.exit(2)


PORT_DEFAUT = 4370
COMKEY_DEFAUT = 123456
MN = 1  # numero machine interne, toujours 1 sur une standalone

# Machines du banc sur lesquelles TOUTE ecriture est interdite (consigne du
# 2026-08-25). Elles restent lisibles : c'est sur elles qu'on a releve le
# format des calendriers et l'etat d'usine, et cette lecture ne modifie rien.
# Pour experimenter, designer une autre pointeuse avec --ip.
IP_LECTURE_SEULE = {"192.168.1.201", "192.168.1.202"}
COMMANDES_ECRITURE = {"poser", "restaurer", "sonde-jours"}

# Pointeuse dediee aux experimentations, la seule sur laquelle on ecrit.
IP_DEFAUT = "192.168.2.230"

# Ordre des jours DANS LA CHAINE DE 56 CARACTERES.
# Convention ZKTeco : dimanche en premier. C'est aussi l'ordre des colonnes de
# l'entite Timezone cote back (sunday..saturday), les deux se correspondent
# terme a terme. La sous-commande "sonde-jours" permet de le reverifier sur
# l'ecran de la machine en 10 secondes plutot que de le croire sur parole.
JOURS = ["dim", "lun", "mar", "mer", "jeu", "ven", "sam"]
JOURS_LONGS = {
    "dim": "sunday", "lun": "monday", "mar": "tuesday", "mer": "wednesday",
    "jeu": "thursday", "ven": "friday", "sam": "saturday",
}

FERME = "00000000"       # jour ou l'acces est refuse en permanence
OUVERT_24H = "00002359"  # jour entierement ouvert


# ---------------------------------------------------------------------------
# Encodage / decodage des calendriers
# ---------------------------------------------------------------------------

def encoder_tz(plages):
    """
    plages : dict {"lun": ("06:00", "12:00"), ...}. Un jour absent est ferme.
    Retourne la chaine de 56 caracteres attendue par SetTZInfo.
    """
    morceaux = []
    for jour in JOURS:
        creneau = plages.get(jour)
        if not creneau:
            morceaux.append(FERME)
            continue
        debut, fin = creneau
        morceaux.append(debut.replace(":", "") + fin.replace(":", ""))
    return "".join(morceaux)


def decoder_tz(chaine):
    """Inverse d'encoder_tz, pour l'affichage."""
    if not chaine or len(chaine) != 56:
        return {"?": chaine}
    resultat = {}
    for i, jour in enumerate(JOURS):
        bloc = chaine[i * 8:(i + 1) * 8]
        resultat[jour] = "%s:%s-%s:%s" % (bloc[0:2], bloc[2:4], bloc[4:6], bloc[6:8])
    return resultat


def formater_tz(chaine):
    d = decoder_tz(chaine)
    if "?" in d:
        return repr(chaine)
    return "  ".join(
        "%s %s" % (j, d[j]) if d[j] != "00:00-00:00" else "%s -----" % j
        for j in JOURS
    )


def parser_calendrier(spec):
    """
    "2:lun=06:00-12:00,mar=06:00-12:00"  ->  (2, {"lun": ("06:00","12:00"), ...})
    "4:tous=00:00-23:59"                 ->  (4, les sept jours ouverts)
    """
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            "calendrier attendu sous la forme IDX:jour=HH:MM-HH:MM,... (recu %r)" % spec)
    idx_txt, reste = spec.split(":", 1)
    try:
        idx = int(idx_txt)
    except ValueError:
        raise argparse.ArgumentTypeError("index de calendrier invalide : %r" % idx_txt)

    plages = {}
    for morceau in reste.split(","):
        morceau = morceau.strip()
        if not morceau:
            continue
        if "=" not in morceau:
            raise argparse.ArgumentTypeError("creneau invalide : %r" % morceau)
        jour, creneau = morceau.split("=", 1)
        jour = jour.strip().lower()[:3]
        if "-" not in creneau:
            raise argparse.ArgumentTypeError("creneau invalide : %r" % creneau)
        debut, fin = creneau.split("-", 1)
        debut, fin = debut.strip(), fin.strip()
        for h in (debut, fin):
            try:
                datetime.strptime(h, "%H:%M")
            except ValueError:
                raise argparse.ArgumentTypeError("heure invalide : %r" % h)
        cibles = JOURS if jour in ("tou", "all") else [jour]
        for c in cibles:
            if c not in JOURS:
                raise argparse.ArgumentTypeError(
                    "jour inconnu : %r (attendus : %s, ou 'tous')" % (c, "/".join(JOURS)))
            plages[c] = (debut, fin)
    return idx, plages


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------

def joignable(ip, port, delai=2.0):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def derniere_erreur(zk):
    # GetLastError rend son code par parametre de sortie ; pywin32 le renvoie
    # comme valeur de retour a condition qu'on fournisse un emplacement.
    try:
        return int(zk.GetLastError(0))
    except Exception:
        return "?"


def connecter(ip, port, comkey):
    """Retourne l'objet COM connecte, ou sort en code 2."""
    if not joignable(ip, port):
        print("%s:%s injoignable en TCP -- pointeuse eteinte, pas une regression."
              % (ip, port), file=sys.stderr)
        sys.exit(2)

    # EnsureDispatch (liaison precoce) et non Dispatch : sans la type library,
    # les parametres de sortie de GetTZInfo / SSR_GetGroupTZ ne remontent pas.
    try:
        zk = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    except Exception as ex:
        print("zkemkeeper.ZKEM indisponible (%s). DLL enregistree ? "
              "Purger %%TEMP%%/gen_py si le cache est corrompu." % ex, file=sys.stderr)
        sys.exit(2)

    if comkey:
        try:
            zk.SetCommPassword(int(comkey))
        except Exception as ex:
            print("SetCommPassword refuse (%s), on tente sans." % ex)

    if not zk.Connect_Net(ip, port):
        print("Connect_Net KO sur %s:%s err=%s" % (ip, port, derniere_erreur(zk)),
              file=sys.stderr)
        sys.exit(2)
    print("connecte a %s:%s" % (ip, port))
    return zk


def identifier(zk):
    try:
        _, produit = zk.GetProductCode(MN)
        _, serie = zk.GetSerialNumber(MN)
        _, firmware = zk.GetFirmwareVersion(MN, "")
        print("  modele %s | SN %s | firmware %s" % (produit, serie, firmware))
    except Exception as ex:
        print("  identification indisponible : %s" % ex)


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def cmd_lire(zk, args):
    identifier(zk)

    print("\n--- Calendriers (SetTZInfo/GetTZInfo) ---")
    for i in range(1, args.nb_tz + 1):
        try:
            ok, chaine = zk.GetTZInfo(MN, i, "")
        except Exception as ex:
            print("  TZ %-2d ERREUR %s" % (i, ex))
            continue
        if not ok:
            print("  TZ %-2d absent" % i)
            continue
        defaut = " (24/7, valeur d'usine)" if chaine == OUVERT_24H * 7 else ""
        print("  TZ %-2d %s%s" % (i, formater_tz(chaine), defaut))

    print("\n--- Groupes d'acces (SSR_GetGroupTZ) ---")
    for g in range(1, args.nb_groupes + 1):
        try:
            ok, tz1, tz2, tz3, ferie, verif = zk.SSR_GetGroupTZ(MN, g, 0, 0, 0, 0, 0)
        except Exception as ex:
            print("  groupe %-2d ERREUR %s" % (g, ex))
            continue
        if not ok:
            print("  groupe %-2d non defini" % g)
            continue
        print("  groupe %-2d calendriers=%s,%s,%s  ferie=%s  verif=%s"
              % (g, tz1, tz2, tz3, ferie, verif))

    print("\n--- Combinaisons de deverrouillage (SSR_GetUnLockGroup) ---")
    for c in range(1, args.nb_combs + 1):
        try:
            ok, g1, g2, g3, g4, g5 = zk.SSR_GetUnLockGroup(MN, c, 0, 0, 0, 0, 0)
        except Exception as ex:
            print("  comb %-2d ERREUR %s" % (c, ex))
            continue
        groupes = [g for g in (g1, g2, g3, g4, g5) if g]
        if not ok or not groupes:
            print("  comb %-2d vide" % c)
            continue
        note = ""
        if len(groupes) > 1:
            # Piege documente : plusieurs groupes dans UNE combinaison exige la
            # validation successive d'une personne de chaque groupe. Pour que
            # chaque groupe entre seul, il faut une ligne par groupe.
            note = "  <-- verification MULTIPLE exigee (un badge par groupe)"
        print("  comb %-2d groupes=%s%s" % (c, "+".join(map(str, groupes)), note))


def lire_utilisateur(zk, pin):
    """Retourne un dict d'etat, ou None si le PIN est inconnu de la machine."""
    etat = {"pin": pin}
    try:
        ok, nom, mdp, privilege, actif = zk.SSR_GetUserInfo(MN, str(pin))
    except Exception as ex:
        print("  SSR_GetUserInfo ERREUR %s" % ex)
        return None
    if not ok:
        return None
    etat.update(nom=nom, privilege=privilege, actif=actif)

    try:
        _, groupe = zk.GetUserGroup(MN, int(pin), 0)
        etat["groupe"] = groupe
    except Exception as ex:
        etat["groupe"] = "ERREUR %s" % ex
    try:
        _, tzstr = zk.GetUserTZStr(MN, int(pin), "")
        etat["tz_perso"] = tzstr
    except Exception as ex:
        etat["tz_perso"] = "ERREUR %s" % ex
    try:
        ok, expire, compte, debut, fin = zk.GetUserValidDate(MN, str(pin))
        etat["validite"] = (debut, fin) if ok else None
    except Exception as ex:
        etat["validite"] = "ERREUR %s" % ex
    return etat


def afficher_utilisateur(zk, etat):
    if etat is None:
        print("  utilisateur inconnu de la machine")
        return
    print("  PIN %s | nom %r | privilege %s | actif %s"
          % (etat["pin"], etat.get("nom"), etat.get("privilege"), etat.get("actif")))
    print("  groupe d'acces : %s" % etat.get("groupe"))
    tzp = etat.get("tz_perso")
    if tzp in (":::", "", None):
        print("  calendriers personnels : aucun -> l'utilisateur suit ceux de son groupe")
    else:
        print("  calendriers personnels : %r (ils PRIMENT sur ceux du groupe)" % tzp)
    val = etat.get("validite")
    if val is None:
        print("  periode de validite : aucune (acces permanent tant que le groupe l'autorise)")
    else:
        print("  periode de validite : %s -> %s" % val)

    groupe = etat.get("groupe")
    if isinstance(groupe, int):
        try:
            ok, tz1, tz2, tz3, ferie, verif = zk.SSR_GetGroupTZ(MN, groupe, 0, 0, 0, 0, 0)
        except Exception as ex:
            print("  groupe %s illisible : %s" % (groupe, ex))
            return
        if not ok:
            print("  /!\\ le groupe %s n'est pas defini sur la machine -- acces refuse" % groupe)
            return
        print("  -> groupe %s : calendriers %s, %s, %s" % (groupe, tz1, tz2, tz3))
        for tz in (tz1, tz2, tz3):
            if not tz:
                continue
            try:
                ok, chaine = zk.GetTZInfo(MN, tz, "")
                if ok:
                    print("     TZ %-2d %s" % (tz, formater_tz(chaine)))
            except Exception as ex:
                print("     TZ %-2d illisible : %s" % (tz, ex))
        # Le groupe doit apparaitre dans au moins une combinaison, sinon rien
        # ne s'ouvre quelle que soit la qualite du calendrier.
        autorise = []
        for c in range(1, 11):
            try:
                ok, g1, g2, g3, g4, g5 = zk.SSR_GetUnLockGroup(MN, c, 0, 0, 0, 0, 0)
            except Exception:
                continue
            groupes = [g for g in (g1, g2, g3, g4, g5) if g]
            if ok and groupes == [groupe]:
                autorise.append(c)
        if autorise:
            print("  -> groupe autorise seul par la/les combinaison(s) %s"
                  % ", ".join(map(str, autorise)))
        else:
            print("  /!\\ AUCUNE combinaison n'autorise le groupe %s a ouvrir SEUL "
                  "-- la porte restera fermee" % groupe)


def cmd_verifier(zk, args):
    identifier(zk)
    print("\n--- Etat de l'utilisateur %s ---" % args.pin)
    afficher_utilisateur(zk, lire_utilisateur(zk, args.pin))


# ---------------------------------------------------------------------------
# Ecriture
# ---------------------------------------------------------------------------

def _ok(libelle, resultat, zk):
    if resultat:
        print("  OK   %s" % libelle)
        return True
    print("  ECHEC %s (err=%s)" % (libelle, derniere_erreur(zk)))
    return False


def cmd_poser(zk, args):
    identifier(zk)
    calendriers = [parser_calendrier(s) for s in args.calendrier]
    if len(calendriers) > 3:
        print("Un groupe ZKTeco ne porte que 3 calendriers au maximum ; "
              "%d fournis." % len(calendriers), file=sys.stderr)
        return 1

    indices = [idx for idx, _ in calendriers]
    while len(indices) < 3:
        indices.append(0)

    print("\n--- Plan ---")
    for idx, plages in calendriers:
        print("  TZ %-2d <- %s" % (idx, formater_tz(encoder_tz(plages))))
    print("  groupe %s <- calendriers %s" % (args.groupe, indices))
    print("  comb %s <- groupe %s (seul)" % (args.comb, args.groupe))
    print("  utilisateur %s (%s) -> groupe %s, validite %s..%s"
          % (args.pin, args.nom, args.groupe, args.debut, args.fin))
    if args.dry_run:
        print("\n--dry-run : rien n'a ete ecrit.")
        return 0

    succes = True
    print("\n--- Ecriture ---")
    # EnableDevice(False) fige le lecteur pendant la mise a jour : sans ca, un
    # badge presente au milieu de la sequence lit une configuration a moitie
    # ecrite.
    zk.EnableDevice(MN, False)
    try:
        for idx, plages in calendriers:
            chaine = encoder_tz(plages)
            succes &= _ok("SetTZInfo(%d) = %s" % (idx, chaine),
                          zk.SetTZInfo(MN, idx, chaine), zk)

        # VaildHoliday=0 et VerifyStyle=0 : les valeurs du groupe 1 d'usine.
        # VerifyStyle=0 signifie "suivre le mode de verification de l'appareil"
        # -- on ne veut surtout pas imposer un mode ici.
        succes &= _ok("SSR_SetGroupTZ(groupe=%d, tz=%s)" % (args.groupe, indices),
                      zk.SSR_SetGroupTZ(MN, args.groupe, indices[0], indices[1],
                                        indices[2], 0, 0), zk)

        # Une ligne PAR groupe. Mettre deux groupes dans la meme combinaison
        # exigerait deux badges successifs pour ouvrir.
        succes &= _ok("SSR_SetUnLockGroup(comb=%d, groupe=%d seul)"
                      % (args.comb, args.groupe),
                      zk.SSR_SetUnLockGroup(MN, args.comb, args.groupe, 0, 0, 0, 0), zk)

        succes &= _ok("SSR_SetUserInfo(pin=%s)" % args.pin,
                      zk.SSR_SetUserInfo(MN, str(args.pin), args.nom, "",
                                         args.privilege, True), zk)

        succes &= _ok("SetUserGroup(pin=%s -> groupe %d)" % (args.pin, args.groupe),
                      zk.SetUserGroup(MN, int(args.pin), args.groupe), zk)

        # Calendriers personnels vides : l'utilisateur suit ceux de son groupe.
        # C'est le mode qu'on veut -- les 173 users existants sont deja ainsi.
        if args.tz_perso:
            succes &= _ok("SetUserTZStr(pin=%s) = %r" % (args.pin, args.tz_perso),
                          zk.SetUserTZStr(MN, int(args.pin), args.tz_perso), zk)

        debut = "%s 00:00:00" % args.debut
        fin = "%s 23:59:59" % args.fin
        succes &= _ok("SetUserValidDate(pin=%s, %s -> %s)" % (args.pin, debut, fin),
                      zk.SetUserValidDate(MN, str(args.pin), 1, 1, debut, fin), zk)
    finally:
        try:
            zk.RefreshData(MN)
        except Exception as ex:
            print("  RefreshData a leve %s" % ex)
        zk.EnableDevice(MN, True)

    print("\n--- Relecture ---")
    afficher_utilisateur(zk, lire_utilisateur(zk, args.pin))
    return 0 if succes else 1


def cmd_restaurer(zk, args):
    """Remet l'utilisateur dans l'etat par defaut : groupe 1, aucun TZ perso."""
    identifier(zk)
    print("\n--- Restauration de %s vers le groupe 1 ---" % args.pin)
    succes = True
    zk.EnableDevice(MN, False)
    try:
        succes &= _ok("SetUserGroup(pin=%s -> groupe 1)" % args.pin,
                      zk.SetUserGroup(MN, int(args.pin), 1), zk)
        succes &= _ok("SetUserTZStr(pin=%s) = ':::'" % args.pin,
                      zk.SetUserTZStr(MN, int(args.pin), ":::"), zk)
    finally:
        try:
            zk.RefreshData(MN)
        except Exception:
            pass
        zk.EnableDevice(MN, True)
    print("\n--- Relecture ---")
    afficher_utilisateur(zk, lire_utilisateur(zk, args.pin))
    return 0 if succes else 1


def cmd_sonde_jours(zk, args):
    """
    Ecrit un calendrier ou chaque jour porte une heure differente, pour lever
    tout doute sur l'ordre des sept blocs de la chaine de 56 caracteres.

    Apres execution, ouvrir sur la machine :
    Menu > Controle d'acces > Calendriers > <index>, et verifier que la ligne
    "dimanche" affiche bien 01:00-01:59.
    """
    idx = args.calendrier_sonde
    plages = {jour: ("%02d:00" % (i + 1), "%02d:59" % (i + 1))
              for i, jour in enumerate(JOURS)}
    chaine = encoder_tz(plages)
    print("\n--- Sonde d'ordre des jours sur le calendrier %d ---" % idx)
    print("  chaine ecrite : %s" % chaine)
    for i, jour in enumerate(JOURS):
        print("    bloc %d -> %s (%s) attendu %02d:00-%02d:59"
              % (i, jour, JOURS_LONGS[jour], i + 1, i + 1))
    if args.dry_run:
        print("\n--dry-run : rien n'a ete ecrit.")
        return 0
    zk.EnableDevice(MN, False)
    try:
        ok = _ok("SetTZInfo(%d)" % idx, zk.SetTZInfo(MN, idx, chaine), zk)
    finally:
        try:
            zk.RefreshData(MN)
        except Exception:
            pass
        zk.EnableDevice(MN, True)
    _, relu = zk.GetTZInfo(MN, idx, "")
    print("  relu          : %s" % relu)
    print("  -> %s" % formater_tz(relu))
    print("\nVerifier maintenant sur l'ecran de la machine :")
    print("  Menu > Controle d'acces > Calendriers > %d" % idx)
    print("  Si la ligne DIMANCHE affiche 01:00-01:59, l'ordre dimanche-d'abord")
    print("  est confirme et JOURS peut rester tel quel.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Calendriers et groupes d'acces sur une standalone ZKTeco.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--ip", default=IP_DEFAUT)
    p.add_argument("--port", type=int, default=PORT_DEFAUT)
    p.add_argument("--comkey", type=int, default=COMKEY_DEFAUT,
                   help="0 pour ne pas envoyer de mot de passe de communication")
    p.add_argument("--dry-run", action="store_true",
                   help="affiche ce qui serait ecrit, sans rien ecrire")

    sp = p.add_subparsers(dest="commande", required=True)

    q = sp.add_parser("lire", help="inventaire des calendriers, groupes et combinaisons")
    q.add_argument("--nb-tz", type=int, default=10)
    q.add_argument("--nb-groupes", type=int, default=8)
    q.add_argument("--nb-combs", type=int, default=10)

    q = sp.add_parser("poser", help="creer les calendriers, le groupe, et y placer un utilisateur")
    q.add_argument("--pin", required=True)
    q.add_argument("--nom", default="Test TZ")
    q.add_argument("--privilege", type=int, default=0,
                   help="0 utilisateur, 2 admin, 3 super-admin (defaut 0 : "
                        "un admin ouvre TOUJOURS, ce qui masquerait le test)")
    q.add_argument("--calendrier", action="append", required=True,
                   metavar="IDX:jour=HH:MM-HH:MM,...",
                   help="repetable, 3 au maximum ; jour parmi dim/lun/mar/mer/"
                        "jeu/ven/sam ou 'tous' ; un jour non cite est ferme")
    q.add_argument("--groupe", type=int, required=True)
    q.add_argument("--comb", type=int, required=True,
                   help="numero de combinaison de deverrouillage a affecter au groupe")
    q.add_argument("--debut", required=True, metavar="AAAA-MM-JJ")
    q.add_argument("--fin", required=True, metavar="AAAA-MM-JJ")
    q.add_argument("--tz-perso", default=None,
                   help="calendriers personnels 'f:tz1:tz2:tz3' ; par defaut on "
                        "n'y touche pas, l'utilisateur suit son groupe")

    q = sp.add_parser("verifier", help="etat complet d'un utilisateur et de sa chaine d'acces")
    q.add_argument("--pin", required=True)

    q = sp.add_parser("restaurer", help="remettre un utilisateur en groupe 1 sans TZ perso")
    q.add_argument("--pin", required=True)

    q = sp.add_parser("sonde-jours", help="lever le doute sur l'ordre des jours")
    q.add_argument("--calendrier-sonde", type=int, default=9)

    args = p.parse_args()

    if args.commande in COMMANDES_ECRITURE and args.ip in IP_LECTURE_SEULE:
        print("%s est en lecture seule : aucune ecriture n'y est autorisee. "
              "Utiliser --ip %s pour experimenter." % (args.ip, IP_DEFAUT),
              file=sys.stderr)
        return 2

    for champ in ("debut", "fin"):
        valeur = getattr(args, champ, None)
        if valeur:
            try:
                datetime.strptime(valeur, "%Y-%m-%d")
            except ValueError:
                print("--%s attend AAAA-MM-JJ (recu %r)" % (champ, valeur), file=sys.stderr)
                return 2

    zk = connecter(args.ip, args.port, args.comkey)
    try:
        return {
            "lire": cmd_lire,
            "poser": cmd_poser,
            "verifier": cmd_verifier,
            "restaurer": cmd_restaurer,
            "sonde-jours": cmd_sonde_jours,
        }[args.commande](zk, args) or 0
    finally:
        try:
            zk.Disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
