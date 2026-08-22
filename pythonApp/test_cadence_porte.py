"""Le panneau ralentit-il à cause de NOUS ? — test de cadence.

Le client observe que lecteur et porte deviennent mous dès que l'application
tourne. Le pont interroge le panneau 5 fois par seconde (GetRTLog toutes les
200 ms) et lui tient une session ouverte en permanence. Deux causes possibles,
qu'il faut séparer :

  - la CADENCE : 5 appels/s occupent le processeur du panneau ;
  - la SESSION : le seul fait de tenir une session TCP le ralentit.

Ce script ouvre une session et interroge à la cadence qu'on lui donne. En
comparant le ressenti à la porte entre plusieurs cadences — et avec le pont
complètement arrêté — on sait laquelle des deux est en cause.

    venv\\Scripts\\python.exe test_cadence_porte.py --ip 192.168.1.201 --cadence 1.0

Protocole :
  1. pont ARRETE, ce script ARRETE .......... badgez : référence
  2. ce script à --cadence 2.0 .............. badgez : session tenue, peu d'appels
  3. ce script à --cadence 0.2 .............. badgez : la cadence du pont

Si (1) et (2) sont francs et (3) est mou, c'est la cadence : on la ralentit.
Si (2) est déjà mou, c'est la session elle-même : la cadence n'y changera rien.

Lecture seule : GetRTLog ne modifie rien dans le panneau.

⚠️ Le pont (pythonApp.exe) doit être ARRETE : un C3 ne délivre qu'UNE session.
"""
import argparse
import ctypes
import socket
import sys
import time
from ctypes import c_char_p, c_int, c_void_p, create_string_buffer
from datetime import datetime

_TTY = sys.stdout.isatty()
VERT = "\033[92m" if _TTY else ""
ROUGE = "\033[91m" if _TTY else ""
GRAS = "\033[1m" if _TTY else ""
RAZ = "\033[0m" if _TTY else ""

CONNECT_TIMEOUT_MS = 20000
BUF = 64 * 1024


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


def main():
    p = argparse.ArgumentParser(description="Test de cadence d'interrogation d'un C3")
    p.add_argument("--ip", default="192.168.1.201")
    p.add_argument("--port", type=int, default=4370)
    p.add_argument("--dll", default="plcommpro.dll")
    p.add_argument("--cadence", type=float, default=0.2,
                   help="secondes entre deux GetRTLog (0.2 = cadence du pont)")
    p.add_argument("--duree", type=float, default=120.0)
    a = p.parse_args()

    print(f"\n{GRAS}CADENCE D'INTERROGATION - {a.ip}:{a.port}{RAZ}")
    print(f"une interrogation toutes les {a.cadence:.2f} s "
          f"({1 / a.cadence:.1f} par seconde) pendant {a.duree:.0f} s")
    print("=" * 70)

    if pont_en_marche():
        print(f"\n  {ROUGE}{GRAS}ARRET : pythonApp ecoute encore sur le port 9998.{RAZ}")
        print("  Quittez l'application de bureau (Electron la relance sinon),")
        print("  verifiez dans le gestionnaire des taches que pythonApp.exe a disparu.\n")
        sys.exit(3)

    pl = ctypes.CDLL(a.dll)
    pl.Connect.argtypes = [c_char_p]
    pl.Connect.restype = c_void_p
    pl.Disconnect.argtypes = [c_void_p]
    pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
    pl.GetRTLog.restype = c_int

    params = (f"protocol=TCP,ipaddress={a.ip},port={a.port},"
              f"timeout={CONNECT_TIMEOUT_MS},passwd=").encode("utf-8")
    h = pl.Connect(params)
    if not h:
        print(f"  {ROUGE}session non ouverte{RAZ}\n")
        sys.exit(1)

    print(f"  {VERT}session ouverte. Badgez maintenant.{RAZ}")
    print("  (Ctrl+C pour arreter)\n")

    buf = create_string_buffer(BUF)
    t0 = time.time()
    appels = evenements = 0
    dernier = None
    try:
        while time.time() - t0 < a.duree:
            ret = pl.GetRTLog(h, buf, BUF)
            appels += 1
            if ret > 0:
                brut = buf.value.decode(errors="ignore").strip()
                for ligne in brut.splitlines():
                    ligne = ligne.strip()
                    if not ligne or ligne == dernier:
                        continue
                    dernier = ligne
                    evenements += 1
                    champs = ligne.split(",")
                    horo = champs[0] if champs else "?"
                    ecart = ""
                    try:
                        dt = datetime.strptime(horo, "%Y-%m-%d %H:%M:%S")
                        ecart = f"  (lu {(datetime.now() - dt).total_seconds():+.1f} s apres)"
                    except ValueError:
                        pass
                    print(f"  {VERT}BADGE{RAZ} {horo}{ecart}   {ligne[:70]}")
            elif ret < 0:
                print(f"  {ROUGE}GetRTLog={ret}{RAZ} apres {time.time() - t0:.1f} s "
                      f"et {appels} appels")
                break
            time.sleep(a.cadence)
    except KeyboardInterrupt:
        print("\n  arret demande")
    finally:
        pl.Disconnect(h)

    duree = time.time() - t0
    print()
    print("=" * 70)
    print(f"  {appels} interrogations en {duree:.0f} s "
          f"({appels / duree:.1f}/s), {evenements} badge(s) lu(s)")
    print(f"  {GRAS}Comment la porte a-t-elle repondu a cette cadence ?{RAZ}")
    print("=" * 70)


if __name__ == "__main__":
    main()
