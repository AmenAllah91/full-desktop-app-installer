"""
Validation du banc — tenant `empire`, branche 1003 (integration).

Deux modes, VOLONTAIREMENT separes : un C3 ne delivre qu'UNE session a la fois.
Lancer le mode --sdk pendant que le pont tourne ferait tomber les deux.

    # 1. Pont ARRETE — teste directement les machines via le SDK
    venv\\Scripts\\python.exe valider_banc.py --sdk

    # 2. Pont DEMARRE — badge sur les machines, puis verifie la chaine complete
    venv\\Scripts\\python.exe valider_banc.py --chaine

Le mode --sdk n'ecrit rien sur les pointeuses. Le mode --chaine ne fait que lire
Kafka et la base.
"""
import os
import sys
import time
import json
import socket

BASE_REST = "https://integration.yo-club.app/gym-management/public"
TENANT = "empire"
BRANCHE = "1003"
BROKER = "54.38.35.221:9094"
TOPIC = "rt_pointage"
DB_HOST = "54.38.35.221"
DB_NAME = "gymapp_empire"

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def machines_actives():
    """La liste que le pont lui-meme recupere : endpoint public, filtre sur Active."""
    import requests
    url = f"{BASE_REST}/am/gb/{BRANCHE}/{TENANT}"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return r.json()


def joignable(ip, port, delai=3.0):
    s = socket.socket()
    s.settimeout(delai)
    try:
        s.connect((ip, int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


# ------------------------------------------------------------------ SDK
def tester_c3(m, secondes=45):
    """Session, renouvellement sur -2, et une commande de lecture."""
    from services.addAndAuthorizeUser import connect_to_device, plcommpro as pl
    from ctypes import c_void_p, c_char_p, c_int, create_string_buffer

    pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
    pl.GetRTLog.restype = c_int
    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int

    ip, port = m["addresseip"], str(m["port"])
    h = connect_to_device(ip, port, max_attempts=3)
    if not h:
        return False, "connexion SDK impossible"

    buf = create_string_buffer(64 * 1024)
    polls = renouv = evenements = 0
    fin = time.time() + secondes
    print(f"      ecoute {secondes}s (badgez si vous voulez voir un pointage)...")
    while time.time() < fin:
        ret = pl.GetRTLog(h, buf, 64 * 1024)
        polls += 1
        if ret > 0:
            evenements += 1
            print(f"      evenement : {buf.value.decode(errors='replace').strip()[:90]}")
        elif ret == -2:
            renouv += 1
            try:
                pl.Disconnect(h)
            except Exception:
                pass
            h = connect_to_device(ip, port, max_attempts=2)
            if not h:
                return False, f"session non renouvelable apres {renouv} tentatives"
        time.sleep(0.2)

    b = create_string_buffer(256 * 1024)
    ret = pl.GetDeviceData(h, b, 256 * 1024, b"user", b"Pin\tCardNo", b"", b"")
    try:
        pl.Disconnect(h)
    except Exception:
        pass

    if ret < 0:
        return False, f"GetDeviceData KO (ret={ret}) apres {renouv} renouvellement(s)"
    return True, (f"{polls} polls, {renouv} renouvellement(s), {evenements} evenement(s), "
                  f"table utilisateurs lue ({ret})")


def tester_standalone(m, secondes=30):
    """Connexion SDK, parametre, puis ecoute temps reel."""
    import pythoncom
    import win32com.client
    from services.common import zk_sdk_lock
    from services.zkem_adapter import ZkemAdapter, zkem_last_error

    class Fiche:
        id = 0
        alias = "validation"
        statut = "Active"
        type = "STANDALONE_NEW_FIRMWARE"

        def __init__(self, ip, port, ck):
            self.addresseip, self.port, self.comKey = ip, port, ck

    a = ZkemAdapter(Fiche(m["addresseip"], m["port"], m.get("comKey")))
    if not a.connect():
        return False, f"Connect_Net KO (erreur SDK = {zkem_last_error(a.zk)})"

    with zk_sdk_lock:
        serie = a.zk.GetSerialNumber(1)

    recus = []

    class Evt:
        def OnAttTransactionEx(self, pin, valide, etat, methode, y, mo, d, h, mi, s, wc):
            recus.append(pin)
            print(f"      pointage : pin={pin} a {y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")

    with zk_sdk_lock:
        _ev = win32com.client.WithEvents(a.zk, Evt)
        inscrit = a.zk.RegEvent(1, 0xFFFF)
    if not inscrit:
        a.disconnect()
        return False, f"RegEvent KO (erreur SDK = {zkem_last_error(a.zk)})"

    print(f"      ecoute {secondes}s (BADGEZ maintenant)...")
    fin = time.time() + secondes
    while time.time() < fin:
        with zk_sdk_lock:
            pythoncom.PumpWaitingMessages()
        time.sleep(0.05)

    a.disconnect()
    return True, f"serie={serie}, {len(recus)} pointage(s) recu(s) en direct"


def mode_sdk():
    print("=== Mode SDK — le pont doit etre ARRETE ===\n")
    try:
        machines = machines_actives()
    except Exception as e:
        print(f"REST injoignable : {e}")
        return 1
    print(f"{len(machines)} machine(s) active(s) sur la branche {BRANCHE} :\n")

    resultats = []
    for m in machines:
        ip, typ = m["addresseip"], m["type"]
        print(f"  --- {m['id']} {m['alias']!r} {ip}:{m['port']} [{typ}] ---")
        if not joignable(ip, m["port"]):
            print("      ECHEC : injoignable en TCP (machine eteinte ?)\n")
            resultats.append((m["id"], False))
            continue
        print("      OK : joignable")
        try:
            if typ == "C3":
                ok, detail = tester_c3(m)
            elif typ.startswith("STANDALONE"):
                ok, detail = tester_standalone(m)
            else:
                ok, detail = False, f"type non gere : {typ}"
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        print(f"      {'OK' if ok else 'ECHEC'} : {detail}\n")
        resultats.append((m["id"], ok))

    echecs = [i for i, ok in resultats if not ok]
    print("=== Verdict SDK ===")
    if echecs:
        print(f"  ECHEC sur les machines {echecs}")
        return 1
    print("  Toutes les machines repondent correctement.")
    return 0


# --------------------------------------------------------------- CHAINE
def mode_chaine(fenetre=180):
    print("=== Mode chaine — le pont doit etre DEMARRE ===")
    print(f"Fenetre d'observation : {fenetre}s. Badgez sur les machines.\n")

    from confluent_kafka import Consumer, TopicPartition
    import pymysql

    mdp = os.environ.get("BENCH_DB_PASSWORD")
    if not mdp:
        print("BENCH_DB_PASSWORD absent de l'environnement (voir .env.bench).")
        return 1

    def compter_base():
        c = pymysql.connect(host=DB_HOST, user="root", password=mdp,
                            database=DB_NAME, charset="utf8mb4").cursor()
        c.execute("SELECT COUNT(*) FROM pointage WHERE DATE(`date`) = CURDATE()")
        n = c.fetchone()[0]
        c.connection.close()
        return n

    cons = Consumer({"bootstrap.servers": BROKER, "group.id": "validation_banc",
                     "enable.auto.commit": False, "auto.offset.reset": "latest"})
    md = cons.list_topics(TOPIC, timeout=15)
    departs = {}
    for p in md.topics[TOPIC].partitions:
        _, hi = cons.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=10)
        departs[p] = hi
    cons.assign([TopicPartition(TOPIC, p, o) for p, o in departs.items()])

    base_avant = compter_base()
    print(f"  pointages en base aujourd'hui, avant : {base_avant}")
    print("  ecoute de rt_pointage...\n")

    vus = []
    fin = time.time() + fenetre
    while time.time() < fin:
        msg = cons.poll(1.0)
        if msg is None or msg.error():
            continue
        try:
            d = json.loads(msg.value().decode(errors="replace"))
        except Exception:
            continue
        if str(d.get("gym_branch_id")) == BRANCHE:
            vus.append(d)
            print(f"      kafka : pin={d.get('pin')} machine={d.get('id_machine')} "
                  f"ip={d.get('ip_adress')} tenant={d.get('tenant')}")
    cons.close()

    time.sleep(5)
    base_apres = compter_base()
    print(f"\n  pointages en base aujourd'hui, apres : {base_apres} "
          f"(+{base_apres - base_avant})")

    print("\n=== Verdict chaine ===")
    if not vus:
        print("  Aucun message sur rt_pointage pour la branche 1003.")
        print("  -> le pont ne publie pas : verifier qu'il tourne et son KAFKA_BROKER.")
        return 1
    tenants = {d.get("tenant") for d in vus}
    print(f"  {len(vus)} message(s) publie(s), tenant(s) = {tenants}")
    if base_apres > base_avant:
        print("  Chaine complete validee : pointeuse -> pont -> Kafka -> base.")
        return 0
    print("  Messages publies mais RIEN ecrit en base.")
    print("  -> cote gym-management : PIN inconnu, ou consommateur arrete.")
    return 2


if __name__ == "__main__":
    if "--sdk" in sys.argv:
        sys.exit(mode_sdk())
    if "--chaine" in sys.argv:
        sys.exit(mode_chaine())
    print(__doc__)
    sys.exit(1)
