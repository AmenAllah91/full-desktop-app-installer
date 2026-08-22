"""INVENTAIRE des utilisateurs d'un C3, sur une plage de PIN — LECTURE SEULE.

Premier des deux scripts de migration vikings. L'ancienne solution y a place les
employes a partir du PIN 40000 ; la notre les attribue a partir de 100000. Avant
de migrer quoi que ce soit, il faut savoir QUI est sur la machine, avec quelle
carte et combien d'empreintes.

Ce script n'ecrit RIEN dans le panneau. Il produit :
  - un rapport lisible a l'ecran ;
  - un fichier JSON horodate, qui sert de SAUVEGARDE et d'entree au script 2.

    venv\\Scripts\\python.exe migration\\c3_inventaire_users.py

Les parametres sont en tete de fichier, a modifier directement.

⚠️ pythonApp doit etre ARRETE : un C3 ne delivre qu'UNE session a la fois, et
   une seconde connexion fait tomber les deux.
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
PIN_MIN = 0              # borne basse incluse
PIN_MAX = 999999         # borne haute incluse
DOSSIER_SORTIE = os.path.dirname(os.path.abspath(__file__))
# ─────────────────────────────────────────────────────────────────────────

CONNECT_TIMEOUT_MS = 20000
TAILLE_USER = 4 * 1024 * 1024
TAILLE_TEMPLATES = 32 * 1024 * 1024   # les gabarits sont volumineux

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
        print("Installez le PullSDK x64 (resources\\SDK.zip -> Register_SDK x64.bat).")
        sys.exit(3)
    pl = ctypes.CDLL(DLL)
    pl.Connect.argtypes = [c_char_p]
    pl.Connect.restype = c_void_p
    pl.Disconnect.argtypes = [c_void_p]
    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int,
                                 c_char_p, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int
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
    """passwd DOIT etre present et vide : l'omettre comme le renseigner donne -14."""
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
    """Lit une table entiere. Retourne le texte brut, ou None sur echec."""
    buf = create_string_buffer(taille)
    ret = pl.GetDeviceData(handle, buf, taille, table, b"*", b"", b"")
    if ret < 0:
        print(f"{ROUGE}  GetDeviceData({table.decode()}) = {ret} "
              f"(PullLastError={pl.PullLastError()}){RAZ}")
        return None
    return buf.value.decode("utf-8", errors="ignore")


def parser_lignes(brut: str):
    """Decoupe une reponse du SDK en dictionnaires.

    Le SDK rend tantot du CSV avec en-tete, tantot du cle=valeur separe par des
    tabulations. Les deux se rencontrent selon la table et le firmware.
    """
    if not brut:
        return []
    lignes = [l for l in brut.replace("\r\n", "\n").split("\n") if l.strip()]
    if not lignes:
        return []

    premiere = lignes[0]
    # Le '=' du base64 (padding) ne doit pas faire passer du CSV pour du KV :
    # on exige une tabulation, ou l'absence de virgule.
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
    # CSV sans en-tete : la premiere ligne est deja une donnee.
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


def main():
    print(f"\n{GRAS}INVENTAIRE C3 — {IP}:{PORT}  |  PIN {PIN_MIN} a {PIN_MAX}{RAZ}")
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}   (LECTURE SEULE)")
    print("=" * 78)

    if pont_en_marche():
        print(f"\n  {ROUGE}{GRAS}ARRET : pythonApp ecoute sur le port 9998.{RAZ}")
        print("  Quittez l'application de bureau et verifiez dans le gestionnaire")
        print("  des taches que pythonApp.exe a disparu — un C3 ne delivre qu'UNE")
        print("  session, la votre ferait tomber la sienne.\n")
        sys.exit(3)

    pl = charger_sdk()
    handle = connecter(pl)
    if not handle:
        print(f"\n{ROUGE}Connexion impossible sur {IP}:{PORT}.{RAZ}\n")
        sys.exit(1)
    print(f"{VERT}Session ouverte.{RAZ}\n")

    try:
        print("Lecture de la table 'user'...")
        brut_users = lire_table(pl, handle, b"user", TAILLE_USER)
        if brut_users is None:
            sys.exit(1)
        users = parser_lignes(brut_users)
        print(f"  {len(users)} fiche(s) au total sur le panneau")

        print("Lecture de la table 'templatev10' (peut prendre du temps)...")
        # Pas de filtre 'Pin=' : selon le firmware il renvoie -101. On lit tout
        # et on regroupe en Python, comme le fait deja le pont.
        brut_tpl = lire_table(pl, handle, b"templatev10", TAILLE_TEMPLATES)
        templates = parser_lignes(brut_tpl) if brut_tpl else []
        print(f"  {len(templates)} gabarit(s) au total")

        print("Lecture de la table 'userauthorize'...")
        brut_auth = lire_table(pl, handle, b"userauthorize", TAILLE_USER)
        autorisations = parser_lignes(brut_auth) if brut_auth else []
        print(f"  {len(autorisations)} autorisation(s) au total")
    finally:
        pl.Disconnect(handle)

    # ─── Regroupement par PIN ────────────────────────────────────────────
    tpl_par_pin = {}
    for t in templates:
        p = entier(t.get("Pin"))
        if p is None:
            continue
        tpl_par_pin.setdefault(p, []).append({
            "fingerId": entier(t.get("FingerID")),
            "valid": t.get("Valid"),
            "size": entier(t.get("Size")),
            "template": (t.get("Template") or "").strip(),
        })

    auth_par_pin = {}
    for a in autorisations:
        p = entier(a.get("Pin"))
        if p is None:
            continue
        auth_par_pin.setdefault(p, []).append({
            "timezoneId": a.get("AuthorizeTimezoneId"),
            "doorId": a.get("AuthorizeDoorId"),
        })

    # ─── Selection de la plage ───────────────────────────────────────────
    retenus = []
    for u in users:
        pin = entier(u.get("Pin"))
        if pin is None or not (PIN_MIN <= pin <= PIN_MAX):
            continue
        empreintes = sorted(tpl_par_pin.get(pin, []),
                            key=lambda x: (x["fingerId"] is None, x["fingerId"]))
        retenus.append({
            "pin": pin,
            "uid": entier(u.get("UID")),
            "cardNo": (u.get("CardNo") or "").strip(),
            "name": (u.get("Name") or "").strip(),
            "password": (u.get("Password") or "").strip(),
            "group": (u.get("Group") or "").strip(),
            "startTime": (u.get("StartTime") or "").strip(),
            "endTime": (u.get("EndTime") or "").strip(),
            "superAuthorize": (u.get("SuperAuthorize") or "").strip(),
            "empreintes": empreintes,
            "autorisations": auth_par_pin.get(pin, []),
        })

    retenus.sort(key=lambda x: x["pin"])

    # ─── Rapport ─────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print(f"{GRAS}{len(retenus)} utilisateur(s) dans la plage {PIN_MIN}-{PIN_MAX}{RAZ}")
    print("=" * 78)
    if retenus:
        # Le nom passe en 2e colonne : c'est par lui qu'on rapproche une fiche
        # machine d'un employe, le PIN seul ne dit rien a personne.
        print(f"{'PIN':>8}  {'NOM':<24}  {'CARTE':>12}  {'EMPR.':>5}  "
              f"{'AUTOR.':>6}  DOIGTS")
        print("-" * 88)
        for r in retenus:
            doigts = ",".join(str(e["fingerId"]) for e in r["empreintes"]
                              if e["fingerId"] is not None)
            couleur = VERT if r["empreintes"] else JAUNE
            nom = r["name"] or "(sans nom)"
            nom_couleur = "" if r["name"] else JAUNE
            print(f"{couleur}{r['pin']:>8}{RAZ}  {nom_couleur}{nom[:24]:<24}{RAZ}  "
                  f"{r['cardNo'] or '-':>12}  {len(r['empreintes']):>5}  "
                  f"{len(r['autorisations']):>6}  {doigts or '-'}")
        print("-" * 88)

        # Le nom est le seul moyen de savoir a QUI appartient un PIN. S'il
        # manque, il faudra rapprocher par le numero de carte — d'ou ce compte,
        # a lire avant de se lancer dans une migration.
        nommes = [r for r in retenus if r["name"]]
        print(f"Nom renseigne sur {len(nommes)}/{len(retenus)} fiche(s).")
        if len(nommes) < len(retenus):
            sans_nom_ni_carte = [r for r in retenus
                                 if not r["name"] and r["cardNo"] in ("", "0", None)]
            if sans_nom_ni_carte:
                print(f"{ROUGE}{len(sans_nom_ni_carte)} fiche(s) sans nom NI carte "
                      f"— rien pour les identifier : "
                      f"{', '.join(str(r['pin']) for r in sans_nom_ni_carte)}{RAZ}")
        sans = [r for r in retenus if not r["empreintes"]]
        if sans:
            print(f"{JAUNE}{len(sans)} sans aucune empreinte : "
                  f"{', '.join(str(r['pin']) for r in sans)}{RAZ}")
        total_e = sum(len(r["empreintes"]) for r in retenus)
        print(f"Total : {total_e} empreinte(s) a deplacer.")
    else:
        print(f"{JAUNE}Aucun utilisateur dans cette plage — verifiez PIN_MIN/PIN_MAX.{RAZ}")

    # ─── Export ──────────────────────────────────────────────────────────
    os.makedirs(DOSSIER_SORTIE, exist_ok=True)
    chemin = os.path.join(
        DOSSIER_SORTIE,
        f"inventaire_{IP.replace('.', '-')}_{PIN_MIN}-{PIN_MAX}"
        f"_{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump({
            "ip": IP,
            "port": PORT,
            "pinMin": PIN_MIN,
            "pinMax": PIN_MAX,
            "date": datetime.now().isoformat(timespec="seconds"),
            "totalFichesPanneau": len(users),
            "utilisateurs": retenus,
        }, f, ensure_ascii=False, indent=2)

    print()
    print(f"{VERT}Sauvegarde ecrite :{RAZ} {chemin}")
    print("Ce fichier contient les gabarits complets : il permet de tout")
    print("reconstruire si une migration tourne mal. Conservez-le.")
    print()


if __name__ == "__main__":
    main()
