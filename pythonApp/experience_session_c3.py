"""Qu'est-ce qui tue une session C3 ? — expérience en trois phases.

Tout le pont repose sur deux affirmations mesurées en juin :

  1. « Une session C3 vit ~3,5 s (14 appels de GetRTLog), puis renvoie -2
     définitivement. »
  2. « Seule la première commande qui suit un Connect aboutit ; les suivantes
     expirent. »

C'est ce qui justifie de reconnecter avant CHAQUE commande et de renouveler la
session ~9 fois par minute, 24h/24 — soit, chez vikingsgym, 1887 cycles de
reconnexion en trois jours.

Le diagnostic du 20/08 sur le banc a montré une session vivante après 30 s et
119 appels de GetRTLog. L'affirmation 1 ne tient donc pas SEULE. Mais ce test
ne faisait tourner qu'un thread sur une session neuve, alors que la production
entrelace le thread temps réel et le thread de commandes sur le MÊME handle.

On sépare donc les trois causes possibles :

  A. temps réel seul        -> le temps qui passe suffit-il à tuer la session ?
  B. commandes seules       -> la 2e commande d'une session échoue-t-elle ?
  C. les deux entrelacés    -> le motif exact de la production

Lecture seule : GetRTLog et GetDeviceData n'écrivent rien dans le panneau.

    venv\\Scripts\\python.exe experience_session_c3.py --ip 192.168.1.205

⚠️ pythonApp doit être arrêté : un C3 ne délivre qu'UN SEUL handle à la fois.
"""
import argparse
import ctypes
import socket
import sys
import threading
import time
from ctypes import c_char_p, c_int, c_void_p, create_string_buffer

_TTY = sys.stdout.isatty()
VERT = "\033[92m" if _TTY else ""
ROUGE = "\033[91m" if _TTY else ""
JAUNE = "\033[93m" if _TTY else ""
GRAS = "\033[1m" if _TTY else ""
RAZ = "\033[0m" if _TTY else ""

CONNECT_TIMEOUT_MS = 20000
BUF_RT = 64 * 1024
BUF_CMD = 1024 * 1024
pl = None


def charger_sdk(chemin):
    global pl
    pl = ctypes.CDLL(chemin)
    pl.Connect.argtypes = [c_char_p]
    pl.Connect.restype = c_void_p
    pl.Disconnect.argtypes = [c_void_p]
    pl.GetRTLog.argtypes = [c_void_p, c_char_p, c_int]
    pl.GetRTLog.restype = c_int
    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int,
                                 c_char_p, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int
    pl.PullLastError.restype = c_int


def connect(ip, port):
    params = (f"protocol=TCP,ipaddress={ip},port={port},"
              f"timeout={CONNECT_TIMEOUT_MS},passwd=").encode("utf-8")
    return pl.Connect(params)


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


# ─── Journal d'appels ────────────────────────────────────────────────────

class Journal:
    """Chaque appel SDK est horodaté depuis l'ouverture de la session."""

    def __init__(self):
        self.t0 = time.time()
        self.appels = []          # (t, operation, code)
        self.verrou = threading.Lock()

    def noter(self, op, code):
        with self.verrou:
            self.appels.append((time.time() - self.t0, op, code))

    def de(self, op):
        return [(t, c) for t, o, c in self.appels if o == op]

    def premier_echec(self, op=None):
        for t, o, c in self.appels:
            if (op is None or o == op) and c < 0:
                return t, o, c
        return None

    def resume(self, op):
        a = self.de(op)
        if not a:
            return f"{op:<14} aucun appel"
        ok = sum(1 for _, c in a if c >= 0)
        ko = len(a) - ok
        ligne = f"{op:<14} {len(a):>4} appels   {ok:>4} ok   {ko:>4} echec"
        if ko:
            t_ko = next(t for t, c in a if c < 0)
            n_ok_avant = sum(1 for t, c in a if t < t_ko and c >= 0)
            ligne += f"   1er -2 a t={t_ko:.1f}s apres {n_ok_avant} appels reussis"
        return ligne


# ─── Primitives d'appel ──────────────────────────────────────────────────

def appel_rt(handle, verrou, journal):
    buf = create_string_buffer(BUF_RT)
    with verrou:
        ret = pl.GetRTLog(handle, buf, BUF_RT)
    journal.noter("GetRTLog", ret)
    return ret


def appel_cmd(handle, verrou, journal):
    buf = create_string_buffer(BUF_CMD)
    with verrou:
        ret = pl.GetDeviceData(handle, buf, BUF_CMD, b"user", b"*", b"", b"")
    journal.noter("GetDeviceData", ret)
    return ret


# ─── Phase A : temps réel seul ───────────────────────────────────────────

def phase_a(ip, port, duree):
    print(f"\n{GRAS}PHASE A - temps reel seul ({duree:.0f} s de GetRTLog a 200 ms){RAZ}")
    print("-" * 74)
    print("   Question : le temps qui passe suffit-il a tuer la session ?")
    h = connect(ip, port)
    if not h:
        print(f"   {ROUGE}session non ouverte{RAZ}")
        return None
    j = Journal()
    verrou = threading.RLock()
    try:
        while time.time() - j.t0 < duree:
            if appel_rt(h, verrou, j) < 0:
                break
            time.sleep(0.2)
    finally:
        pl.Disconnect(h)

    print("   " + j.resume("GetRTLog"))
    e = j.premier_echec()
    if e:
        print(f"   {ROUGE}Session morte a t={e[0]:.1f}s.{RAZ}")
    else:
        print(f"   {VERT}Session vivante sur toute la duree.{RAZ}")
    return j


# ─── Phase B : commandes seules ──────────────────────────────────────────

def phase_b(ip, port, n, pause):
    print(f"\n{GRAS}PHASE B - commandes seules ({n} GetDeviceData sur UNE session){RAZ}")
    print("-" * 74)
    print("   Question : la 2e commande d'une session echoue-t-elle vraiment ?")
    print("   C'est l'affirmation qui justifie la reconnexion avant chaque commande.")
    h = connect(ip, port)
    if not h:
        print(f"   {ROUGE}session non ouverte{RAZ}")
        return None
    j = Journal()
    verrou = threading.RLock()
    try:
        for i in range(n):
            ret = appel_cmd(h, verrou, j)
            etat = f"{VERT}ok{RAZ}" if ret >= 0 else f"{ROUGE}ECHEC {ret}{RAZ}"
            print(f"   commande {i + 1:>2}/{n}  t={time.time() - j.t0:5.1f}s  {etat}")
            if ret < 0:
                break
            time.sleep(pause)
    finally:
        pl.Disconnect(h)

    print("   " + j.resume("GetDeviceData"))
    return j


# ─── Phase C : entrelacement, le motif de production ─────────────────────

def phase_c(ip, port, duree, intervalle_cmd):
    print(f"\n{GRAS}PHASE C - entrelacement RT + commandes sur le MEME handle{RAZ}")
    print("-" * 74)
    print(f"   Le motif exact de la production : GetRTLog toutes les 200 ms,")
    print(f"   GetDeviceData toutes les {intervalle_cmd:.0f} s, un seul handle, un seul verrou.")
    h = connect(ip, port)
    if not h:
        print(f"   {ROUGE}session non ouverte{RAZ}")
        return None

    j = Journal()
    verrou = threading.RLock()
    stop = threading.Event()

    def boucle_rt():
        while not stop.is_set():
            if appel_rt(h, verrou, j) < 0:
                # On NE s'arrete pas : on veut savoir si la session revient.
                pass
            time.sleep(0.2)

    def boucle_cmd():
        n = 0
        while not stop.is_set():
            time.sleep(intervalle_cmd)
            if stop.is_set():
                break
            n += 1
            ret = appel_cmd(h, verrou, j)
            etat = f"{VERT}ok{RAZ}" if ret >= 0 else f"{ROUGE}ECHEC {ret}{RAZ}"
            print(f"   commande {n:>2}  t={time.time() - j.t0:5.1f}s  {etat}")

    trt = threading.Thread(target=boucle_rt, daemon=True)
    tcmd = threading.Thread(target=boucle_cmd, daemon=True)
    trt.start()
    tcmd.start()
    try:
        time.sleep(duree)
    finally:
        stop.set()
        trt.join(timeout=25)
        tcmd.join(timeout=25)
        pl.Disconnect(h)

    print("   " + j.resume("GetRTLog"))
    print("   " + j.resume("GetDeviceData"))

    # La session revient-elle d'elle-meme apres un -2 ?
    e = j.premier_echec()
    if e:
        t_ko = e[0]
        apres = [(t, o, c) for t, o, c in j.appels if t > t_ko and c >= 0]
        if apres:
            print(f"   {JAUNE}La session REVIENT : {len(apres)} appels reussis apres le 1er -2")
            print(f"   (le premier a t={apres[0][0]:.1f}s, {apres[0][1]}).{RAZ}")
            print("   L'affirmation « elle ne revient jamais d'elle-meme » est a revoir.")
        else:
            print(f"   {ROUGE}Aucun appel ne reussit apres le 1er -2 : session definitivement morte.{RAZ}")
    return j


# ─── Conclusion ──────────────────────────────────────────────────────────

def conclure(ja, jb, jc):
    print(f"\n{GRAS}CONCLUSION{RAZ}")
    print("=" * 74)

    a_ok = ja is not None and ja.premier_echec() is None
    b_ech = jb.premier_echec("GetDeviceData") if jb else None
    c_ech = jc.premier_echec() if jc else None

    if ja is None or jb is None or jc is None:
        print(f"  {ROUGE}Experience incomplete : une phase n'a pas pu ouvrir de session.{RAZ}")
        print("=" * 74)
        return 1

    # B : combien de commandes passent d'affilee sur une seule session ?
    cmds_ok = sum(1 for _, c in jb.de("GetDeviceData") if c >= 0)

    print(f"  A. temps reel seul      : {'session VIVANTE' if a_ok else 'session morte'}")
    print(f"  B. commandes seules     : {cmds_ok} commande(s) reussie(s) d'affilee sur une session")
    print(f"  C. entrelacement        : {'session morte a t=%.1fs' % c_ech[0] if c_ech else 'session VIVANTE'}")
    print()

    if a_ok and cmds_ok >= 2 and not c_ech:
        print(f"  {VERT}{GRAS}LES DEUX HYPOTHESES DE CONCEPTION SONT INVALIDEES.{RAZ}")
        print("  La session ne meurt ni avec le temps, ni a la 2e commande, ni sous")
        print("  l'entrelacement. La reconnexion avant chaque commande et le")
        print("  renouvellement permanent n'ont plus de justification sur ce materiel.")
        print(f"  {JAUNE}A confirmer sur un 2e panneau avant de toucher au pont.{RAZ}")
        code = 0
    elif a_ok and cmds_ok >= 2 and c_ech:
        print(f"  {JAUNE}{GRAS}C'EST L'ENTRELACEMENT QUI TUE LA SESSION.{RAZ}")
        print("  Seule, chaque boucle tient. Ensemble sur un handle partage, elles")
        print("  la font tomber. Le design actuel est donc justifie dans son principe,")
        print("  mais la vraie cible est le partage du handle, pas la duree de vie.")
        code = 0
    elif a_ok and cmds_ok < 2:
        print(f"  {JAUNE}{GRAS}L'AFFIRMATION SUR LES COMMANDES SE CONFIRME.{RAZ}")
        print(f"  Seulement {cmds_ok} commande(s) par session : reconnecter avant chaque")
        print("  commande reste necessaire. En revanche le temps seul ne tue rien,")
        print("  donc le renouvellement du thread temps reel peut etre allege.")
        code = 0
    else:
        print(f"  {JAUNE}Resultat mixte - a lire phase par phase ci-dessus.{RAZ}")
        code = 0

    print("=" * 74)
    return code


def main():
    p = argparse.ArgumentParser(description="Qu'est-ce qui tue une session C3 ?")
    p.add_argument("--ip", default="192.168.1.205")
    p.add_argument("--port", type=int, default=4370)
    p.add_argument("--dll", default="plcommpro.dll")
    p.add_argument("--duree-a", type=float, default=60.0)
    p.add_argument("--commandes-b", type=int, default=10)
    p.add_argument("--pause-b", type=float, default=1.0)
    p.add_argument("--duree-c", type=float, default=90.0)
    p.add_argument("--intervalle-c", type=float, default=5.0)
    a = p.parse_args()

    print(f"\n{GRAS}QU'EST-CE QUI TUE UNE SESSION C3 ? - {a.ip}:{a.port}{RAZ}")
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 74)

    if pont_en_marche():
        print(f"\n  {ROUGE}{GRAS}ARRET : pythonApp ecoute encore sur le port 9998.{RAZ}")
        print("  Un C3 ne delivre qu'UN SEUL handle a la fois.\n")
        sys.exit(3)

    try:
        charger_sdk(a.dll)
    except OSError as e:
        print(f"  {ROUGE}SDK introuvable : {e}{RAZ}")
        sys.exit(3)

    ja = phase_a(a.ip, a.port, a.duree_a)
    time.sleep(2)
    jb = phase_b(a.ip, a.port, a.commandes_b, a.pause_b)
    time.sleep(2)
    jc = phase_c(a.ip, a.port, a.duree_c, a.intervalle_c)

    sys.exit(conclure(ja, jb, jc))


if __name__ == "__main__":
    main()
