"""Migre un PIN vers un autre sur un C3, empreintes comprises.

    migrer_pin.exe <ancien_pin> <nouveau_pin> [ip] [--force]

Exemple :
    migrer_pin.exe 400030 100002
    migrer_pin.exe 400030 100002 192.168.1.205
    migrer_pin.exe 400030 100002 --force

Deplace la fiche, les gabarits d'empreintes et les autorisations de porte de
l'ancien PIN vers le nouveau, puis SUPPRIME l'ancien du panneau.

Ordre des operations, pense pour qu'un echec ne coute jamais de donnees :

  1. lecture de l'ancien PIN : fiche, gabarits, autorisations
  2. SAUVEGARDE dans un JSON horodate — avant la moindre ecriture
  3. refus si le nouveau PIN existe deja (on n'ecrase personne)
  4. creation de la nouvelle fiche, puis des gabarits, puis des autorisations
  5. RELECTURE de controle : le nouveau PIN doit porter autant de gabarits
  6. suppression de l'ancien — uniquement si l'etape 5 est concluante

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

IP_PAR_DEFAUT = "192.168.1.201"
PORT = 4370
CONNECT_TIMEOUT_MS = 20000
TAILLE_USER = 4 * 1024 * 1024
TAILLE_TEMPLATES = 32 * 1024 * 1024

# Le panneau rapporte la taille DECODEE a la lecture mais accepte la longueur
# base64 a l'ecriture — c'est ce que fait add_fingerprint en production, seul
# chemin d'ecriture eprouve sur du materiel. Si un doigt migre ne passe plus,
# c'est le premier suspect.
TAILLE_GABARIT_BASE64 = True

_TTY = sys.stdout.isatty()
VERT = "\033[92m" if _TTY else ""
ROUGE = "\033[91m" if _TTY else ""
JAUNE = "\033[93m" if _TTY else ""
GRAS = "\033[1m" if _TTY else ""
RAZ = "\033[0m" if _TTY else ""

DLL = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "plcommpro.dll")


def sortir(code, message=None):
    if message:
        print(message)
    print()
    sys.exit(code)


def charger_sdk():
    if not os.path.exists(DLL):
        sortir(3, f"{ROUGE}plcommpro.dll introuvable : {DLL}{RAZ}\n"
                  "Installez le PullSDK x64 (Register_SDK x64.bat en administrateur).")
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


def connecter(pl, ip):
    """passwd DOIT etre present et vide : l'omettre comme le renseigner donne -14."""
    params = (f"protocol=TCP,ipaddress={ip},port={PORT},"
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


def parser_lignes(brut):
    """Le SDK rend tantot du CSV avec en-tete, tantot du cle=valeur."""
    if not brut:
        return []
    lignes = [l for l in brut.replace("\r\n", "\n").split("\n") if l.strip()]
    if not lignes:
        return []

    premiere = lignes[0]
    # Le '=' du padding base64 ne doit pas faire passer du CSV pour du KV.
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


def entier(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


class Panneau:
    def __init__(self, pl, handle):
        self.pl, self.handle = pl, handle
        self.users, self.templates, self.autorisations = [], [], []

    def rafraichir(self):
        """Relit les trois tables. Rend False si l'une d'elles a echoue.

        ⚠️ Une lecture ratee rend None, que parser_lignes transforme en liste
        vide — indiscernable d'une table reellement vide. Le danger n'est pas
        theorique : si templatev10 echoue pendant que user reussit, le script
        croit l'employe sans empreinte, en ecrit zero, compare 0 a 0 au controle
        — qui passe — puis SUPPRIME l'ancien PIN. Les empreintes sont perdues,
        et la sauvegarde JSON ne contient rien pour les retrouver.
        Vu en production le 2026-08-22 : les trois lectures ont rendu -2 d'un
        coup. On distingue donc l'echec du vide, et l'appelant s'arrete.
        """
        brut_users = lire_table(self.pl, self.handle, b"user", TAILLE_USER)
        # Pas de filtre 'Pin=' sur templatev10 : selon le firmware il rend -101.
        brut_tpl = lire_table(self.pl, self.handle, b"templatev10", TAILLE_TEMPLATES)
        brut_auth = lire_table(self.pl, self.handle, b"userauthorize", TAILLE_USER)

        if brut_users is None or brut_tpl is None or brut_auth is None:
            return False

        self.users = parser_lignes(brut_users)
        self.templates = parser_lignes(brut_tpl)
        self.autorisations = parser_lignes(brut_auth)
        return True

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
            # `fid is None` et non `not fid` : le doigt 0 existe reellement.
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
    """UID n'est PAS transmis : le panneau l'attribue, le reimposer ecraserait
    la fiche qui le porte deja."""
    champs = [f"Pin={nouveau_pin}"]
    for cle in ("CardNo", "Name", "Password", "Group",
                "StartTime", "EndTime", "SuperAuthorize"):
        champs.append(f"{cle}={(source.get(cle) or '').strip()}")
    ret = pl.SetDeviceData(handle, b"user", "\t".join(champs).encode("utf-8"), None)
    return ret == 0, ret


def ecrire_gabarit(pl, handle, nouveau_pin, g):
    taille = len(g["template"]) if (TAILLE_GABARIT_BASE64 or not g.get("size")) else g["size"]
    data = (f"Size={taille}\tUID={nouveau_pin}\tPin={nouveau_pin}\t"
            f"FingerID={g['fingerId']}\tValid={g.get('valid') or 1}\t"
            f"Template={g['template']}\tResverd=\tEndTag=").encode("utf-8")
    ret = pl.SetDeviceData(handle, b"templatev10", data, None)
    return ret == 0, ret


def ecrire_autorisations(pl, handle, nouveau_pin, autorisations):
    if autorisations:
        lignes = [f"Pin={nouveau_pin}\tAuthorizeTimezoneId={a['timezoneId'] or 1}"
                  f"\tAuthorizeDoorId={a['doorId']}"
                  for a in autorisations if a.get("doorId")]
    else:
        lignes = [f"Pin={nouveau_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1",
                  f"Pin={nouveau_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=2"]
    if not lignes:
        return True, 0
    ret = pl.SetDeviceData(handle, b"userauthorize",
                           "\r\n".join(lignes).encode("utf-8"), None)
    return ret == 0, ret


def supprimer(pl, handle, pin, gabarits):
    erreurs = []
    for g in gabarits:
        cond = f"Pin={pin}\tFingerID={g['fingerId']}".encode("utf-8")
        if pl.DeleteDeviceData(handle, b"templatev10", cond, None) != 0:
            erreurs.append(f"gabarit doigt {g['fingerId']}")
    if pl.DeleteDeviceData(handle, b"userauthorize",
                           f"Pin={pin}".encode("utf-8"), None) != 0:
        erreurs.append("autorisation")
    if pl.DeleteDeviceData(handle, b"user",
                           f"Pin={pin}".encode("utf-8"), None) != 0:
        erreurs.append("fiche")
    return erreurs


def usage():
    sortir(2,
           f"{GRAS}Usage :{RAZ} migrer_pin.exe <ancien_pin> <nouveau_pin> [ip] [--force]\n\n"
           f"  migrer_pin.exe 400030 100002\n"
           f"  migrer_pin.exe 400030 100002 192.168.1.205\n"
           f"  migrer_pin.exe 400030 100002 --force   (sans confirmation)\n\n"
           f"  ip par defaut : {IP_PAR_DEFAUT}")


def main():
    args = [a for a in sys.argv[1:] if a not in ("--force", "-f")]
    force = len(args) != len(sys.argv[1:])

    if len(args) < 2:
        usage()

    ancien, nouveau = entier(args[0]), entier(args[1])
    if ancien is None or nouveau is None:
        sortir(2, f"{ROUGE}Les deux PIN doivent etre des nombres.{RAZ}")
    if ancien == nouveau:
        sortir(2, f"{ROUGE}Les deux PIN sont identiques.{RAZ}")

    ip = args[2] if len(args) > 2 else IP_PAR_DEFAUT

    print(f"\n{GRAS}MIGRATION DE PIN{RAZ}   {ancien} -> {nouveau}")
    print(f"panneau : {GRAS}{ip}:{PORT}{RAZ}   {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 72)

    if pont_en_marche():
        sortir(3, f"\n  {ROUGE}{GRAS}ARRET : pythonApp ecoute sur le port 9998.{RAZ}\n"
                  "  Quittez l'application de bureau : un C3 ne delivre qu'UNE\n"
                  "  session, et la votre ferait tomber la sienne.")

    pl = charger_sdk()
    handle = connecter(pl, ip)
    if not handle:
        sortir(1, f"\n{ROUGE}Connexion impossible sur {ip}:{PORT}.{RAZ}")

    try:
        panneau = Panneau(pl, handle)
        if not panneau.rafraichir():
            sortir(1, f"{ROUGE}Lecture du panneau impossible — le SDK a rendu -2 "
                      f"sur au moins une table.{RAZ}\n"
                      f"Rien n'a ete ecrit. Verifiez que pythonApp est bien ferme, "
                      f"attendez quelques secondes et relancez.")

        fiche = panneau.fiche(ancien)
        if fiche is None:
            sortir(1, f"{ROUGE}Le PIN {ancien} n'existe pas sur ce panneau.{RAZ}")

        # La cible existe presque toujours : c'est la fiche que YoGym a poussee
        # en donnant l'acces a l'employe. Elle est vide d'empreintes, et c'est
        # justement ce qu'on vient completer. Refuser ce cas reviendrait a
        # refuser le scenario nominal.
        #
        # En revanche une cible qui porte DEJA des empreintes n'est pas une
        # fiche neuve : soit elle appartient a quelqu'un d'autre, soit la
        # migration a deja eu lieu. Dans les deux cas on s'arrete.
        fiche_cible = panneau.fiche(nouveau)
        fusion = fiche_cible is not None
        if fusion:
            deja = panneau.gabarits(nouveau)
            if deja:
                sortir(1, f"{ROUGE}Le PIN {nouveau} porte deja "
                          f"{len(deja)} empreinte(s) — doigts "
                          f"{', '.join(str(g['fingerId']) for g in deja)}.{RAZ}\n"
                          f"Ce n'est pas une fiche neuve : migration deja faite, "
                          f"ou PIN appartenant a quelqu'un d'autre.")

        gabarits = panneau.gabarits(ancien)
        autorisations = panneau.autorisations_de(ancien)
        doigts = ", ".join(str(g["fingerId"]) for g in gabarits) or "aucun"
        portes = ", ".join(a["doorId"] for a in autorisations if a.get("doorId")) or "aucune"

        print(f"  nom          : {fiche.get('Name') or '(sans nom)'}")
        print(f"  carte        : {fiche.get('CardNo') or '-'}")
        print(f"  validite     : {fiche.get('StartTime') or '-'} -> {fiche.get('EndTime') or '-'}")
        print(f"  empreintes   : {len(gabarits)}  (doigts {doigts})")
        print(f"  portes       : {portes}")
        print("=" * 72)

        # Sauvegarde AVANT toute ecriture : elle permet de tout reconstruire.
        dossier = os.path.dirname(os.path.abspath(sys.argv[0]))
        chemin = os.path.join(
            dossier, f"sauvegarde_{ancien}_vers_{nouveau}_{datetime.now():%Y%m%d-%H%M%S}.json")
        try:
            with open(chemin, "w", encoding="utf-8") as f:
                json.dump({"ip": ip, "ancienPin": ancien, "nouveauPin": nouveau,
                           "date": datetime.now().isoformat(timespec="seconds"),
                           "fiche": fiche, "gabarits": gabarits,
                           "autorisations": autorisations},
                          f, ensure_ascii=False, indent=2)
            print(f"{VERT}Sauvegarde :{RAZ} {chemin}")
        except OSError as exc:
            sortir(1, f"{ROUGE}Sauvegarde impossible ({exc}) — on n'ecrit rien "
                      f"sans filet.{RAZ}")

        if not force:
            print()
            reponse = input(f"Migrer {ancien} vers {nouveau} et SUPPRIMER {ancien} ? [o/N] ")
            if reponse.strip().lower() not in ("o", "oui", "y", "yes"):
                sortir(0, f"{JAUNE}Annule — rien n'a ete modifie.{RAZ}")

        print()
        if fusion:
            # Les DATES de la cible viennent de YoGym et font autorite : c'est
            # la periode d'acces reelle de l'employe, on n'y touche pas.
            #
            # La CARTE, elle, est ecrasee par celle de l'ancienne fiche : c'est
            # le badge que la personne a physiquement en poche. Garder celle de
            # YoGym la laisserait devant une porte qui ne la reconnait plus.
            carte_ancienne = (fiche.get("CardNo") or "").strip()
            carte_cible = (fiche_cible.get("CardNo") or "").strip()

            if carte_ancienne and carte_ancienne != "0" and carte_ancienne != carte_cible:
                # ⚠️ On reecrit la fiche ENTIERE, pas seulement Pin + CardNo.
                # Verifie sur le banc le 2026-08-22 : une ecriture partielle ne
                # met pas a jour les champs fournis, elle REMPLACE la fiche —
                # StartTime et EndTime sont retombes a 0, detruisant la periode
                # de validite venue de YoGym. On repart donc des champs de la
                # cible, avec la seule carte substituee.
                fusionnee = dict(fiche_cible)
                fusionnee["CardNo"] = carte_ancienne
                ok_carte, ret = ecrire_fiche(pl, handle, nouveau, fusionnee)
                ret = 0 if ok_carte else ret
                if ret == 0:
                    print(f"  {VERT}carte {carte_cible or '-'} -> {carte_ancienne} "
                          f"(reprise de {ancien}){RAZ}")
                else:
                    print(f"  {ROUGE}echec du report de la carte "
                          f"(SetDeviceData={ret}) — la cible garde "
                          f"{carte_cible or '-'}{RAZ}")
            else:
                print(f"  carte {carte_cible or '-'} inchangee")

            print(f"  {VERT}dates {nouveau} conservees "
                  f"({fiche_cible.get('StartTime') or '-'} -> "
                  f"{fiche_cible.get('EndTime') or '-'}){RAZ}")
        else:
            ok, ret = ecrire_fiche(pl, handle, nouveau, fiche)
            if not ok:
                sortir(1, f"{ROUGE}ECHEC creation de la fiche (SetDeviceData={ret}). "
                          f"Rien n'a ete supprime.{RAZ}")
            print(f"  {VERT}fiche {nouveau} creee{RAZ}")

        ecrits = 0
        for g in gabarits:
            ok, ret = ecrire_gabarit(pl, handle, nouveau, g)
            if ok:
                ecrits += 1
            else:
                print(f"  {ROUGE}echec du doigt {g['fingerId']} (SetDeviceData={ret}){RAZ}")
        print(f"  {ecrits}/{len(gabarits)} empreinte(s) ecrite(s)")

        if fusion:
            portes_cible = panneau.autorisations_de(nouveau)
            if portes_cible:
                print(f"  autorisations : conservees "
                      f"({', '.join(x['doorId'] for x in portes_cible if x.get('doorId'))})")
            else:
                ok, ret = ecrire_autorisations(pl, handle, nouveau, autorisations)
                print(f"  autorisations : reprises de {ancien} "
                      f"({'ok' if ok else f'echec ({ret})'})")
        else:
            ok, ret = ecrire_autorisations(pl, handle, nouveau, autorisations)
            print(f"  autorisations : {'ok' if ok else f'echec ({ret})'}")

        # Controle AVANT toute suppression. Si la relecture echoue, on ne peut
        # RIEN affirmer : l'ancien PIN reste en place.
        if not panneau.rafraichir():
            sortir(1, f"\n{ROUGE}Relecture de controle impossible (SDK -2).\n"
                      f"L'ancien PIN {ancien} est CONSERVE : sans controle, le "
                      f"supprimer serait un pari.{RAZ}")
        verif = panneau.gabarits(nouveau)
        if panneau.fiche(nouveau) is None or len(verif) != len(gabarits):
            sortir(1, f"\n{ROUGE}CONTROLE ECHOUE : {len(verif)}/{len(gabarits)} "
                      f"empreinte(s) relues sur {nouveau}.\n"
                      f"L'ancien PIN {ancien} est CONSERVE — corrigez avant de "
                      f"recommencer.{RAZ}")
        # On affiche la fiche telle qu'elle est REELLEMENT apres ecriture : une
        # mise a jour partielle qui blanchirait les dates se verrait ici, plutot
        # que sur une porte fermee.
        finale = panneau.fiche(nouveau) or {}
        print(f"  {VERT}controle ok : {len(verif)} empreinte(s) relues sur {nouveau}{RAZ}")
        print(f"  fiche finale : carte {finale.get('CardNo') or '-'}, "
              f"{finale.get('StartTime') or '-'} -> {finale.get('EndTime') or '-'}")

        erreurs = supprimer(pl, handle, ancien, gabarits)
        if erreurs:
            print(f"  {JAUNE}ancien PIN partiellement supprime : {', '.join(erreurs)}{RAZ}")
            code = 1
        else:
            print(f"  {VERT}ancien PIN {ancien} supprime{RAZ}")
            code = 0

        print("=" * 72)
        print(f"{GRAS}{ancien} -> {nouveau} : "
              f"{'TERMINE' if code == 0 else 'TERMINE AVEC RESERVES'}{RAZ}")
        sortir(code)
    finally:
        try:
            pl.Disconnect(handle)
        except Exception:
            pass


if __name__ == "__main__":
    main()
