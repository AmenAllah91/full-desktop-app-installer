"""
Ecoute SIMULTANEE des deux machines de la branche 1003 (integration).

Le C3 et la standalone sont surveilles en parallele, dans une seule fenetre :
badgez sur l'une puis sur l'autre, dans n'importe quel ordre.

    venv\\Scripts\\python.exe ecoute_deux.py [duree_en_secondes]

Lecture seule : rien n'est ecrit sur les pointeuses.
"""
import sys
import time
import threading
from datetime import datetime

sys.path.insert(0, r"C:\Users\Public\Documents\workspace\full-desktop-app-installer\pythonApp")

import logging
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

DUREE = int(sys.argv[1]) if len(sys.argv) > 1 else 90
C3_IP, C3_PORT = "192.168.1.205", "4370"
SA_IP, SA_PORT, SA_KEY = "192.168.2.12", 4370, 123456

stop = threading.Event()
detections = []          # (machine, pin, horodatage)
etat = {"c3": "…", "sa": "…"}
verrou = threading.Lock()


def signaler(machine, pin, quand):
    with verrou:
        detections.append((machine, pin, quand))
        print(f"\n  >>> POINTAGE DETECTE — {machine} — pin={pin} — {quand}\n", flush=True)


# ------------------------------------------------------------------ C3
def ecouter_c3():
    from ctypes import c_void_p, c_char_p, c_int, create_string_buffer
    from services.addAndAuthorizeUser import connect_to_device, plcommpro as pl
    from services.MachineMonitor import evenements_du_tampon

    pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
    pl.GetRTLog.restype = c_int

    h = connect_to_device(C3_IP, C3_PORT, max_attempts=3)
    if not h:
        etat["c3"] = "connexion impossible"
        return
    etat["c3"] = "a l'ecoute"

    buf = create_string_buffer(64 * 1024)
    renouv = 0
    while not stop.is_set():
        ret = pl.GetRTLog(h, buf, 64 * 1024)
        if ret > 0:
            brut = buf.value.decode(errors="replace").strip()
            # evenements_du_tampon ecarte les statuts porte/alarme (255) ET
            # decoupe le tampon : GetRTLog colle plusieurs trames quand deux
            # badgeages se suivent de pres. Le try/except qui entourait
            # parse_c3_line ici les perdait tous les deux en silence — soit
            # exactement le cas qu'on veut observer avec cet outil.
            for pin, dt, state, door, card in evenements_du_tampon(brut, C3_IP):
                signaler("C3 192.168.1.205", pin, dt.strftime("%H:%M:%S"))
        elif ret == -2:
            renouv += 1
            try:
                pl.Disconnect(h)
            except Exception:
                pass
            h = connect_to_device(C3_IP, C3_PORT, max_attempts=2)
            if not h:
                etat["c3"] = f"session perdue apres {renouv} renouvellements"
                return
        time.sleep(0.2)

    etat["c3"] = f"termine ({renouv} renouvellement(s))"
    try:
        pl.Disconnect(h)
    except Exception:
        pass


# ------------------------------------------------------- STANDALONE ZKEM
def ecouter_standalone():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()          # apartment COM propre a ce thread
    from services.common import zk_sdk_lock
    from services.zkem_adapter import ZkemAdapter

    class Fiche:
        id = 2043
        alias = "standalone 1"
        statut = "Active"
        type = "STANDALONE_NEW_FIRMWARE"
        addresseip = SA_IP
        port = SA_PORT
        comKey = SA_KEY

    a = ZkemAdapter(Fiche())
    if not a.connect():
        etat["sa"] = "connexion impossible"
        return

    class Evt:
        def OnAttTransactionEx(self, pin, valide, etatv, methode, y, mo, d, h, mi, s, wc):
            signaler("STANDALONE 192.168.2.12", pin,
                     f"{h:02d}:{mi:02d}:{s:02d}")

    with zk_sdk_lock:
        _ev = win32com.client.WithEvents(a.zk, Evt)
        inscrit = a.zk.RegEvent(1, 0xFFFF)
    if not inscrit:
        etat["sa"] = "RegEvent refuse"
        a.disconnect()
        return
    etat["sa"] = "a l'ecoute"

    while not stop.is_set():
        with zk_sdk_lock:
            pythoncom.PumpWaitingMessages()
        time.sleep(0.05)

    etat["sa"] = "termine"
    try:
        a.disconnect()
    except Exception:
        pass


# ------------------------------------------------------------------ main
print("=" * 62)
print("  ECOUTE SIMULTANEE DES DEUX MACHINES")
print("=" * 62)

t1 = threading.Thread(target=ecouter_c3, daemon=True)
t2 = threading.Thread(target=ecouter_standalone, daemon=True)
t1.start()
t2.start()
time.sleep(6)          # laisser les deux sessions s'etablir

print(f"  C3 192.168.1.205        : {etat['c3']}")
print(f"  STANDALONE 192.168.2.12 : {etat['sa']}")
print("=" * 62)
print(f"\n  >>>>>>  BADGEZ SUR LES DEUX MACHINES MAINTENANT  <<<<<<")
print(f"          fenetre de {DUREE} secondes, dans n'importe quel ordre\n", flush=True)

debut = time.time()
while time.time() - debut < DUREE:
    reste = int(DUREE - (time.time() - debut))
    if reste % 15 == 0 and reste > 0:
        print(f"      ... {reste}s restantes ({len(detections)} pointage(s) detecte(s))",
              flush=True)
        time.sleep(1)
    time.sleep(0.5)

stop.set()
time.sleep(2)

print("\n" + "=" * 62)
print("  RESULTAT")
print("=" * 62)
if not detections:
    print("  Aucun pointage detecte.")
    print(f"  etat C3         : {etat['c3']}")
    print(f"  etat standalone : {etat['sa']}")
    sys.exit(1)

par_machine = {}
for machine, pin, quand in detections:
    par_machine.setdefault(machine, []).append((pin, quand))

for machine, liste in par_machine.items():
    print(f"  {machine} : {len(liste)} pointage(s)")
    for pin, quand in liste:
        print(f"      pin={pin} a {quand}")

manquantes = [m for m in ("C3 192.168.1.205", "STANDALONE 192.168.2.12")
              if m not in par_machine]
if manquantes:
    print(f"\n  Aucun pointage sur : {', '.join(manquantes)}")
    sys.exit(2)

print("\n  Les DEUX machines remontent leurs pointages en temps reel.")
sys.exit(0)
