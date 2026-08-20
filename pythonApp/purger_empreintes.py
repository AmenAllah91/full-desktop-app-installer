"""
Purge les GABARITS D'EMPREINTES des machines de test.

    venv\\Scripts\\python.exe purger_empreintes.py          # simulation, ne supprime rien
    venv\\Scripts\\python.exe purger_empreintes.py --go     # supprime pour de vrai

Ne touche QUE les empreintes : les utilisateurs, leurs cartes et leurs
autorisations d'accès restent intacts.

Le pont doit être ARRÊTÉ : un C3 ne délivre qu'une session à la fois.
"""
import sys
import time
import ctypes

sys.path.insert(0, r"C:\Users\Public\Documents\workspace\full-desktop-app-installer\pythonApp")

import logging
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

C3 = {"ip": "192.168.1.205", "port": "4370"}
STANDALONE = {"ip": "192.168.2.12", "port": 4370, "comkey": 123456}

GO = "--go" in sys.argv
DOIGTS = range(10)          # 0 a 9


def titre(t):
    print()
    print("=" * 64)
    print(f"  {t}")
    print("=" * 64)


# ---------------------------------------------------------------- C3
def purger_c3():
    titre(f"C3 {C3['ip']} — table templatev10")
    from ctypes import c_void_p, c_char_p, c_int, create_string_buffer
    from services.addAndAuthorizeUser import connect_to_device, plcommpro as pl

    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int, c_char_p, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int
    pl.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
    pl.DeleteDeviceData.restype = c_int

    etat = {"h": None}

    def session():
        """Session fraiche : un C3 ne repond que ~3,5 s par session."""
        if etat["h"]:
            try:
                pl.Disconnect(etat["h"])
            except Exception:
                pass
        etat["h"] = connect_to_device(C3["ip"], C3["port"], max_attempts=3)
        return etat["h"]

    # Tampon alloue AVANT la connexion : une session C3 ne vit que ~3,5 s, et
    # allouer 4 Mo entre le Connect et la lecture suffisait a la perdre.
    #
    # Champs restreints a Pin+FingerID, JAMAIS "*" : les gabarits eux-memes
    # pesent lourd, et les demander fait depasser le delai (ret=-2). Mesure :
    # "*" sur 4 Mo -> -2 ; "Pin\tFingerID" sur 256 Ko -> 149 enregistrements.
    TAILLE = 256 * 1024
    buf = create_string_buffer(TAILLE)

    ret = -2
    for essai in range(3):
        if not session():
            continue
        ret = pl.GetDeviceData(etat["h"], buf, TAILLE,
                               b"templatev10", b"Pin\tFingerID", b"", b"")
        if ret >= 0:
            break
        time.sleep(1)

    if ret < 0:
        print(f"  lecture de templatev10 impossible apres 3 essais (ret={ret})")
        print("  machine eteinte, ou pont encore actif qui monopolise la session")
        return None

    # GetDeviceData rend du CSV avec une ligne d'en-tete :
    #     Pin,FingerID
    #     3040,2
    #     34,0
    # et non des paires "Pin=valeur" comme les conditions de filtrage.
    brut = buf.value.decode(errors="replace")
    lignes = [l.strip() for l in brut.splitlines() if l.strip()]
    pins, paires = [], 0
    if lignes:
        entete = [c.strip().lower() for c in lignes[0].split(",")]
        idx = entete.index("pin") if "pin" in entete else 0
        for ligne in lignes[1:]:
            cols = [c.strip() for c in ligne.split(",")]
            if len(cols) <= idx:
                continue
            paires += 1
            p = cols[idx]
            if p and p not in pins:
                pins.append(p)
    print(f"  {paires} gabarit(s) au total")

    print(f"  {ret} enregistrement(s), {len(pins)} utilisateur(s) avec empreintes")
    if pins:
        print(f"  PIN concernes : {', '.join(pins[:25])}{' ...' if len(pins) > 25 else ''}")

    if not pins:
        print("  rien a purger")
        return 0

    if not GO:
        print(f"\n  [SIMULATION] {len(pins)} PIN seraient purges. Relancer avec --go.")
        return 0

    def supprimer(pin):
        """Supprime les empreintes d'un PIN. Renvoie True, False, ou None si
        la session n'a pas pu etre etablie."""
        # JAMAIS d'appel SDK sans handle valide : passer None a DeleteDeviceData
        # provoque une violation d'acces qui tue le process.
        if not session():
            return None
        cond = f"Pin={pin}\t".encode("utf-8")
        try:
            return pl.DeleteDeviceData(etat["h"], b"templatev10", cond, b"") == 0
        except OSError as exc:
            print(f"      appel SDK en echec : {exc}")
            etat["h"] = None
            return None

    supprimes, a_reprendre = 0, []
    for i, pin in enumerate(pins, 1):
        r = supprimer(pin)
        if r is True:
            supprimes += 1
            print(f"  [{i}/{len(pins)}] PIN {pin} : supprimees")
        else:
            a_reprendre.append(pin)
            motif = "session indisponible" if r is None else "refus du panneau"
            print(f"  [{i}/{len(pins)}] PIN {pin} : echec ({motif}) — a reprendre")
            time.sleep(2)      # laisser le panneau souffler
        time.sleep(0.3)        # 123 reconnexions d'affilee le saturent

    # Deuxieme passe sur les echecs : ils viennent de la saturation, pas d'un
    # refus de fond.
    if a_reprendre:
        print(f"\n  reprise de {len(a_reprendre)} PIN en echec...")
        restants = []
        for pin in a_reprendre:
            time.sleep(1)
            if supprimer(pin) is True:
                supprimes += 1
                print(f"     PIN {pin} : supprimees")
            else:
                restants.append(pin)
        if restants:
            print(f"     toujours en echec : {restants}")

    # Verification
    ret2 = -2
    for _ in range(3):
        if session():
            ret2 = pl.GetDeviceData(etat["h"], buf, TAILLE,
                                    b"templatev10", b"Pin\tFingerID", b"", b"")
            if ret2 >= 0:
                break
        time.sleep(1)
    print(f"\n  apres purge : {ret2 if ret2 >= 0 else '?'} enregistrement(s) restant(s)")
    try:
        pl.Disconnect(etat["h"])
    except Exception:
        pass
    return supprimes


# --------------------------------------------------------- STANDALONE
def purger_standalone():
    titre(f"STANDALONE {STANDALONE['ip']} — SSR_DeleteEnrollDataExt")
    import pythoncom
    pythoncom.CoInitialize()
    from services.zkem_adapter import ZkemAdapter, zkem_last_error

    class Fiche:
        id = 2043
        alias = "standalone 1"
        statut = "Active"
        type = "STANDALONE_NEW_FIRMWARE"
        addresseip = STANDALONE["ip"]
        port = STANDALONE["port"]
        comKey = STANDALONE["comkey"]

    a = ZkemAdapter(Fiche())
    if not a.connect():
        print(f"  connexion impossible (err={zkem_last_error(a.zk)})")
        return None

    zk, mn = a.zk, a.mn

    # Inventaire des utilisateurs
    pins = []
    try:
        zk.ReadAllUserID(mn)
        while True:
            r = zk.SSR_GetAllUserInfo(mn)
            if not r or not r[0]:
                break
            pin = str(r[1]).strip() if len(r) > 1 else ""
            if pin:
                pins.append(pin)
            if len(pins) > 5000:
                break
    except Exception as e:
        print(f"  inventaire des utilisateurs KO : {e}")

    print(f"  {len(pins)} utilisateur(s) sur la machine")
    if pins:
        print(f"  PIN : {', '.join(pins[:25])}{' ...' if len(pins) > 25 else ''}")

    if not pins:
        print("  rien a purger")
        a.disconnect()
        return 0

    if not GO:
        print(f"\n  [SIMULATION] les 10 doigts seraient purges pour {len(pins)} PIN.")
        print("  Relancer avec --go.")
        a.disconnect()
        return 0

    supprimes = 0
    try:
        zk.EnableDevice(mn, False)
        for i, pin in enumerate(pins, 1):
            n = 0
            for doigt in DOIGTS:
                try:
                    if zk.SSR_DeleteEnrollDataExt(mn, str(pin), int(doigt)):
                        n += 1
                except Exception:
                    pass
            supprimes += n
            print(f"  [{i}/{len(pins)}] PIN {pin} : {n} gabarit(s) supprime(s)")
    finally:
        try:
            zk.RefreshData(mn)
            zk.EnableDevice(mn, True)
        except Exception:
            pass

    a.disconnect()
    return supprimes


if __name__ == "__main__":
    print(__doc__)
    if not GO:
        print(">>> MODE SIMULATION — rien ne sera supprime <<<")
    else:
        print(">>> MODE REEL — les gabarits vont etre SUPPRIMES <<<")

    r1 = purger_c3()
    r2 = purger_standalone()

    titre("BILAN")
    print(f"  C3 {C3['ip']:15} : "
          f"{'inaccessible' if r1 is None else str(r1) + ' PIN purge(s)'}")
    print(f"  STANDALONE {STANDALONE['ip']:9} : "
          f"{'inaccessible' if r2 is None else str(r2) + ' gabarit(s) supprime(s)'}")
    if not GO:
        print("\n  Relancer avec --go pour appliquer.")
