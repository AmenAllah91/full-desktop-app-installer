"""MIGRATION d'un PIN vers un autre sur un C3, empreintes comprises.

Second des deux scripts vikings. L'ancienne solution place les employes a partir
de 40000, la notre a partir de 100000 : une fois l'employe cree dans YoGym, on
deplace sa fiche machine vers son nouveau PIN sans lui faire refaire ses
empreintes.

Ordre des operations, pense pour qu'un echec ne coute jamais de donnees :

  1. lecture de l'ancien PIN : fiche, gabarits, autorisations
  2. SAUVEGARDE de tout cela dans un JSON horodate — avant la moindre ecriture
  3. refus si le nouveau PIN existe deja (on n'ecrase personne)
  4. creation de la nouvelle fiche, puis des gabarits, puis des autorisations
  5. RELECTURE de controle : le nouveau PIN doit porter autant de gabarits
  6. suppression de l'ancien PIN — uniquement si l'etape 5 est concluante

    venv\\Scripts\\python.exe migration\\c3_migre_pin.py

Les parametres sont en tete de fichier, a modifier directement.

⚠️ SIMULATION = True par defaut : le script annonce ce qu'il ferait sans rien
   ecrire. Passez-le a False une fois la sortie verifiee.
⚠️ pythonApp doit etre ARRETE : un C3 ne delivre qu'UNE session a la fois.
"""
import ctypes
import json
import os
import socket
import sys
import time
from ctypes import c_char_p, c_int, c_void_p, create_string_buffer
from datetime import datetime

# ─── A MODIFIER ──────────────────────────────────────────────────────────
IP = "192.168.1.205"
PORT = 4370

# Ancien PIN -> nouveau PIN. Le nouveau est celui que YoGym a attribue.
MIGRATIONS = {
    3058: 100050,      # test banc : 3 empreintes dont le doigt 0
}

SIMULATION = True          # True = aucune ecriture, on annonce seulement

# Taille declaree dans le champ Size du gabarit.
#   True  = longueur de la chaine base64, comme le fait add_fingerprint en
#           production — seul chemin d'ecriture jamais verifie sur du materiel.
#   False = Size relu sur le panneau (taille decodee, ~3/4 de la base64).
# Le panneau rapporte la taille DECODEE a la lecture, mais accepte la longueur
# base64 a l'ecriture. Si un doigt migre ne passe plus, basculez a False : c'est
# le premier suspect.
TAILLE_GABARIT_BASE64 = True
SUPPRIMER_ANCIEN = True    # False = on garde l'ancien PIN en place apres copie
DOSSIER_SORTIE = os.path.dirname(os.path.abspath(__file__))
# ─────────────────────────────────────────────────────────────────────────

CONNECT_TIMEOUT_MS = 20000
TAILLE_USER = 4 * 1024 * 1024
TAILLE_TEMPLATES = 32 * 1024 * 1024

_TTY = sys.stdout.isatty()
VERT = "\033[92m" if _TTY else ""
ROUGE = "\033[91m" if _TTY else ""
JAUNE = "\033[93m" if _TTY else ""
GRAS = "\033[1m" if _TTY else ""
RAZ = "\033[0m" if _TTY else ""

DLL = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "plcommpro.dll")


def charger_sdk():
    if not os.path.exists(DLL):
        print(f"{ROUGE}plcommpro.dll introuvable : {DLL}{RAZ}")
        sys.exit(3)
    pl = ctypes.CDLL(DLL)
    pl.Connect.argtypes = [c_char_p]
    pl.Connect.restype = c_void_p
    pl.Disconnect.argtypes = [c_void_p]
    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int,
                                 c_char_p, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int
    pl.SetDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
    pl.SetDeviceData.restype = c_int
    pl.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
    pl.DeleteDeviceData.restype = c_int
    pl.PullLastError.restype = c_int
    return pl


def pont_en_marche(port=9998):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def connecter(pl):
    params = (f"protocol=TCP,ipaddress={IP},port={PORT},"
              f"timeout={CONNECT_TIMEOUT_MS},passwd=").encode("utf-8")
    for essai in range(1, 4):
        h = pl.Connect(params)
        if h:
            return h
        print(f"  tentative {essai}/3 echouee (PullLastError={pl.PullLastError()})")
        time.sleep(2)
    return None


def lire_table(pl, handle, table: bytes, taille: int):
    buf = create_string_buffer(taille)
    ret = pl.GetDeviceData(handle, buf, taille, table, b"*", b"", b"")
    if ret < 0:
        print(f"{ROUGE}  GetDeviceData({table.decode()}) = {ret} "
              f"(PullLastError={pl.PullLastError()}){RAZ}")
        return None
    return buf.value.decode("utf-8", errors="ignore")


def parser_lignes(brut: str):
    """Identique au script d'inventaire — voir ses commentaires."""
    if not brut:
        return []
    lignes = [l for l in brut.replace("\r\n", "\n").split("\n") if l.strip()]
    if not lignes:
        return []

    premiere = lignes[0]
    ressemble_kv = ("=" in premiere) and ("\t" in premiere or "," not in premiere)

    if ressemble_kv:
        out = []
        for l in lignes:
            d = {}
            for champ in l.split("\t"):
                if "=" in champ:
                    k, _, v = champ.partition("=")
                    d[k.strip()] = v.strip()
            if d:
                out.append(d)
        return out

    entetes = [h.strip() for h in premiere.split(",")]
    corps = lignes[1:]
    if entetes and entetes[0].isdigit():
        entetes = ["Size", "UID", "Pin", "FingerID", "Valid",
                   "Template", "Resverd", "EndTag"]
        corps = lignes

    out = []
    for l in corps:
        champs = l.split(",")
        if len(champs) < 2:
            continue
        out.append({entetes[i]: champs[i].strip()
                    for i in range(min(len(entetes), len(champs)))})
    return out


def entier(valeur):
    try:
        return int(str(valeur).strip())
    except Exception:
        return None


class Panneau:
    """Etat lu une seule fois, puis rafraichi apres chaque migration."""

    def __init__(self, pl, handle):
        self.pl = pl
        self.handle = handle
        self.users = []
        self.templates = []
        self.autorisations = []

    def rafraichir(self):
        self.users = parser_lignes(lire_table(self.pl, self.handle, b"user", TAILLE_USER))
        # Pas de filtre 'Pin=' sur templatev10 : selon le firmware il renvoie
        # -101. On lit tout et on filtre en Python, comme le pont.
        self.templates = parser_lignes(
            lire_table(self.pl, self.handle, b"templatev10", TAILLE_TEMPLATES))
        self.autorisations = parser_lignes(
            lire_table(self.pl, self.handle, b"userauthorize", TAILLE_USER))

    def fiche(self, pin):
        for u in self.users:
            if entier(u.get("Pin")) == pin:
                return u
        return None

    def gabarits(self, pin):
        out = []
        for t in self.templates:
            if entier(t.get("Pin")) != pin:
                continue
            tpl = (t.get("Template") or "").strip()
            fid = entier(t.get("FingerID"))
            if not tpl or fid is None:
                continue
            out.append({"fingerId": fid, "template": tpl,
                        "valid": (t.get("Valid") or "1").strip(),
                        "size": entier(t.get("Size"))})
        out.sort(key=lambda x: x["fingerId"])
        return out

    def autorisations_de(self, pin):
        return [{"timezoneId": (a.get("AuthorizeTimezoneId") or "1").strip(),
                 "doorId": (a.get("AuthorizeDoorId") or "").strip()}
                for a in self.autorisations if entier(a.get("Pin")) == pin]


def ecrire_fiche(pl, handle, nouveau_pin, source):
    """Cree la fiche du nouveau PIN a partir de l'ancienne.

    UID n'est PAS transmis : c'est l'identifiant interne du panneau, il
    l'attribue lui-meme. Le reimposer ecraserait la fiche qui le porte deja.
    """
    champs = [f"Pin={nouveau_pin}"]
    for cle, valeur in (("CardNo", source.get("CardNo")),
                        ("Name", source.get("Name")),
                        ("Password", source.get("Password")),
                        ("Group", source.get("Group")),
                        ("StartTime", source.get("StartTime")),
                        ("EndTime", source.get("EndTime")),
                        ("SuperAuthorize", source.get("SuperAuthorize"))):
        champs.append(f"{cle}={(valeur or '').strip()}")
    data = "\t".join(champs).encode("utf-8")
    ret = pl.SetDeviceData(handle, b"user", data, None)
    return ret == 0, ret


def ecrire_gabarit(pl, handle, nouveau_pin, gabarit):
    """Format repris a l'identique de PlcommAdapter.add_fingerprint."""
    # Voir TAILLE_GABARIT_BASE64 en tete de fichier : le panneau rapporte la
    # taille DECODEE a la lecture, mais accepte la longueur base64 a l'ecriture.
    if TAILLE_GABARIT_BASE64 or not gabarit.get("size"):
        taille = len(gabarit["template"])
    else:
        taille = gabarit["size"]

    data = (
        f"Size={taille}\t"
        f"UID={nouveau_pin}\t"
        f"Pin={nouveau_pin}\t"
        f"FingerID={gabarit['fingerId']}\t"
        f"Valid={gabarit.get('valid') or 1}\t"
        f"Template={gabarit['template']}\t"
        f"Resverd=\t"
        f"EndTag="
    ).encode("utf-8")
    ret = pl.SetDeviceData(handle, b"templatev10", data, None)
    return ret == 0, ret


def ecrire_autorisations(pl, handle, nouveau_pin, autorisations):
    """Recopie les portes autorisees ; a defaut, ouvre les portes 1 et 2."""
    if autorisations:
        lignes = [f"Pin={nouveau_pin}\tAuthorizeTimezoneId={a['timezoneId'] or 1}"
                  f"\tAuthorizeDoorId={a['doorId']}"
                  for a in autorisations if a.get("doorId")]
    else:
        lignes = [f"Pin={nouveau_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1",
                  f"Pin={nouveau_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=2"]
    if not lignes:
        return True, 0
    ret = pl.SetDeviceData(handle, b"userauthorize", "\r\n".join(lignes).encode("utf-8"), None)
    return ret == 0, ret


def supprimer_ancien(pl, handle, ancien_pin, gabarits):
    """Retire gabarits, autorisation puis fiche. Dans cet ordre."""
    erreurs = []
    for g in gabarits:
        cond = f"Pin={ancien_pin}\tFingerID={g['fingerId']}".encode("utf-8")
        if pl.DeleteDeviceData(handle, b"templatev10", cond, None) != 0:
            erreurs.append(f"gabarit doigt {g['fingerId']}")
    if pl.DeleteDeviceData(handle, b"userauthorize",
                           f"Pin={ancien_pin}".encode("utf-8"), None) != 0:
        erreurs.append("autorisation")
    if pl.DeleteDeviceData(handle, b"user",
                           f"Pin={ancien_pin}".encode("utf-8"), None) != 0:
        erreurs.append("fiche")
    return erreurs


def main():
    print(f"\n{GRAS}MIGRATION DE PIN — {IP}:{PORT}{RAZ}")
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}")
    if SIMULATION:
        print(f"{JAUNE}{GRAS}MODE SIMULATION — aucune ecriture ne sera faite.{RAZ}")
    else:
        print(f"{ROUGE}{GRAS}MODE REEL — le panneau va etre modifie.{RAZ}")
    print("=" * 78)

    if pont_en_marche():
        print(f"\n  {ROUGE}{GRAS}ARRET : pythonApp ecoute sur le port 9998.{RAZ}")
        print("  Quittez l'application de bureau avant de migrer.\n")
        sys.exit(3)

    if not MIGRATIONS:
        print(f"{ROUGE}MIGRATIONS est vide.{RAZ}")
        sys.exit(1)

    pl = charger_sdk()
    handle = connecter(pl)
    if not handle:
        print(f"\n{ROUGE}Connexion impossible sur {IP}:{PORT}.{RAZ}\n")
        sys.exit(1)
    print(f"{VERT}Session ouverte.{RAZ}")

    resultats = []
    try:
        panneau = Panneau(pl, handle)
        print("Lecture de l'etat du panneau...")
        panneau.rafraichir()
        print(f"  {len(panneau.users)} fiche(s), {len(panneau.templates)} gabarit(s)\n")

        # ── Sauvegarde AVANT toute ecriture ──────────────────────────────
        sauvegarde = []
        for ancien, nouveau in MIGRATIONS.items():
            fiche = panneau.fiche(ancien)
            sauvegarde.append({
                "ancienPin": ancien,
                "nouveauPin": nouveau,
                "fiche": fiche,
                "gabarits": panneau.gabarits(ancien),
                "autorisations": panneau.autorisations_de(ancien),
            })
        chemin = os.path.join(
            DOSSIER_SORTIE,
            f"sauvegarde_avant_migration_{datetime.now():%Y%m%d-%H%M%S}.json")
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump({"ip": IP, "date": datetime.now().isoformat(timespec="seconds"),
                       "simulation": SIMULATION, "entrees": sauvegarde},
                      f, ensure_ascii=False, indent=2)
        print(f"{VERT}Sauvegarde ecrite :{RAZ} {chemin}\n")

        # ── Migrations ───────────────────────────────────────────────────
        for entree in sauvegarde:
            ancien, nouveau = entree["ancienPin"], entree["nouveauPin"]
            fiche, gabarits = entree["fiche"], entree["gabarits"]
            print("-" * 78)
            print(f"{GRAS}{ancien} -> {nouveau}{RAZ}   "
                  f"{(fiche or {}).get('Name') or '(sans nom)'}   "
                  f"carte {(fiche or {}).get('CardNo') or '-'}   "
                  f"{len(gabarits)} empreinte(s)")

            if fiche is None:
                print(f"  {ROUGE}IGNORE : le PIN {ancien} n'existe pas sur le panneau.{RAZ}")
                resultats.append((ancien, nouveau, "absent"))
                continue

            if panneau.fiche(nouveau) is not None:
                print(f"  {ROUGE}IGNORE : le PIN {nouveau} existe DEJA — "
                      f"migrer l'ecraserait.{RAZ}")
                resultats.append((ancien, nouveau, "cible occupee"))
                continue

            if SIMULATION:
                print(f"  {JAUNE}simulation : creation de {nouveau}, "
                      f"{len(gabarits)} gabarit(s), "
                      f"{len(entree['autorisations']) or 2} autorisation(s), "
                      f"puis suppression de {ancien}."
                      f"{'' if SUPPRIMER_ANCIEN else ' (suppression desactivee)'}{RAZ}")
                resultats.append((ancien, nouveau, "simule"))
                continue

            ok, ret = ecrire_fiche(pl, handle, nouveau, fiche)
            if not ok:
                print(f"  {ROUGE}ECHEC creation de la fiche (SetDeviceData={ret}). "
                      f"Rien n'a ete supprime.{RAZ}")
                resultats.append((ancien, nouveau, "echec fiche"))
                continue
            print(f"  {VERT}fiche {nouveau} creee{RAZ}")

            ecrits = 0
            for g in gabarits:
                ok, ret = ecrire_gabarit(pl, handle, nouveau, g)
                if ok:
                    ecrits += 1
                else:
                    print(f"  {ROUGE}echec du gabarit doigt {g['fingerId']} "
                          f"(SetDeviceData={ret}){RAZ}")
            print(f"  {ecrits}/{len(gabarits)} gabarit(s) ecrit(s)")

            ok, ret = ecrire_autorisations(pl, handle, nouveau, entree["autorisations"])
            print(f"  autorisations : {'ok' if ok else f'echec ({ret})'}")

            # ── Controle avant toute suppression ─────────────────────────
            panneau.rafraichir()
            verif = panneau.gabarits(nouveau)
            if panneau.fiche(nouveau) is None or len(verif) != len(gabarits):
                print(f"  {ROUGE}CONTROLE ECHOUE : {len(verif)}/{len(gabarits)} "
                      f"gabarit(s) relus sur {nouveau}. L'ancien PIN est CONSERVE.{RAZ}")
                resultats.append((ancien, nouveau, "controle echoue"))
                continue
            print(f"  {VERT}controle ok : {len(verif)} gabarit(s) relus sur {nouveau}{RAZ}")

            if not SUPPRIMER_ANCIEN:
                print(f"  {JAUNE}ancien PIN conserve (SUPPRIMER_ANCIEN=False){RAZ}")
                resultats.append((ancien, nouveau, "copie, ancien conserve"))
                continue

            erreurs = supprimer_ancien(pl, handle, ancien, gabarits)
            if erreurs:
                print(f"  {JAUNE}ancien PIN partiellement supprime : "
                      f"{', '.join(erreurs)}{RAZ}")
                resultats.append((ancien, nouveau, "migre, nettoyage partiel"))
            else:
                print(f"  {VERT}ancien PIN {ancien} supprime — migration terminee{RAZ}")
                resultats.append((ancien, nouveau, "migre"))

            panneau.rafraichir()
    finally:
        pl.Disconnect(handle)

    print("=" * 78)
    print(f"{GRAS}RECAPITULATIF{RAZ}")
    for ancien, nouveau, etat in resultats:
        couleur = VERT if etat in ("migre", "simule") else (
            JAUNE if "conserve" in etat or "partiel" in etat else ROUGE)
        print(f"  {ancien:>7} -> {nouveau:<7}  {couleur}{etat}{RAZ}")
    print("=" * 78)
    if SIMULATION:
        print(f"{JAUNE}Rien n'a ete ecrit. Passez SIMULATION = False pour appliquer.{RAZ}")
    print()


if __name__ == "__main__":
    main()
