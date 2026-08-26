# -*- coding: utf-8 -*-
"""Tests d'INTÉGRATION des créneaux horaires — exige les deux pointeuses.

Conditions requises :
  - C3         192.168.1.205 joignable
  - standalone 192.168.2.230 joignable (comKey 123456)
  - pythonApp ARRÊTÉ — un C3 ne délivre qu'une session à la fois, lancer ces
    tests pendant que le pont tourne fait tomber les deux

    venv\\Scripts\\python.exe tests\\test_timezone_machines.py

Ce que ces tests protègent réellement, et pourquoi ils existent :

process_device_queue appelle désormais `adapter.add_user(...)` avec SEPT
arguments positionnels, pour les deux familles de pointeuse. L'adaptateur C3 ne
se sert pas des deux derniers dans add_user, mais il doit les accepter : une
signature oubliée, c'est un TypeError à la première tâche d'accès, sur du
matériel client. Les tests C3 ci-dessous ne vérifient donc pas une nouvelle
fonctionnalité — ils vérifient une NON-RÉGRESSION, et c'est le point le plus
important du lot.

Non destructifs : PIN dédiés (999001 sur C3, 99005 sur la standalone),
supprimés en fin de parcours. Aucune porte n'est ouverte, aucun utilisateur
existant n'est touché.

Codes de sortie : 0 tout passe, 1 échec, 2 conditions non réunies.
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harnais import Suite, verifier, egal, ROUGE, VERT, GRAS, RAZ  # noqa: E402

IP_C3 = "192.168.1.205"
IP_STANDALONE = "192.168.2.230"
PORT = 4370
COMKEY_STANDALONE = 123456

PIN_C3 = "999001"
PIN_SA = "99005"

# Créneau de test sur la standalone. 1, 2, 3, 5 et 6 sont déjà pris (1 =
# défaut d'usine, 2 = configuration posée à la main sur l'écran, 3/5/6 =
# essais précédents) : on prend 4, libre, et on le rend en fin de parcours.
SLOT_SA = 4

# Sur le C3, seul le calendrier 1 existe d'usine. On prend le 7, comme sur la
# standalone : même numéro de créneau, mêmes horaires, deux familles.
SLOT_C3 = 7

HORAIRE_SA = {"monday": "06:00-12:00", "tuesday": "06:00-12:00",
              "wednesday": "06:00-12:00", "thursday": "06:00-12:00",
              "friday": "06:00-12:00"}          # samedi et dimanche fermés

HORAIRE_MODIFIE = {"monday": "07:30-11:00", "saturday": "09:00-13:00"}

CTX = {}

suite = Suite("PONT — créneaux horaires sur pointeuses réelles (C3 + standalone)")


# ─── Contrôle préalable ──────────────────────────────────────────────────

def _joignable(ip, port, delai=2.5):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _abandonner(manquants):
    print(f"\n{ROUGE}Conditions non réunies — aucun test exécuté{RAZ}")
    for m in manquants:
        print(f"  - {m}")
    sys.exit(2)


def controle_prealable():
    manquants = []
    print(f"{GRAS}Contrôle préalable{RAZ}")
    print("-" * 76)

    for ip, quoi in ((IP_C3, "C3"), (IP_STANDALONE, "standalone")):
        if _joignable(ip, PORT):
            print(f"  {VERT}ok{RAZ}    {quoi} {ip} joignable")
        else:
            print(f"  {ROUGE}ECHEC{RAZ} {quoi} {ip} injoignable")
            manquants.append(f"{quoi} {ip}:{PORT} injoignable — pointeuse "
                             f"éteinte ou hors du réseau du poste")

    # Le pont doit être arrêté : il tient la session du C3.
    if _joignable("127.0.0.1", 9998, delai=1.0):
        print(f"  {ROUGE}ECHEC{RAZ} pythonApp répond sur 9998")
        manquants.append("pythonApp tourne (port 9998) — il détient la session "
                         "du C3, arrêtez-le avant de lancer ces tests")
    else:
        print(f"  {VERT}ok{RAZ}    pythonApp arrêté (9998 libre)")

    if manquants:
        _abandonner(manquants)
    print()


# ─── Outillage ───────────────────────────────────────────────────────────

def _machine(ip, alias, type_):
    from domain.AccessMachine import AccessMachine
    return AccessMachine(id=999999, alias=alias, addresseip=ip, port=PORT,
                         statut="Active", type=type_, door1=1, door2=0,
                         door3=0, door4=0, porte_type=None,
                         comKey=COMKEY_STANDALONE if type_ != "C3" else 0)


def _zk_brut():
    """Objet COM indépendant, pour relire l'appareil sans passer par l'adaptateur."""
    import win32com.client
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    z.SetCommPassword(COMKEY_STANDALONE)
    verifier(z.Connect_Net(IP_STANDALONE, PORT), "relecture : connexion standalone KO")
    return z


# ═════════════════════════════════════════════════════════════════════════
# C3 — le chemin Pull SDK. Le calendrier y vit dans la table `timezone` et
# l'affectation dans `userauthorize` : pas de groupe, pas de combinaison.
# ═════════════════════════════════════════════════════════════════════════

def _lire_c3(table, filtre=b""):
    """Lecture brute d'une table du panneau, pour vérifier ce qui y est écrit.

    ⚠️ Ne JAMAIS passer un nom de table inconnu : `timeseg` (qui n'existe pas)
    fait planter le process — exit 127, aucune exception, la DLL emporte tout.
    """
    import ctypes
    from services.adapters import plcommpro
    buf = ctypes.create_string_buffer(262144)
    ret = plcommpro.GetDeviceData(CTX["c3"].handle, buf, 262144,
                                  table, b"*", filtre, b"")
    verifier(ret >= 0, f"GetDeviceData({table!r}) a renvoyé {ret}")
    return buf.value.decode("utf-8", errors="ignore").strip()


def _ligne_timezone(slot):
    for l in _lire_c3(b"timezone").splitlines()[1:]:
        if l.split(",")[0] == str(slot):
            return l.split(",")
    return None


@suite.test("C3 : add_user accepte les 7 arguments positionnels du nouveau contrat")
def _():
    from services.adapters import PlcommAdapter
    a = PlcommAdapter(_machine(IP_C3, "C3 banc", "C3"))
    verifier(a.connect() is not None, "connexion C3 impossible")
    CTX["c3"] = a
    # L'appel exact que fait process_device_queue. Une signature non mise à
    # jour lèverait TypeError ici — c'est la non-régression qui compte.
    ok = a.add_user(PIN_C3, "Test TZ C3", "", "20260825", "20261231",
                    SLOT_C3, HORAIRE_SA)
    verifier(ok, "add_user C3 a renvoyé False")


@suite.test("C3 : l'utilisateur est bien écrit, avec ses dates de validité")
def _():
    brut = _lire_c3(b"user", f"Pin={PIN_C3}\t".encode())
    verifier(PIN_C3 in brut, f"le PIN {PIN_C3} est absent de la table user")
    verifier("20260825" in brut, "la date de début n'a pas été écrite")
    verifier("20261231" in brut, "la date de fin n'a pas été écrite")


@suite.test("C3 : add_user ignore le créneau — chez lui il se pose dans authorize_user")
def _():
    a = CTX["c3"]
    avec = _lire_c3(b"user", f"Pin={PIN_C3}\t".encode())
    verifier(a.add_user(PIN_C3, "Test TZ C3", "", "20260825", "20261231"),
             "add_user C3 sans créneau a renvoyé False")
    sans = _lire_c3(b"user", f"Pin={PIN_C3}\t".encode())
    egal(sans, avec, "la fiche user diffère selon qu'on passe ou non un créneau")


@suite.test("C3 : authorize_user avec un créneau écrit la table timezone")
def _():
    a = CTX["c3"]
    verifier(a.authorize_user(PIN_C3, SLOT_C3, HORAIRE_SA),
             "authorize_user C3 a renvoyé False")
    ligne = _ligne_timezone(SLOT_C3)
    verifier(ligne is not None, f"aucune ligne timezone pour le créneau {SLOT_C3}")
    # Colonnes : TimezoneId, Sun1..3, Mon1..3, Tue1..3, Wed1..3, Thu1..3,
    # Fri1..3, Sat1..3. L'entier vaut débutHHMM * 10000 + finHHMM.
    egal(ligne[1], "0", "dimanche doit être fermé")
    egal(ligne[4], "6001200", "lundi doit valoir 06:00-12:00")
    egal(ligne[7], "6001200", "mardi doit valoir 06:00-12:00")
    egal(ligne[19], "0", "samedi doit être fermé")


@suite.test("C3 : la fiche userauthorize renvoie bien vers ce créneau")
def _():
    lignes = [l.split(",") for l in
              _lire_c3(b"userauthorize", f"Pin={PIN_C3}\t".encode()).splitlines()[1:]]
    verifier(lignes, "aucune ligne userauthorize pour le PIN de test")
    for l in lignes:
        egal(l[1], str(SLOT_C3),
             f"AuthorizeTimezoneId inattendu sur la porte {l[2]}")


@suite.test("C3 : modifier la timezone réécrit LE MÊME créneau")
def _():
    a = CTX["c3"]
    verifier(a.authorize_user(PIN_C3, SLOT_C3, HORAIRE_MODIFIE),
             "authorize_user C3 a renvoyé False")
    ligne = _ligne_timezone(SLOT_C3)
    egal(ligne[4], "7301100", "lundi doit valoir 07:30-11:00")
    egal(ligne[19], "9001300", "samedi doit valoir 09:00-13:00")
    # Les jours retirés de l'horaire doivent repasser à FERMÉ. Si on n'écrivait
    # que les jours présents, mardi garderait 06:00-12:00 de l'essai précédent.
    egal(ligne[7], "0", "mardi doit être refermé, pas garder l'ancienne valeur")


@suite.test("C3 : changer de créneau REMPLACE la ligne, ne s'y ajoute pas")
def _():
    # La régression la plus vicieuse du lot : userauthorize a pour clé
    # (Pin, TimezoneId, DoorId). Sans purge préalable, repasser un adhérent du
    # créneau 7 au créneau 1 laissait QUATRE lignes, et le calendrier le plus
    # permissif l'emportait — la restriction n'était jamais appliquée.
    a = CTX["c3"]
    avant = _ligne_timezone(1)
    verifier(a.authorize_user(PIN_C3), "authorize_user C3 sans créneau a échoué")
    lignes = [l.split(",") for l in
              _lire_c3(b"userauthorize", f"Pin={PIN_C3}\t".encode()).splitlines()[1:]]
    egal(len(lignes), 2, "il doit rester exactement 2 lignes (une par porte)")
    for l in lignes:
        egal(l[1], "1", "sans créneau, la fiche doit pointer sur le calendrier 1")
    egal(_ligne_timezone(1), avant,
         "le calendrier 1 a été réécrit alors qu'il ne devait pas l'être")


@suite.test("C3 : horaire illisible -> repli sur le calendrier 1")
def _():
    a = CTX["c3"]
    verifier(a.authorize_user(PIN_C3, SLOT_C3, {"monday": "6h à midi"}),
             "un horaire illisible ne doit pas faire échouer l'autorisation")
    lignes = [l.split(",") for l in
              _lire_c3(b"userauthorize", f"Pin={PIN_C3}\t".encode()).splitlines()[1:]]
    for l in lignes:
        egal(l[1], "1", "un horaire illisible doit replier sur le calendrier 1")


@suite.test("C3 : UPDATE_TIMEZONE réécrit le calendrier sans toucher aux fiches")
def _():
    # La promesse métier : le gérant change les horaires, tous les adhérents du
    # créneau suivent, sans qu'on repousse un seul accès.
    a = CTX["c3"]
    verifier(a.authorize_user(PIN_C3, SLOT_C3, HORAIRE_SA),
             "remise en place du créneau avant l'essai")
    avant = [l.split(",") for l in
             _lire_c3(b"userauthorize", f"Pin={PIN_C3}\t".encode()).splitlines()[1:]]

    verifier(a.update_timezone(SLOT_C3, HORAIRE_MODIFIE),
             "update_timezone a renvoyé False")

    ligne = _ligne_timezone(SLOT_C3)
    egal(ligne[4], "7301100", "lundi porte le nouvel horaire")
    egal(ligne[19], "9001300", "samedi porte le nouvel horaire")
    apres = [l.split(",") for l in
             _lire_c3(b"userauthorize", f"Pin={PIN_C3}\t".encode()).splitlines()[1:]]
    egal(apres, avant, "aucune fiche userauthorize n'a bougé")


@suite.test("C3 : UPDATE_TIMEZONE sur le créneau 1 ne touche pas au calendrier par défaut")
def _():
    a = CTX["c3"]
    avant = _ligne_timezone(1)
    verifier(a.update_timezone(1, {"monday": "09:00-10:00"}),
             "une demande sur le créneau par défaut doit être acceptée sans effet")
    egal(_ligne_timezone(1), avant,
         "le calendrier 1 est le 24/7 d'usine, il ne se réécrit jamais")


@suite.test("C3 : ménage du PIN et du créneau de test")
def _():
    a = CTX["c3"]
    a.delete_user(PIN_C3)              # retire les lignes userauthorize
    a._del(b"user", f"Pin={PIN_C3}")   # puis la fiche
    a._del(b"timezone", f"TimezoneId={SLOT_C3}")
    reste = _lire_c3(b"user", f"Pin={PIN_C3}\t".encode())
    verifier(PIN_C3 not in reste, "le PIN de test n'a pas été supprimé")
    verifier(_ligne_timezone(SLOT_C3) is None,
             "le créneau de test n'a pas été supprimé")
    verifier(_ligne_timezone(1) is not None, "le calendrier 1 a disparu !")
    a.disconnect()


# ═════════════════════════════════════════════════════════════════════════
# Standalone — la fonctionnalité elle-même.
# ═════════════════════════════════════════════════════════════════════════

@suite.test("standalone : créneau 4 -> calendrier, groupe et combinaison écrits")
def _():
    from services.zkem_adapter import ZkemAdapter
    a = ZkemAdapter(_machine(IP_STANDALONE, "standalone banc",
                             "STANDALONE_NEW_FIRMWARE"))
    CTX["sa"] = a
    ok = a.add_user(PIN_SA, "Test TZ standalone", "", "20260825", "20261231",
                    SLOT_SA, HORAIRE_SA)
    verifier(ok, "add_user standalone a renvoyé False")

    z = _zk_brut()
    CTX["z"] = z
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], SLOT_SA,
         "l'adhérent n'est pas dans le groupe du créneau")
    egal(z.SSR_GetGroupTZ(1, SLOT_SA, 0, 0, 0, 0, 0)[1], SLOT_SA,
         "le groupe ne pointe pas sur son calendrier")
    egal(z.SSR_GetUnLockGroup(1, SLOT_SA, 0, 0, 0, 0, 0)[1], SLOT_SA,
         "le groupe n'ouvre pas seul dans sa combinaison")


@suite.test("standalone : le calendrier porte les bonnes plages, week-end fermé")
def _():
    from services.zkem_timezone import decoder_semaine
    jours = decoder_semaine(CTX["z"].GetTZInfo(1, SLOT_SA, "")[1])
    egal(jours["monday"], "06:00-12:00", "lundi")
    egal(jours["friday"], "06:00-12:00", "vendredi")
    # Le piège qui compte : un jour non cité doit être FERMÉ. S'il ressortait
    # ouvert, une activité du matin ouvrirait la porte le week-end.
    egal(jours["saturday"], None, "samedi doit être fermé")
    egal(jours["sunday"], None, "dimanche doit être fermé")


@suite.test("standalone : la période de validité est posée en plus du créneau")
def _():
    ok, _, _, debut, fin = CTX["z"].GetUserValidDate(1, PIN_SA)
    verifier(ok, "aucune période de validité sur l'adhérent")
    verifier(debut.startswith("2026-8-25"), f"début inattendu : {debut}")
    verifier(fin.startswith("2026-12-31"), f"fin inattendue : {fin}")


@suite.test("standalone : modifier la timezone réécrit LE MÊME créneau")
def _():
    # L'objectif métier : le gérant change les horaires, tous les adhérents du
    # groupe suivent, sans qu'on repousse un seul accès.
    from services.zkem_timezone import encoder_semaine, decoder_semaine
    z = CTX["z"]
    z.EnableDevice(1, False)
    try:
        verifier(z.SetTZInfo(1, SLOT_SA, encoder_semaine(HORAIRE_MODIFIE)),
                 "réécriture du calendrier KO")
        z.RefreshData(1)
    finally:
        z.EnableDevice(1, True)

    jours = decoder_semaine(z.GetTZInfo(1, SLOT_SA, "")[1])
    egal(jours["monday"], "07:30-11:00", "les nouveaux horaires ne sont pas appliqués")
    egal(jours["saturday"], "09:00-13:00", "le samedi ouvert n'est pas appliqué")
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], SLOT_SA,
         "l'adhérent a changé de groupe alors qu'on n'a touché qu'au calendrier")


@suite.test("standalone : créneau 1 (défaut) -> groupe 1, aucun calendrier écrit")
def _():
    from services.zkem_timezone import HORAIRE_24_7
    z = CTX["z"]
    avant = z.GetTZInfo(1, 1, "")[1]
    verifier(CTX["sa"].add_user(PIN_SA, "Test TZ standalone", "",
                                "20260825", "20261231", 1, None),
             "add_user avec le créneau par défaut a renvoyé False")
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], 1, "l'adhérent doit revenir en groupe 1")
    egal(z.GetTZInfo(1, 1, "")[1], avant,
         "le calendrier par défaut a été réécrit alors qu'il ne devait pas l'être")
    egal(avant, HORAIRE_24_7, "le calendrier 1 n'est plus le 24/7 d'usine")


@suite.test("standalone : sans créneau (cloud pas à jour) -> comportement inchangé")
def _():
    # Cinq arguments, comme avant la modification : l'adhérent doit rester en
    # groupe 1, donc conserver son accès permanent.
    z = CTX["z"]
    verifier(CTX["sa"].add_user(PIN_SA, "Test TZ standalone", "",
                                "20260825", "20261231"),
             "add_user à 5 arguments a renvoyé False")
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], 1,
         "sans créneau, l'adhérent doit rester en groupe 1")


@suite.test("standalone : horaire illisible -> repli sur le défaut, pas de porte ouverte au hasard")
def _():
    z = CTX["z"]
    verifier(CTX["sa"].add_user(PIN_SA, "Test TZ standalone", "",
                                "20260825", "20261231", SLOT_SA,
                                {"monday": "6h à midi"}),
             "un horaire illisible ne doit pas faire échouer l'ajout")
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], 1,
         "un horaire illisible doit replier sur le groupe par défaut")


@suite.test("standalone : UPDATE_TIMEZONE change l'horaire sans déplacer l'adhérent")
def _():
    # Le cas qui justifie l'opération : sans elle, il faudrait repousser l'accès
    # de chaque adhérent du club pour propager un changement d'horaire.
    from services.zkem_timezone import decoder_semaine
    a, z = CTX["sa"], CTX["z"]

    # On remet l'adhérent dans le créneau, les tests précédents l'ont ramené au 1.
    verifier(a.add_user(PIN_SA, "Test TZ standalone", "", "20260825", "20261231",
                        SLOT_SA, HORAIRE_SA),
             "remise en place du créneau avant l'essai")
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], SLOT_SA, "l'adhérent est dans le créneau")

    verifier(a.update_timezone(SLOT_SA, HORAIRE_MODIFIE),
             "update_timezone a renvoyé False")

    jours = decoder_semaine(z.GetTZInfo(1, SLOT_SA, "")[1])
    egal(jours["monday"], "07:30-11:00", "lundi porte le nouvel horaire")
    egal(jours["saturday"], "09:00-13:00", "samedi est désormais ouvert")
    egal(jours["tuesday"], None, "mardi, retiré de l'horaire, est refermé")
    egal(z.GetUserGroup(1, int(PIN_SA), 0)[1], SLOT_SA,
         "l'adhérent n'a PAS changé de groupe — c'est tout l'intérêt")
    egal(z.SSR_GetGroupTZ(1, SLOT_SA, 0, 0, 0, 0, 0)[1], SLOT_SA,
         "le groupe pointe toujours sur son calendrier")


@suite.test("standalone : UPDATE_TIMEZONE refuse un horaire illisible sans rien casser")
def _():
    from services.zkem_timezone import decoder_semaine
    a, z = CTX["sa"], CTX["z"]
    avant = z.GetTZInfo(1, SLOT_SA, "")[1]
    verifier(not a.update_timezone(SLOT_SA, {"monday": "6h à midi"}),
             "un horaire illisible doit renvoyer False")
    egal(z.GetTZInfo(1, SLOT_SA, "")[1], avant,
         "et surtout laisser le calendrier existant intact")


@suite.test("standalone : UPDATE_TIMEZONE sur le créneau 1 est sans effet")
def _():
    from services.zkem_timezone import HORAIRE_24_7
    z = CTX["z"]
    verifier(CTX["sa"].update_timezone(1, {"monday": "09:00-10:00"}),
             "une demande sur le créneau par défaut doit être acceptée sans effet")
    egal(z.GetTZInfo(1, 1, "")[1], HORAIRE_24_7,
         "le calendrier 1 reste le 24/7 d'usine")


@suite.test("standalone : ménage du PIN et du créneau de test")
def _():
    from services.zkem_timezone import HORAIRE_24_7
    z = CTX["z"]
    z.EnableDevice(1, False)
    try:
        verifier(z.SSR_DeleteEnrollData(1, PIN_SA, 12),
                 "suppression du PIN de test KO")
        # On rend le créneau 4 tel qu'on l'a trouvé : calendrier 24/7, groupe
        # et combinaison remis à zéro. Laisser un groupe orphelin réduirait la
        # réserve de créneaux pour rien.
        z.SetTZInfo(1, SLOT_SA, HORAIRE_24_7)
        z.SSR_SetGroupTZ(1, SLOT_SA, 0, 0, 0, 0, 0)
        z.SSR_SetUnLockGroup(1, SLOT_SA, 0, 0, 0, 0, 0)
        z.RefreshData(1)
    finally:
        z.EnableDevice(1, True)
        z.Disconnect()


if __name__ == "__main__":
    import pythoncom
    pythoncom.CoInitialize()
    controle_prealable()
    sys.exit(0 if suite.executer() else 1)
