"""Diagnostic terrain d'un panneau C3 — lecture seule par défaut.

Conçu pour être exécuté APRÈS une mise hors tension du contrôleur, sur le
réseau du matériel, afin de répondre à une seule question : la carte de
communication a-t-elle récupéré, ou le panneau est-il à remplacer ?

Chaque mesure est comparée à la référence extraite du log de production
vikingsgym (18–20/08/2026), pour que l'avant/après soit lisible.

    venv\\Scripts\\python.exe diagnostic_c3.py --ip 192.168.1.201

⚠️ pythonApp DOIT être arrêté : un C3 ne délivre qu'UN SEUL handle à la fois.
   Deux sessions ouvertes font tomber les deux. Le script refuse de démarrer
   si le pont écoute encore.
"""
import argparse
import ctypes
import socket
import statistics
import sys
import time
from ctypes import c_char_p, c_int, c_void_p, create_string_buffer
from datetime import datetime

# ─── Référence mesurée dans le log de production ─────────────────────────
# Ce sont les chiffres du panneau EN PANNE. Le but du diagnostic est de
# montrer un écart net avec eux.
REF_PREMIER_APPEL = 20.0    # % de réussite (mesuré 17–24 % selon le repos laissé)
REF_CONNECT = 76.0          # % (367 séquences en échec sur ~1550 tentatives)
REF_DERIVE_HORLOGE = 171    # secondes de retard au 20/08, +18 s/jour

_TTY = sys.stdout.isatty()
VERT = "\033[92m" if _TTY else ""
ROUGE = "\033[91m" if _TTY else ""
JAUNE = "\033[93m" if _TTY else ""
GRAS = "\033[1m" if _TTY else ""
RAZ = "\033[0m" if _TTY else ""

CONNECT_TIMEOUT_MS = 20000  # identique à la production
pl = None


# ─── Chargement du SDK ───────────────────────────────────────────────────

def charger_sdk(chemin):
    """Charge plcommpro.dll avec les signatures exactes de la production."""
    global pl
    try:
        pl = ctypes.CDLL(chemin)
    except OSError as e:
        print(f"  {ROUGE}ECHEC{RAZ} impossible de charger '{chemin}' - {e}")
        print(f"  {JAUNE}Le SDK PullSDK 64 bits n'est pas installe sur ce poste.{RAZ}")
        print("  Installez-le : resources\\SDK.zip -> SDK\\x64\\ puis lancez")
        print("  'Register_SDK x64.bat' en administrateur. Ou passez --dll <chemin>.")
        return False

    pl.Connect.argtypes = [c_char_p]
    pl.Connect.restype = c_void_p
    pl.Disconnect.argtypes = [c_void_p]
    pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
    pl.GetRTLog.restype = c_int
    pl.GetDeviceParam.argtypes = [c_void_p, c_char_p, c_int, c_char_p]
    pl.GetDeviceParam.restype = c_int
    pl.SetDeviceParam.argtypes = [c_void_p, c_char_p]
    pl.SetDeviceParam.restype = c_int
    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int,
                                 c_char_p, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int
    pl.PullLastError.restype = c_int
    return True


def connect(ip, port):
    """Connect avec les paramètres EXACTS de la production.

    passwd doit être présent et vide : l'omettre comme le renseigner donne -14
    sur un C3.
    """
    params = (f"protocol=TCP,ipaddress={ip},port={port},"
              f"timeout={CONNECT_TIMEOUT_MS},passwd=").encode("utf-8")
    return pl.Connect(params)


def erreur():
    try:
        return pl.PullLastError()
    except Exception:
        return "?"


# ─── Horloge ZKTeco ──────────────────────────────────────────────────────

def decoder_datetime(v):
    s = v % 60; v //= 60
    mi = v % 60; v //= 60
    h = v % 24; v //= 24
    d = v % 31 + 1; v //= 31
    mo = v % 12 + 1; v //= 12
    return datetime(v + 2000, mo, d, h, mi, s)


def encoder_datetime(dt):
    return ((((dt.year - 2000) * 12 * 31 + (dt.month - 1) * 31 + (dt.day - 1))
             * 24 + dt.hour) * 60 + dt.minute) * 60 + dt.second


# ─── Contrôle préalable ──────────────────────────────────────────────────

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


# ─── Test 1 : joignabilité TCP brute ─────────────────────────────────────

def test_tcp(ip, port, n):
    """Le panneau répond-il à la poignée de main TCP ? Aucun SDK requis."""
    print(f"\n{GRAS}1. Joignabilite reseau (TCP brut, {n} tentatives){RAZ}")
    print("-" * 74)
    ok, lat = 0, []
    for _ in range(n):
        s = socket.socket()
        s.settimeout(5)
        t0 = time.time()
        try:
            s.connect((ip, port))
            lat.append((time.time() - t0) * 1000)
            ok += 1
        except Exception:
            pass
        finally:
            s.close()
        time.sleep(0.3)

    taux = 100.0 * ok / n
    print(f"   poignees de main reussies : {ok}/{n}  ({taux:.0f} %)")
    if lat:
        print(f"   latence : min={min(lat):.0f} ms  mediane={statistics.median(lat):.0f} ms  max={max(lat):.0f} ms")
    if taux < 100:
        print(f"   {ROUGE}Le panneau refuse ou ignore des connexions TCP.{RAZ}")
        print("   Cable, alimentation ou carte de comm. Rien de logiciel ne peut causer ca.")
    else:
        print(f"   {VERT}Couche reseau saine.{RAZ}")
    return taux


# ─── Test 2 : Connect SDK ────────────────────────────────────────────────

def test_connect(ip, port, n):
    """Le SDK obtient-il un handle ? C'est l'étape qui échouait 367 fois."""
    print(f"\n{GRAS}2. Ouverture de session SDK ({n} tentatives){RAZ}")
    print("-" * 74)
    ok, erreurs, lat = 0, {}, []
    for _ in range(n):
        t0 = time.time()
        h = connect(ip, port)
        dt = (time.time() - t0) * 1000
        if h:
            ok += 1
            lat.append(dt)
            pl.Disconnect(h)
        else:
            e = erreur()
            erreurs[e] = erreurs.get(e, 0) + 1
        time.sleep(0.5)

    taux = 100.0 * ok / n
    print(f"   sessions ouvertes : {ok}/{n}  ({taux:.0f} %)   [reference en panne : {REF_CONNECT:.0f} %]")
    if lat:
        print(f"   duree du Connect : mediane={statistics.median(lat):.0f} ms  max={max(lat):.0f} ms")
    if erreurs:
        print(f"   {ROUGE}PullLastError des echecs : {erreurs}{RAZ}")
        print("   (-2 = delai depasse cote panneau, -14 = mot de passe de comm refuse)")
    return taux


# ─── Test 3 : premier appel après Connect — LE test décisif ──────────────

def test_premier_appel(ip, port, n):
    """Sur un C3 sain, le premier appel qui suit un Connect aboutit toujours.

    En production ce taux est tombe a 17-24 %. C'est cette mesure, et elle
    seule, qui dit si la carte a recupere.
    """
    print(f"\n{GRAS}3. Premier appel apres Connect - TEST DECISIF ({n} cycles){RAZ}")
    print("-" * 74)
    print("   Sur un panneau sain, ce taux doit etre proche de 100 %.")
    ok, ko, codes = 0, 0, {}
    taille = 1024 * 1024
    for i in range(n):
        h = connect(ip, port)
        if not h:
            ko += 1
            codes["Connect KO"] = codes.get("Connect KO", 0) + 1
            time.sleep(1)
            continue
        buf = create_string_buffer(taille)
        ret = pl.GetDeviceData(h, buf, taille, b"user", b"*", b"", b"")
        if ret >= 0:
            ok += 1
        else:
            ko += 1
            codes[ret] = codes.get(ret, 0) + 1
        pl.Disconnect(h)
        if (i + 1) % 5 == 0:
            print(f"   ... {i + 1}/{n} cycles  ({100.0 * ok / (i + 1):.0f} % de reussite)")
        time.sleep(1)

    taux = 100.0 * ok / n
    print()
    print(f"   {GRAS}reussite : {ok}/{n}  ({taux:.0f} %){RAZ}   [reference en panne : {REF_PREMIER_APPEL:.0f} %]")
    if codes:
        print(f"   codes d'echec : {codes}")
    return taux


# ─── Test 4 : durée de vie d'une session ─────────────────────────────────

def test_duree_session(ip, port, limite=30.0):
    """Combien de temps une session survit-elle au polling temps reel ?

    Le pont est construit sur une mesure de ~3,5 s. On la verifie sur CE
    panneau : une valeur tres inferieure expliquerait une partie des echecs.
    """
    print(f"\n{GRAS}4. Duree de vie d'une session sous polling temps reel{RAZ}")
    print("-" * 74)
    h = connect(ip, port)
    if not h:
        print(f"   {ROUGE}ECHEC{RAZ} session non ouverte (PullLastError={erreur()}) - test ignore")
        return None

    buf = create_string_buffer(64 * 1024)
    t0 = time.time()
    appels, evenements = 0, 0
    try:
        while time.time() - t0 < limite:
            ret = pl.GetRTLog(h, buf, 64 * 1024)
            appels += 1
            if ret > 0:
                evenements += 1
            elif ret < 0:
                duree = time.time() - t0
                print(f"   session expiree apres {GRAS}{duree:.1f} s{RAZ} et {appels} appels (GetRTLog={ret})")
                print(f"   {JAUNE}Reference de conception : ~3,5 s / 14 appels.{RAZ}")
                if evenements:
                    print(f"   {evenements} evenement(s) temps reel lu(s) au passage.")
                return duree
            time.sleep(0.2)
    finally:
        pl.Disconnect(h)

    print(f"   {VERT}session toujours vivante apres {limite:.0f} s et {appels} appels.{RAZ}")
    print("   C'est BIEN MIEUX que les ~3,5 s mesures jusqu'ici : le renouvellement")
    print("   permanent de session ne serait alors plus necessaire.")
    return limite


# ─── Test 5 : horloge du panneau ─────────────────────────────────────────

def test_horloge(ip, port, regler=False):
    """L'horloge interne est ~3 min en retard et derive de +18 s/jour."""
    print(f"\n{GRAS}5. Horloge interne du panneau{RAZ}")
    print("-" * 74)
    h = connect(ip, port)
    if not h:
        print(f"   {ROUGE}ECHEC{RAZ} session non ouverte (PullLastError={erreur()}) - test ignore")
        return None

    try:
        buf = create_string_buffer(256)
        ret = pl.GetDeviceParam(h, buf, 256, b"DateTime")
        pc = datetime.now()
        if ret < 0:
            print(f"   {ROUGE}ECHEC{RAZ} GetDeviceParam a renvoye {ret} (PullLastError={erreur()})")
            return None

        brut = buf.value.decode(errors="ignore").strip()
        val = brut.split("=", 1)[1] if "=" in brut else brut
        panneau = decoder_datetime(int(val))
        ecart = (pc - panneau).total_seconds()

        print(f"   heure du panneau : {panneau:%Y-%m-%d %H:%M:%S}")
        print(f"   heure du PC      : {pc:%Y-%m-%d %H:%M:%S}")
        print(f"   {GRAS}ecart : {ecart:+.0f} s{RAZ}   [reference au 20/08 : {REF_DERIVE_HORLOGE:+d} s, derive +18 s/jour]")

        if abs(ecart) > 30:
            print(f"   {ROUGE}Tous les pointages sont enregistres avec cet ecart.{RAZ}")
            if not regler:
                print(f"   {JAUNE}Relancez avec --regler-heure pour remettre le panneau a l'heure.{RAZ}")

        if regler and abs(ecart) > 2:
            payload = f"DateTime={encoder_datetime(datetime.now())}".encode("utf-8")
            r = pl.SetDeviceParam(h, payload)
            if r >= 0:
                print(f"   {VERT}Horloge remise a l'heure du PC.{RAZ}")
            else:
                print(f"   {ROUGE}Echec du reglage : SetDeviceParam={r} (PullLastError={erreur()}){RAZ}")
        return ecart
    finally:
        pl.Disconnect(h)


# ─── Test 6 : intégrité des données ──────────────────────────────────────

def test_donnees(ip, port):
    """Le panneau a-t-il perdu ses utilisateurs pendant la coupure ?"""
    print(f"\n{GRAS}6. Integrite des donnees du panneau{RAZ}")
    print("-" * 74)
    h = connect(ip, port)
    if not h:
        print(f"   {ROUGE}ECHEC{RAZ} session non ouverte - test ignore")
        return None
    try:
        taille = 1024 * 1024
        buf = create_string_buffer(taille)
        ret = pl.GetDeviceData(h, buf, taille, b"user", b"*", b"", b"")
        if ret < 0:
            print(f"   {ROUGE}ECHEC{RAZ} GetDeviceData={ret} (PullLastError={erreur()})")
            return None
        brut = buf.value.decode("utf-8", errors="ignore").strip()
        lignes = [x for x in brut.splitlines() if x.strip()]
        n = max(0, len(lignes) - 1)  # moins l'en-tete
        print(f"   utilisateurs lus dans la table 'user' : {GRAS}{n}{RAZ}")
        if n == 0:
            print(f"   {ROUGE}Table vide - le panneau a perdu ses donnees.{RAZ}")
            print("   Il faudra repousser les acces depuis l'application.")
        else:
            print(f"   {VERT}Donnees presentes, la coupure ne les a pas effacees.{RAZ}")
        return n
    finally:
        pl.Disconnect(h)


# ─── Verdict ─────────────────────────────────────────────────────────────

def verdict(tcp, cnx, premier, duree, ecart, users):
    print(f"\n{GRAS}VERDICT{RAZ}")
    print("=" * 74)

    # La couche reseau prime sur tout le reste : accuser la carte de
    # communication alors que le panneau ne repond meme pas au TCP enverrait
    # sur une fausse piste.
    if tcp is not None and tcp == 0:
        print(f"  {ROUGE}{GRAS}PANNEAU INJOIGNABLE SUR LE RESEAU.{RAZ}")
        print("  Aucune poignee de main TCP n'aboutit : le diagnostic de la carte")
        print("  de communication est impossible tant que ce point n'est pas regle.")
        print("  A verifier dans cet ordre : alimentation du panneau, cable et")
        print("  voyants du port reseau, IP du panneau, et que ce PC est bien sur")
        print("  le meme sous-reseau.")
        print("=" * 74)
        return 3

    if tcp is not None and tcp < 100:
        print(f"  {JAUNE}{GRAS}RESEAU INSTABLE - a traiter avant d'accuser la carte.{RAZ}")
        print(f"  Seulement {tcp:.0f} % des poignees de main TCP aboutissent.")
        print("  Cable, switch ou alimentation. Les mesures qui suivent sont a")
        print("  relire avec cette reserve.")
        print()

    if premier is None:
        print(f"  {ROUGE}Aucune mesure exploitable : le panneau n'a pas repondu.{RAZ}")
        print("  Verifiez l'alimentation, le cable reseau et l'IP.")
        return 1

    if premier >= 90:
        print(f"  {VERT}{GRAS}LE PANNEAU A RECUPERE.{RAZ}")
        print(f"  Premier appel apres Connect : {premier:.0f} % contre {REF_PREMIER_APPEL:.0f} % en panne.")
        print("  La mise hors tension a remis la carte de communication en etat.")
        print("  Surveillez le taux d'echec quelques jours : une carte abimee")
        print("  par une surtension peut se degrader de nouveau.")
        code = 0
    elif premier >= 40:
        print(f"  {JAUNE}{GRAS}PANNEAU PARTIELLEMENT RETABLI - INSTABLE.{RAZ}")
        print(f"  Premier appel : {premier:.0f} %. Mieux que les {REF_PREMIER_APPEL:.0f} % en panne,")
        print("  mais loin des 100 % attendus. Les attributions d'acces resteront lentes.")
        print("  Refaites une coupure plus longue (5 min) et remesurez. Si le taux ne")
        print("  monte pas, prevoyez le remplacement du panneau.")
        code = 1
    else:
        print(f"  {ROUGE}{GRAS}LE PANNEAU EST TOUJOURS EN PANNE.{RAZ}")
        print(f"  Premier appel : {premier:.0f} %, soit le niveau du log de production ({REF_PREMIER_APPEL:.0f} %).")
        print("  La coupure n'a rien change : la carte de communication ne repond plus")
        print("  correctement. C'est un probleme materiel, a faire remplacer sous garantie.")
        code = 2

    print()
    if tcp is not None and tcp < 100:
        print(f"  - Reseau : {tcp:.0f} % de poignees de main TCP seulement. A verifier avant tout.")
    if cnx is not None:
        print(f"  - Ouverture de session SDK : {cnx:.0f} %")
    if duree is not None:
        print(f"  - Duree de vie d'une session : {duree:.1f} s")
    if ecart is not None and abs(ecart) > 30:
        print(f"  - {ROUGE}Horloge : {ecart:+.0f} s d'ecart - les pointages sont mal horodates.{RAZ}")
    elif ecart is not None:
        print(f"  - Horloge : {ecart:+.0f} s, correct.")
    if users is not None:
        print(f"  - Utilisateurs presents dans le panneau : {users}")
    print("=" * 74)
    return code


def main():
    p = argparse.ArgumentParser(description="Diagnostic terrain d'un panneau C3")
    p.add_argument("--ip", default="192.168.1.201")
    p.add_argument("--port", type=int, default=4370)
    p.add_argument("--essais", type=int, default=20,
                   help="cycles du test decisif (defaut 20, ~40 s)")
    p.add_argument("--dll", default="plcommpro.dll")
    p.add_argument("--regler-heure", action="store_true",
                   help="ECRITURE : remet l'horloge du panneau a celle du PC")
    a = p.parse_args()

    print(f"\n{GRAS}DIAGNOSTIC PANNEAU C3 - {a.ip}:{a.port}{RAZ}")
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 74)

    if pont_en_marche():
        print(f"\n  {ROUGE}{GRAS}ARRET : pythonApp ecoute encore sur le port 9998.{RAZ}")
        print("  Un C3 ne delivre qu'UN SEUL handle a la fois : deux sessions")
        print("  ouvertes font tomber les deux et fausseraient toutes les mesures.")
        print("  Fermez l'application de bureau, puis relancez ce script.\n")
        sys.exit(3)

    # La couche reseau se teste sans le SDK : on la mesure meme si la DLL manque.
    tcp = test_tcp(a.ip, a.port, 10)

    if not charger_sdk(a.dll):
        print(f"\n  {JAUNE}Seul le test reseau a pu etre execute.{RAZ}\n")
        sys.exit(3)

    cnx = test_connect(a.ip, a.port, 10)
    premier = test_premier_appel(a.ip, a.port, a.essais)
    duree = test_duree_session(a.ip, a.port)
    ecart = test_horloge(a.ip, a.port, regler=a.regler_heure)
    users = test_donnees(a.ip, a.port)

    sys.exit(verdict(tcp, cnx, premier, duree, ecart, users))


if __name__ == "__main__":
    main()
