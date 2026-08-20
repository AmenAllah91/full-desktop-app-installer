"""Tests du pont NÉCESSITANT pythonApp démarré ET les 2 pointeuses joignables.

Conditions requises :
  - pythonApp en local sur http://localhost:9998
  - standalone 192.168.2.12 (comKey 123456) connectée
  - C3         192.168.1.205 connectée

Si une condition manque, le script s'arrête AVANT tout test en nommant
précisément ce qui manque : un test rouge par absence de matériel serait
indiscernable d'une vraie régression.

    venv\\Scripts\\python.exe tests\\test_avec_machines.py

Les tests sont volontairement NON DESTRUCTIFS : aucune empreinte n'est
supprimée, aucune porte ouverte. On vérifie les contrats des routes que nous
avons construites, pas le comportement physique des pointeuses.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
from harnais import Suite, verifier, egal, parmi, ROUGE, VERT, JAUNE, GRAS, RAZ  # noqa: E402

BASE = "http://localhost:9998"
TIMEOUT = 20

MACHINES_ATTENDUES = {
    "192.168.2.12": "standalone (comKey 123456)",
    "192.168.1.205": "C3",
}

# Rempli par le contrôle préalable, réutilisé par les tests
CONTEXTE = {"branche": None, "machines": {}}


# ─── Contrôle préalable ──────────────────────────────────────────────────

def controle_prealable():
    """Vérifie les conditions AVANT de lancer le moindre test."""
    manquants = []

    print(f"{GRAS}Controle prealable des conditions de test{RAZ}")
    print("-" * 76)

    # 1. pythonApp répond-il ?
    try:
        r = requests.get(f"{BASE}/health", timeout=6)
        verifier(r.status_code == 200, f"/health a repondu {r.status_code}")
        print(f"  {VERT}ok{RAZ}    pythonApp repond sur {BASE}")
    except Exception as e:
        print(f"  {ROUGE}ECHEC{RAZ} pythonApp ne repond pas sur {BASE} ({type(e).__name__})")
        manquants.append(
            f"pythonApp ne repond pas sur {BASE} — demarrez l'application de bureau "
            f"(ou 'python main.py') avant de relancer ces tests"
        )
        _abandonner(manquants)

    # 2. Branche courante configurée
    try:
        conf = requests.get(f"{BASE}/config/gymBranchId", timeout=6).json()
        CONTEXTE["branche"] = conf.get("gymBranchId")
        print(f"  {VERT}ok{RAZ}    branche courante : {CONTEXTE['branche']}")
    except Exception as e:
        manquants.append(f"impossible de lire la branche courante ({type(e).__name__})")

    # 3. Les 2 machines sont-elles présentes ET connectées ?
    try:
        devices = requests.get(f"{BASE}/api/devices", timeout=10).json().get("devices", [])
    except Exception as e:
        print(f"  {ROUGE}ECHEC{RAZ} /api/devices injoignable ({type(e).__name__})")
        manquants.append(f"/api/devices injoignable ({type(e).__name__})")
        _abandonner(manquants)

    par_ip = {d.get("ip"): d for d in devices}
    for ip, libelle in MACHINES_ATTENDUES.items():
        d = par_ip.get(ip)
        if d is None:
            print(f"  {ROUGE}ECHEC{RAZ} machine absente : {libelle} — {ip}")
            manquants.append(f"machine ABSENTE de la configuration : {libelle} — {ip}")
        elif not d.get("connected"):
            print(f"  {ROUGE}ECHEC{RAZ} machine NON CONNECTEE : {libelle} — {ip}")
            manquants.append(f"machine NON CONNECTEE : {libelle} — {ip} (verifiez qu'elle est allumee et joignable)")
        else:
            CONTEXTE["machines"][ip] = d
            print(f"  {VERT}ok{RAZ}    machine connectee : {libelle} — {ip} (id={d.get('id')})")

    if manquants:
        _abandonner(manquants)

    print("-" * 76)
    print(f"{VERT}Conditions reunies.{RAZ}\n")


def _abandonner(manquants):
    print("-" * 76)
    print(f"\n{ROUGE}{GRAS}LES CONDITIONS DE TEST NE SONT PAS RESPECTEES{RAZ}\n")
    for m in manquants:
        print(f"  - {m}")
    print(f"\n{JAUNE}Aucun test n'a ete execute.{RAZ}")
    print("Les tests independants du materiel restent lancables :")
    print("    venv\\Scripts\\python.exe tests\\test_sans_machines.py\n")
    sys.exit(2)


# ─── Tests ───────────────────────────────────────────────────────────────

suite = Suite("PONT — routes dependantes des machines")

PIN_TEST = 3040  # adhérent de test


@suite.test("/api/devices expose les champs attendus pour chaque machine")
def _():
    d = requests.get(f"{BASE}/api/devices", timeout=10).json()
    verifier("devices" in d, "cle 'devices' absente")
    for ip in MACHINES_ATTENDUES:
        m = next((x for x in d["devices"] if x.get("ip") == ip), None)
        verifier(m is not None, f"machine {ip} absente")
        for champ in ("id", "alias", "ip", "port", "connected"):
            verifier(champ in m, f"champ '{champ}' manquant pour {ip}")


@suite.test("/api/machines/status renvoie un etat par machine")
def _():
    d = requests.get(f"{BASE}/api/machines/status", timeout=10).json()
    verifier("machines" in d, "cle 'machines' absente")
    ips = {m.get("ip") for m in d["machines"]}
    for ip in MACHINES_ATTENDUES:
        verifier(ip in ips, f"machine {ip} absente du statut")


@suite.test("/getFingerprints repond pour chaque machine connectee")
def _():
    # Contrat de la route utilisee par l'import d'empreintes. On n'exige pas
    # que l'adherent AIT des empreintes : seulement une reponse structuree.
    for ip, machine in CONTEXTE["machines"].items():
        url = f"{BASE}/getFingerprints/{PIN_TEST}/{CONTEXTE['branche']}/{machine['id']}"
        r = requests.get(url, timeout=TIMEOUT)
        parmi(r.status_code, (200, 404, 500), f"code inattendu pour {ip}")
        if r.status_code == 200:
            body = r.json()
            verifier("fingerprints" in body, f"cle 'fingerprints' absente pour {ip}")
            verifier(isinstance(body["fingerprints"], list), f"'fingerprints' doit etre une liste ({ip})")
            for fp in body["fingerprints"]:
                verifier("fingerId" in fp and "template" in fp,
                         f"gabarit mal forme depuis {ip}: {fp.keys()}")


@suite.test("/getFingerprints refuse une gymBranchId etrangere (400)")
def _():
    machine = next(iter(CONTEXTE["machines"].values()))
    mauvaise = int(CONTEXTE["branche"]) + 999
    r = requests.get(f"{BASE}/getFingerprints/{PIN_TEST}/{mauvaise}/{machine['id']}", timeout=TIMEOUT)
    egal(r.status_code, 400, "une branche etrangere doit etre refusee")


@suite.test("/fingerprint/push — champs manquants -> 400")
def _():
    r = requests.post(f"{BASE}/fingerprint/push", json={}, timeout=TIMEOUT)
    egal(r.status_code, 400, "un corps vide doit etre refuse")
    verifier("error" in r.json(), "le message d'erreur doit etre explicite")


@suite.test("/fingerprint/push — pin sans template -> 400")
def _():
    r = requests.post(f"{BASE}/fingerprint/push", json={"pin": PIN_TEST}, timeout=TIMEOUT)
    egal(r.status_code, 400, "template manquant doit etre refuse")


@suite.test("/fingerprint/push — template non base64 -> 400 (et non 500)")
def _():
    r = requests.post(f"{BASE}/fingerprint/push",
                      json={"pin": PIN_TEST, "fingerId": 1, "template": "ceci n'est pas du base64 !!"},
                      timeout=TIMEOUT)
    egal(r.status_code, 400, "un gabarit illisible est une erreur client, pas serveur")
    verifier("base64" in r.json().get("error", "").lower(),
             "le message doit indiquer le probleme de format")


@suite.test("/fingerprint/push — fingerId manquant -> 400")
def _():
    import base64 as b64
    r = requests.post(f"{BASE}/fingerprint/push",
                      json={"pin": PIN_TEST, "template": b64.b64encode(b"x" * 64).decode()},
                      timeout=TIMEOUT)
    egal(r.status_code, 400, "fingerId manquant doit etre refuse")


@suite.test("/getFace repond pour chaque machine connectee")
def _():
    for ip, machine in CONTEXTE["machines"].items():
        url = f"{BASE}/getFace/{PIN_TEST}/{CONTEXTE['branche']}/{machine['id']}"
        r = requests.get(url, timeout=TIMEOUT)
        parmi(r.status_code, (200, 400, 500), f"code inattendu pour {ip}")
        if r.status_code == 200:
            verifier("photo" in r.json(), f"cle 'photo' absente depuis {ip}")


@suite.test("/health expose l'etat de surveillance attendu")
def _():
    # Contrat consomme par Electron toutes les 10 s pour relancer le pont
    # quand un thread meurt. Volontairement PAS d'assertion sur le temps de
    # reponse : une surcharge d'environ 2 s frappe TOUTES les routes du pont
    # (mesuree aussi sur /config/gymBranchId, qui ne fait aucun traitement),
    # elle n'est donc pas imputable a /health.
    r = requests.get(f"{BASE}/health", timeout=10)
    egal(r.status_code, 200, "/health doit repondre 200")
    body = r.json()
    for champ in ("status", "machines", "machinesConnected", "machinesTotal",
                  "queuePending", "threads", "threadsDead", "watchdogStaleSeconds"):
        verifier(champ in body, f"champ '{champ}' manquant dans /health")
    verifier(isinstance(body["threads"], dict), "'threads' doit lister les threads attendus")


@suite.test("/health signale les threads de fond morts")
def _():
    # threadsDead est ce qui permet de detecter un process vivant mais fige —
    # le cas qui laissait des salles sans pointage sans que rien ne le signale.
    body = requests.get(f"{BASE}/health", timeout=10).json()
    morts = body.get("threadsDead")
    verifier(morts is not None, "'threadsDead' doit etre expose")
    if morts:
        print(f"        (info : threads morts signales -> {morts})")


if __name__ == "__main__":
    controle_prealable()
    sys.exit(0 if suite.executer() else 1)
