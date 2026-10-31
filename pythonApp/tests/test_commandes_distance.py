"""Commandes à distance (remote_commands.py) — sans machines, sans réseau, sans Kafka.

    venv\\Scripts\\python.exe tests\\test_commandes_distance.py
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnais import Suite, verifier, egal  # noqa: E402
import remote_commands  # noqa: E402
from remote_commands import RemoteCommands  # noqa: E402

suite = Suite("PONT — commandes à distance (aucune machine requise)")

NOW = 1_793_400_000_000


class FakeProducer:
    def __init__(self, on_produce=None):
        self.sent = []
        self.on_produce = on_produce

    def produce(self, topic, value):
        if self.on_produce:
            self.on_produce()
        self.sent.append((topic, json.loads(value)))

    def poll(self, _):
        return 0

    def flush(self, _):
        return 0


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeLock:
    def __init__(self, free=True, on_acquire=None):
        self.free = free
        self.on_acquire = on_acquire
        self.timeouts = []
        self.held = False

    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        if self.on_acquire:
            self.on_acquire(len(self.timeouts))
        if self.free:
            self.held = True
        return self.free

    def release(self):
        self.held = False


class PlcommAdapter:
    def __init__(self, ok=True, boom=None, handle="h"):
        self.ok = ok
        self.boom = boom
        self.calls = []
        self.connected = True
        self.handle = handle
        self.on_open = None

    def open_door(self, door_no=1, duration=5):
        self.calls.append(("C3", door_no, duration))
        if self.on_open:
            self.on_open()
        if self.boom:
            raise self.boom
        return self.ok


class ZkemAdapter:
    def __init__(self, ok=True, boom=None):
        self.ok = ok
        self.boom = boom
        self.calls = []

    def open_door(self, duration_seconds=5.0):
        self.calls.append(("ZKEM", duration_seconds))
        if self.boom:
            raise self.boom
        return self.ok


class Machine:
    def __init__(self, mid, door1=1, door2=2, door3=0, door4=0, porte_type=None):
        self.id = mid
        self.addresseip = "192.168.1.205"
        self.port = 4370
        self.type = "C3"
        self.door1, self.door2, self.door3, self.door4 = door1, door2, door3, door4
        self.porte_type = porte_type


class Ctx:
    def __init__(self, machine, adapter, lock=None):
        self.machine = machine
        self.adapter = adapter
        self.lock = lock or FakeLock()


class Banc:
    def __init__(self, contexts=None, check=204, check_error=None, clock=None, reachable=True,
                 sdk_lock=None, on_produce=None):
        self.producer = FakeProducer(on_produce)
        self.checks = []
        self.passages = []
        self.sleeps = []
        self.probes = []
        self.t = NOW
        self.ticks = list(clock) if clock else None
        self.sdk_lock = sdk_lock or FakeLock()
        contexts = contexts or {}

        def http_get(url, params=None, timeout=None):
            self.checks.append((url, params, timeout))
            if check_error:
                raise check_error
            return FakeResponse(check)

        def probe(machine, timeout):
            self.probes.append(timeout)
            return reachable

        self.r = RemoteCommands("empire", "1003", "broker:9094", "rt_pointage", "https://integration.yo-club.app/",
                                producer=self.producer, http_get=http_get, now_ms=self.now_ms,
                                device_context=lambda mid: contexts.get(mid),
                                device_contexts=lambda: list(contexts.values()),
                                publish_passage=lambda *args: self.passages.append(args),
                                reachable=probe, sdk_lock=self.sdk_lock, sleep=self.sleep)

    def now_ms(self):
        if self.ticks:
            return self.ticks[0] if len(self.ticks) == 1 else self.ticks.pop(0)
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += int(seconds * 1000)

    def result(self):
        results = [payload for topic, payload in self.producer.sent if topic == "device_result"]
        return results[-1] if results else None


def command(**overrides):
    message = {"commandId": "c1", "type": "OPEN_DOOR", "tenant": "empire", "gymBranchId": 1003,
               "machineId": 12, "door": 2, "unlockSeconds": 5, "memberPin": None,
               "issuedAt": NOW - 300, "deadlinePcMs": NOW + 3000, "requestedBy": "sub"}
    message.update(overrides)
    return message


def demarrage_remote(arbre):
    for noeud in ast.walk(arbre):
        corps = getattr(noeud, "body", None)
        if not isinstance(corps, list):
            continue
        for index, instr in enumerate(corps):
            if isinstance(instr, ast.Try) and any(
                    isinstance(i, ast.Import) and any(a.name == "remote_commands" for a in i.names)
                    for i in instr.body):
                return corps, index
    return None, None


@suite.test("le réglage REMOTE_COMMANDS_ENABLED est éteint par défaut")
def _():
    verifier(not remote_commands.enabled({}), "absent doit valoir éteint")
    verifier(not remote_commands.enabled({"REMOTE_COMMANDS_ENABLED": "false"}), "false doit valoir éteint")
    for valeur in ["1", "true", "TRUE", "oui", " yes "]:
        verifier(remote_commands.enabled({"REMOTE_COMMANDS_ENABLED": valeur}), f"{valeur!r} doit allumer")


@suite.test("éteint, start() ne crée rien et rend None")
def _():
    precedent = os.environ.pop("REMOTE_COMMANDS_ENABLED", None)
    try:
        egal(remote_commands.start("empire", "1003", "broker:9094", "rt_pointage", "https://x"), None,
             "start éteint")
    finally:
        if precedent is not None:
            os.environ["REMOTE_COMMANDS_ENABLED"] = precedent


@suite.test("main.py démarre le module en dernier, dans un try/except, juste avant la boucle principale")
def _():
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    arbre = ast.parse(open(chemin, encoding="utf-8").read())
    corps, index = demarrage_remote(arbre)
    verifier(corps is not None, "démarrage introuvable")
    garde = corps[index]
    appels = [instr for instr in garde.body if isinstance(instr, ast.Expr)
              and isinstance(instr.value, ast.Call) and isinstance(instr.value.func, ast.Attribute)
              and instr.value.func.attr == "start"
              and getattr(instr.value.func.value, "id", None) == "remote_commands"]
    egal(len(appels), 1, "remote_commands.start appelé dans ce try")
    verifier(any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in garde.handlers),
             "le démarrage doit attraper Exception")
    verifier("start_machine_status_broadcast" in ast.unparse(corps[index - 1]),
             "le démarrage suit start_machine_status_broadcast")
    verifier(isinstance(corps[index + 1], ast.While), "la boucle principale suit immédiatement")
    sans_garde = [n for n in ast.walk(arbre) if isinstance(n, ast.Import)
                  and any(a.name == "remote_commands" for a in n.names)]
    egal(len(sans_garde), 1, "remote_commands n'est importé qu'une fois")


@suite.test("une commande d'une autre filiale est ignorée sans réponse")
def _():
    b = Banc()
    egal(b.r.handle(command(gymBranchId=7)), None, "statut")
    egal(b.producer.sent, [], "rien publié")
    egal(b.checks, [], "aucune vérification")


@suite.test("reçue après l'échéance -> EXPIRED, sans vérification ni pointeuse")
def _():
    adapter = PlcommAdapter()
    b = Banc({12: Ctx(Machine(12), adapter)})
    egal(b.r.handle(command(deadlinePcMs=NOW - 1)), "EXPIRED", "statut")
    egal(b.r.handle(command(deadlinePcMs=True)), "EXPIRED", "échéance booléenne")
    egal(b.checks, [], "aucune vérification")
    egal(adapter.calls, [], "aucune ouverture")
    egal(b.result()["status"], "EXPIRED", "résultat publié")


@suite.test("type inconnu -> REJECTED avant la vérification")
def _():
    b = Banc()
    egal(b.r.handle(command(type="REBOOT")), "REJECTED", "statut")
    egal(b.checks, [], "aucune vérification")


@suite.test("pointeuse inconnue ou porte invalide -> REJECTED sans consommer la commande au serveur")
def _():
    b = Banc({})
    egal(b.r.handle(command()), "REJECTED", "pointeuse inconnue")
    adapter = PlcommAdapter()
    b2 = Banc({12: Ctx(Machine(12), adapter)})
    for porte in (None, 5, 0, True, "2"):
        egal(b2.r.handle(command(door=porte)), "REJECTED", f"porte {porte!r}")
    egal(b.checks + b2.checks, [], "aucune vérification")
    egal(adapter.calls, [], "aucune ouverture")


@suite.test("la vérification porte la cible exacte ; non confirmée ou serveur injoignable -> REJECTED")
def _():
    adapter = PlcommAdapter()
    b = Banc({12: Ctx(Machine(12), adapter)}, check=404)
    egal(b.r.handle(command(memberPin="7")), "REJECTED", "404")
    url, params, timeout = b.checks[0]
    egal(url, "https://integration.yo-club.app/gym-management/public/device/commands/empire/c1/check",
         "adresse de vérification")
    egal(params, {"gymBranchId": "1003", "machineId": 12, "door": 2, "memberPin": "7"}, "cible liée")
    egal(timeout, 2, "délai de vérification")
    zkem = ZkemAdapter()
    b = Banc({13: Ctx(Machine(13), zkem)})
    b.r.handle(command(machineId=13, door=None))
    egal(b.checks[0][1], {"gymBranchId": "1003", "machineId": 13}, "autonome : ni porte ni adhérent")
    b = Banc({12: Ctx(Machine(12), adapter)}, check_error=OSError("réseau"))
    egal(b.r.handle(command()), "REJECTED", "exception réseau")
    egal(adapter.calls, [], "aucune ouverture")


@suite.test("C3 : ouverture, verrou attendu au plus jusqu'à l'échéance, rendu avant de publier")
def _():
    lock = FakeLock()
    adapter = PlcommAdapter()
    b = Banc({12: Ctx(Machine(12), adapter, lock)}, on_produce=lambda: etats.append(lock.held))
    etats = []
    egal(b.r.handle(command(door=2)), "OPENED", "porte 2")
    egal(adapter.calls, [("C3", 2, 5)], "même appel que /door/open")
    verifier(not lock.held, "verrou rendu")
    egal(lock.timeouts, [3.0], "attente du verrou limitée au temps restant")
    egal(etats, [False], "le résultat part verrou rendu")
    egal(b.sdk_lock.timeouts, [], "le verrou SDK ZKEM n'est pas pris pour un C3")
    resultat = b.result()
    egal((resultat["commandId"], resultat["tenant"], resultat["gymBranchId"], resultat["machineId"]),
         ("c1", "empire", 1003, 12), "identité du résultat")
    egal(resultat["detail"], None, "pas de détail")


@suite.test("une durée d'ouverture illisible ne bloque jamais la pointeuse et reste bornée")
def _():
    for valeur, attendu in (("x", 5), ([], 5), ({}, 5), (None, 5), (True, 5), (float("inf"), 5),
                            ("7", 7), (99, 10), (0, 1), (-3, 1)):
        lock = FakeLock()
        adapter = PlcommAdapter()
        b = Banc({12: Ctx(Machine(12), adapter, lock)})
        egal(b.r.handle(command(unlockSeconds=valeur)), "OPENED", f"durée {valeur!r}")
        egal(adapter.calls, [("C3", 2, attendu)], f"durée {valeur!r} bornée")
        verifier(not lock.held, f"verrou rendu pour {valeur!r}")


@suite.test("C3 sans session : jamais de connexion parallèle, attente bornée puis FAILED")
def _():
    lock = FakeLock()
    adapter = PlcommAdapter(handle=None)
    b = Banc({12: Ctx(Machine(12), adapter, lock)})
    egal(b.r.handle(command()), "FAILED", "statut")
    egal(adapter.calls, [], "open_door jamais appelé sans session")
    verifier(not lock.held, "verrou rendu")
    verifier(b.t <= NOW + 3000, "attente arrêtée à l'échéance")
    verifier(all(0 <= s <= 0.2 for s in b.sleeps), "attente par petits pas, verrou rendu entre deux")
    egal(b.result()["detail"], "Pointeuse C3 sans session jusqu'à l'échéance", "détail")


@suite.test("C3 en cours de renouvellement de session : on attend la nouvelle session puis on ouvre")
def _():
    adapter = PlcommAdapter(handle=None)

    def session_rendue(nombre):
        if nombre == 3:
            adapter.handle = "h2"

    lock = FakeLock(on_acquire=session_rendue)
    b = Banc({12: Ctx(Machine(12), adapter, lock)})
    egal(b.r.handle(command()), "OPENED", "statut")
    egal(adapter.calls, [("C3", 2, 5)], "une seule ouverture")
    egal(len(b.sleeps), 2, "deux attentes avant la session")
    verifier(not lock.held, "verrou rendu")


@suite.test("autonome : sonde TCP puis verrou SDK borné, porte ignorée, durée en secondes")
def _():
    adapter = ZkemAdapter()
    lock = FakeLock()
    b = Banc({13: Ctx(Machine(13), adapter, lock)})
    egal(b.r.handle(command(machineId=13, door=None)), "OPENED", "statut")
    egal(adapter.calls, [("ZKEM", 5)], "ACUnlock via open_door(duration_seconds)")
    egal(b.probes, [1.5], "sonde TCP bornée")
    egal(b.sdk_lock.timeouts, [3.0], "verrou SDK attendu au plus jusqu'à l'échéance")
    verifier(not b.sdk_lock.held and not lock.held, "les deux verrous rendus")


@suite.test("autonome injoignable -> FAILED sans entrer dans le SDK")
def _():
    adapter = ZkemAdapter()
    lock = FakeLock()
    b = Banc({13: Ctx(Machine(13), adapter, lock)}, reachable=False)
    egal(b.r.handle(command(machineId=13, door=None)), "FAILED", "statut")
    egal(adapter.calls, [], "aucune ouverture")
    egal(b.sdk_lock.timeouts, [], "verrou SDK jamais demandé")
    verifier(not lock.held, "verrou machine rendu")


@suite.test("SDK occupé par une autre pointeuse jusqu'à l'échéance -> EXPIRED, verrous rendus")
def _():
    adapter = ZkemAdapter()
    lock = FakeLock()
    b = Banc({13: Ctx(Machine(13), adapter, lock)}, sdk_lock=FakeLock(free=False))
    egal(b.r.handle(command(machineId=13, door=None)), "EXPIRED", "statut")
    egal(adapter.calls, [], "aucune ouverture")
    verifier(not lock.held, "verrou machine rendu")


@suite.test("exception de l'autonome -> FAILED, les deux verrous rendus")
def _():
    lock = FakeLock()
    b = Banc({13: Ctx(Machine(13), ZkemAdapter(boom=RuntimeError("COM")), lock)})
    egal(b.r.handle(command(machineId=13, door=None)), "FAILED", "statut")
    verifier(not lock.held and not b.sdk_lock.held, "verrous rendus")


@suite.test("pointeuse occupée jusqu'à l'échéance -> EXPIRED sans ouverture")
def _():
    adapter = PlcommAdapter()
    b = Banc({12: Ctx(Machine(12), adapter, FakeLock(free=False))})
    egal(b.r.handle(command()), "EXPIRED", "statut")
    egal(adapter.calls, [], "aucune ouverture")


@suite.test("l'échéance passée pendant la vérification -> EXPIRED")
def _():
    adapter = PlcommAdapter()
    b = Banc({12: Ctx(Machine(12), adapter)}, clock=[NOW, NOW + 3000, NOW + 3001])
    egal(b.r.handle(command()), "EXPIRED", "statut")
    egal(adapter.calls, [], "aucune ouverture")


@suite.test("une ouverture qui finit après l'échéance est publiée telle quelle, avec son retard")
def _():
    adapter = PlcommAdapter()
    b = Banc({12: Ctx(Machine(12), adapter)})

    def lente():
        b.t = NOW + 4200

    adapter.on_open = lente
    egal(b.r.handle(command()), "OPENED", "la porte s'est ouverte")
    egal(b.result()["detail"], "Ouverte 1200 ms après l'échéance", "retard signalé")


@suite.test("refus ou erreur de la pointeuse -> FAILED, verrou rendu")
def _():
    lock = FakeLock()
    b = Banc({12: Ctx(Machine(12), PlcommAdapter(ok=False), lock)})
    egal(b.r.handle(command()), "FAILED", "refus")
    lock = FakeLock()
    b = Banc({12: Ctx(Machine(12), PlcommAdapter(boom=RuntimeError("x" * 500)), lock)})
    egal(b.r.handle(command()), "FAILED", "exception")
    verifier(not lock.held, "verrou rendu après une exception")
    egal(len(b.result()["detail"]), 200, "détail tronqué")


@suite.test("passage publié seulement après une ouverture pour un adhérent, sens tiré de la porte")
def _():
    machine = Machine(12, door1=1, door2=2)
    b = Banc({12: Ctx(machine, PlcommAdapter())})
    egal(b.r.handle(command(door=2, memberPin="1")), "OPENED", "statut")
    egal(len(b.passages), 1, "un passage")
    ctx, door, pin, porte, topic, tenant, branche = b.passages[0]
    egal((door, pin, porte, topic, tenant, branche), (2, "1", "SORTIE", "rt_pointage", "empire", "1003"),
         "passage")
    b = Banc({12: Ctx(machine, PlcommAdapter(ok=False))})
    b.r.handle(command(memberPin="1"))
    egal(b.passages, [], "aucun passage sans ouverture")
    b = Banc({12: Ctx(machine, PlcommAdapter())})
    b.r.handle(command())
    egal(b.passages, [], "aucun passage sans adhérent")


@suite.test("autonome : le passage prend le sens de la fiche, comme MonitorZkem")
def _():
    b = Banc({13: Ctx(Machine(13, porte_type="SORTIE"), ZkemAdapter())})
    egal(b.r.handle(command(machineId=13, door=None, memberPin="1")), "OPENED", "statut")
    egal(b.passages[0][1:4], (1, "1", "SORTIE"), "porte 1, sens de la fiche")
    b = Banc({13: Ctx(Machine(13), ZkemAdapter())})
    b.r.handle(command(machineId=13, door=None, memberPin="1"))
    egal(b.passages[0][3], "", "sens vide quand la fiche n'en a pas")


@suite.test("un passage impossible à publier ne change pas le résultat")
def _():
    b = Banc({12: Ctx(Machine(12), PlcommAdapter())})

    def casse(*_):
        raise RuntimeError("kafka")

    b.r._publish_passage = casse
    egal(b.r.handle(command(memberPin="1")), "OPENED", "statut")


@suite.test("le fil des commandes initialise COM avant toute chose")
def _():
    appels = []
    original = remote_commands._com_initialize
    remote_commands._com_initialize = lambda: appels.append("CoInitialize")
    try:
        b = Banc()
        b.r.stop()
        b.r._consume_loop()
    finally:
        remote_commands._com_initialize = original
    egal(appels, ["CoInitialize"], "COM initialisé")
    remote_commands._com_initialize()


@suite.test("le signal de présence porte la filiale, les capacités et l'état connu des pointeuses")
def _():
    sans_etat = Ctx(Machine(13), ZkemAdapter())
    b = Banc({12: Ctx(Machine(12), PlcommAdapter()), 13: sans_etat})
    signal = b.r.presence()
    egal(signal["tenant"], "empire", "tenant")
    egal(signal["gymBranchId"], 1003, "filiale")
    egal(signal["capabilities"], ["OPEN_DOOR"], "capacités")
    egal(signal["sentAtPcMs"], NOW, "heure du PC")
    egal(signal["machines"], [{"id": 12, "connected": True}, {"id": 13, "connected": None}], "pointeuses")


if __name__ == "__main__":
    sys.exit(0 if suite.executer() else 1)
