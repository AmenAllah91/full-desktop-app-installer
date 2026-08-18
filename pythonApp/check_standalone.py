"""
Controle prealable du chemin STANDALONE_NEW_FIRMWARE (faciales ZKEM).

A lancer AVANT de deployer sur les postes empiregym, sur une machine allumee :

    venv\\Scripts\\python.exe check_standalone.py 192.168.1.201

Il exerce le VRAI code de l'application (ZkemAdapter, zk_sdk_lock, zkem_last_error),
pas une reimplementation : ce qui passe ici passera dans le pont.

Lecture seule : aucune ecriture sur la pointeuse.
"""
import sys
import time
import logging

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

IP = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4370

import pythoncom
pythoncom.CoInitialize()

from services.common import tcp_reachable, zk_sdk_lock          # noqa: E402
from services.zkem_adapter import ZkemAdapter, zkem_last_error  # noqa: E402


class Machine:
    """Fiche minimale, comme celle que le pont recoit du REST."""
    def __init__(self, ip, port):
        self.id = 0
        self.addresseip = ip
        self.port = port
        self.type = "STANDALONE_NEW_FIRMWARE"
        self.alias = "controle"
        self.comKey = None
        self.statut = "Active"


etapes = []


def etape(nom, ok, detail=""):
    etapes.append((nom, ok))
    print(f"  {'OK   ' if ok else 'ECHEC'} {nom}" + (f" — {detail}" if detail else ""))


print(f"\n=== Controle standalone {IP}:{PORT} ===\n")

# 1. Joignabilite
joignable = tcp_reachable(IP, PORT)
etape("joignable en TCP", joignable,
      "" if joignable else "machine eteinte ou hors du reseau — inutile d'aller plus loin")
if not joignable:
    sys.exit(1)

# 2. Connexion SDK
adapter = ZkemAdapter(Machine(IP, PORT))
try:
    connecte = adapter.connect()
    etape("connexion SDK (Connect_Net)", connecte,
          "" if connecte else f"derniere erreur SDK = {zkem_last_error(adapter.zk)}")
except Exception as exc:
    etape("connexion SDK (Connect_Net)", False, f"{type(exc).__name__}: {exc}")
    connecte = False

if not connecte:
    print("\nLe SDK ne repond pas. Verifier l'alimentation, l'IP et le port.")
    sys.exit(1)

# 3. Lecture d'un parametre (prouve que la session sert vraiment a quelque chose)
try:
    with zk_sdk_lock:
        serie = adapter.zk.GetSerialNumber(1)
    etape("lecture d'un parametre", bool(serie), f"numero de serie = {serie}")
except Exception as exc:
    etape("lecture d'un parametre", False, f"{type(exc).__name__}: {exc}")

# 4. Comptage des utilisateurs deja presents
try:
    with zk_sdk_lock:
        adapter.zk.ReadAllUserID(1)
        n = 0
        while True:
            res = adapter.zk.SSR_GetAllUserInfo(1)
            if not res or not res[0]:
                break
            n += 1
            if n > 5000:
                break
    etape("lecture de la table utilisateurs", True, f"{n} utilisateur(s)")
except Exception as exc:
    etape("lecture de la table utilisateurs", False, f"{type(exc).__name__}: {exc}")

# 5. Ecoute temps reel — badgez pendant ce temps
DUREE = 30
print(f"\n  >>> BADGEZ SUR LA MACHINE dans les {DUREE} prochaines secondes <<<\n")
recu = []
try:
    import win32com.client

    class Evt:
        def OnAttTransactionEx(self, pin, isValid, attState, verifyMethod,
                               y, m, d, h, mi, s, workcode):
            recu.append(pin)
            print(f"       pointage recu : pin={pin} a {y}-{m:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")

    with zk_sdk_lock:
        evenements = win32com.client.WithEvents(adapter.zk, Evt)
        inscrit = adapter.zk.RegEvent(1, 0xFFFF)
    etape("inscription aux evenements (RegEvent)", bool(inscrit),
          "" if inscrit else f"erreur SDK = {zkem_last_error(adapter.zk)}")

    fin = time.time() + DUREE
    while time.time() < fin:
        with zk_sdk_lock:
            pythoncom.PumpWaitingMessages()
        time.sleep(0.05)

    etape("pointage recu en temps reel", bool(recu),
          f"{len(recu)} evenement(s)" if recu else "aucun badge detecte (avez-vous badge ?)")
except Exception as exc:
    etape("ecoute temps reel", False, f"{type(exc).__name__}: {exc}")

try:
    adapter.disconnect()
except Exception:
    pass

# Verdict
print("\n=== Verdict ===")
bloquants = [n for n, ok in etapes if not ok and "temps reel" not in n]
if bloquants:
    print("  ECHEC sur : " + ", ".join(bloquants))
    print("  Ne pas deployer avant d'avoir compris ces points.")
    sys.exit(1)
if not recu:
    print("  Le SDK repond correctement, mais aucun badge n'a ete detecte.")
    print("  Relancer en badgant, sinon le temps reel reste non verifie.")
    sys.exit(2)
print("  Chemin standalone valide de bout en bout : SDK, session, temps reel.")
sys.exit(0)
