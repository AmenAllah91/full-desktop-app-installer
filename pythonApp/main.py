import base64
import os
import signal
import sys
import threading
from datetime import datetime

import psutil
import logging
import time
import json
import sqlite3

import pythoncom

from domain import Operation
from kafka_service.kafkaservice import KafkaService
from services.DeviceMAnager import DeviceManager
from services.MonitorZkem import monitor_zkem
from services.adapters import PlcommAdapter, MachineType
from services.common import zk_sdk_lock, tcp_reachable
from services.captureFingerPrint import FingerprintCapture
from services.documentManagerService import DocumentManagerService
from services.machinesService import AccessMachineService
from services.MachineMonitor import monitor_machine, make_rt_json, kafka, attempt_c3_reconnection

from flask import Flask, jsonify
from flask_cors import CORS
from queue import Queue
from dotenv import load_dotenv, set_key

from services.websocket import start_ws_server, send_pointage, start_machine_status_broadcast
from services.zkem_adapter import ZkemAdapter
from services.adms_adapter import ADMSAdapter
from services.adms_server import ADMSServer
from services.MonitorADMS import monitor_adms
from services.logger import setup_logging, log_memory_usage, start_memory_monitor

# v2 imports
from services.v2.startup import start_v2
from services.v2.device_manager_v2 import DeviceManagerV2

# Version globale — "v1" (legacy multi-protocole) ou "v2" (full PUSH/ADMS)
app_version = "v1"

def get_app_data_dir():
    logging.info("Trying to get APPDATA environment variable...")
    app_data = os.environ.get('APPDATA')

    if not app_data:
        logging.warning("APPDATA not found. Falling back to default path...")
        app_data = os.path.expanduser('~\\AppData\\Roaming')
    else:
        logging.info("APPDATA found: %s", app_data)

    app_dir = os.path.join(app_data, 'desktop-app')
    logging.info("Full application directory path: %s", app_dir)

    os.makedirs(app_dir, exist_ok=True)
    logging.info("Directory ensured (created if it didn't exist).")

    return app_dir


def initialize_env_file():
    """Initialize the .env file with default values if it doesn't exist."""
    if not os.path.exists(ENV_FILE_PATH):
        default_env_content = """# .env

# Racine de la plateforme : seule ligne à changer pour basculer d'environnement.
YOGYM_BASE_URL=https://app.yogym.co

KAFKA_BROKER=51.178.55.238:9094
KAFKA_GROUP_ID=group_c
KAFKA_TOPIC=rt_pointage
GYM_BRANCH_ID=0

FLASK_HOST=0.0.0.0
FLASK_PORT=9998

PLCOMPRO_URL=plcommpro.dll
"""
        with open(ENV_FILE_PATH, 'w') as f:
            f.write(default_env_content)
        logging.info("Created default .env file at: %s", ENV_FILE_PATH)


# Get paths for data files
APP_DATA_DIR = get_app_data_dir()
ENV_FILE_PATH = os.path.join(APP_DATA_DIR, '.env')
DB_FILE = os.path.join(APP_DATA_DIR, 'task_queue.db')
TEMP_DIR = os.path.join(APP_DATA_DIR, 'temp')

# Create temp directory for photos
os.makedirs(TEMP_DIR, exist_ok=True)

# Load environment variables from AppData
load_dotenv(dotenv_path=ENV_FILE_PATH)
initialize_env_file()

KafkaBroker = os.getenv("KAFKA_BROKER")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "rt_pointage")

# Flask application initialization
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, allow_headers=["*"], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], supports_credentials=True)


@app.after_request
def add_private_network_headers(response):
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# Werkzeug sert chaque requête dans un thread neuf, et plusieurs endpoints
# touchent le SDK ZK (photo, porte, empreintes) voire créent un objet COM
# (get_fingerprints_api enregistre une machine en repli). Un thread qui n'a pas
# initialisé COM n'a pas le droit d'en créer : sans ça l'appel échoue, ou pire
# « marche » en vtable brute jusqu'au jour où il cesse de marcher.
@app.before_request
def _com_initialize_request_thread():
    try:
        pythoncom.CoInitialize()
    except Exception as exc:
        logging.debug("CoInitialize ignoré sur ce thread : %s", exc)


@app.teardown_request
def _com_uninitialize_request_thread(exc=None):
    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass
# ------------------------------------------------------------------ #
# Identité de l'instance : tenant + club.
#
# Elle DOIT être résolue ici, avant la création des KafkaService, car elle
# détermine leur group.id. sys.argv fait foi — c'est Electron qui passe le
# tenant et le gymBranchId saisis à l'installation. Le .env n'est qu'un repli
# pour les lancements manuels : il est identique dans tous les installeurs,
# s'en servir ferait rejoindre le même consumer group à tous les postes de
# tous les clubs, et un seul d'entre eux recevrait chaque message.
# ------------------------------------------------------------------ #
def _resolve_identity() -> tuple[str, str]:
    """
    Identité du poste : tenant + club, fournis par Electron en arguments.

    AUCUN REPLI, volontairement. Le `.env` est identique dans tous les
    installeurs : s'en servir ferait démarrer un poste vikingsgym sous l'identité
    empiregym — pointages écrits dans la mauvaise base, et consumer group partagé
    entre clubs qui se voleraient les messages. Une chaîne vide est `falsy` en
    Python, donc l'ancien `argv or getenv(...)` basculait sur le repli dès que le
    formulaire Electron était validé à vide. Mieux vaut refuser de démarrer.
    """
    argv_tenant = sys.argv[1].strip() if len(sys.argv) >= 2 else ""
    argv_branch = sys.argv[2].strip() if len(sys.argv) >= 3 else ""

    manquants = []
    if not argv_tenant:
        manquants.append("tenant")
    if not argv_branch:
        manquants.append("gymBranchId")

    if manquants:
        msg = (
            "DEMARRAGE REFUSE : " + " et ".join(manquants) + " non fourni(s). "
            "Usage : pythonApp.exe <tenant> <gymBranchId>. "
            "Ces deux valeurs sont saisies a la premiere ouverture de YoGym et "
            "conservees dans config.json. Aucune valeur par defaut n'est "
            "appliquee : un repli ferait tourner ce poste sous l'identite d'un "
            "autre club."
        )
        print(msg, file=sys.stderr, flush=True)
        logging.critical(msg)
        sys.exit(2)

    return argv_tenant, argv_branch


tenant, currentGymBranchId = _resolve_identity()

# Kafka service configuration
# group.id unique par tenant + club : chaque poste doit recevoir TOUS les
# messages de son club, jamais une part répartie entre postes.
# auto.offset.reset=latest : ces topics portent des commandes, pas un
# historique à rejouer — au premier démarrage on part de maintenant.
_KAFKA_GROUP_SUFFIX = f"{tenant}_{currentGymBranchId}"
pointage_kafka = KafkaService(kafka_broker=KafkaBroker,
                              group_id=f"pointage_{_KAFKA_GROUP_SUFFIX}",
                              auto_offset_reset="latest")
publish_photo_kafka = KafkaService(kafka_broker=KafkaBroker,
                                   group_id=f"photo_publish_{_KAFKA_GROUP_SUFFIX}",
                                   auto_offset_reset="latest")
fingerprint_kafka = KafkaService(kafka_broker=KafkaBroker,
                                 group_id=f"fingerprint_{_KAFKA_GROUP_SUFFIX}",
                                 auto_offset_reset="latest")

machineService = AccessMachineService()

# Global variables and synchronization primitives
device_handle = None
handle_lock = threading.RLock()
stop_event_monitoring = threading.Event()
stop_event_kafka = threading.Event()
monitoring_success = threading.Event()
device_queue = Queue()

# Renseignés au démarrage dans __main__, lus par /restart et par le thread de
# rafraîchissement des machines.
watchdog = None
adms_server = None

APP_STARTED_AT = time.time()

# Configuration constants
DEVICE_IP = os.getenv("DEVICE_IP")
DEVICE_PORT = os.getenv("DEVICE_PORT")
FLASK_HOST = os.getenv("FLASK_HOST")
FLASK_PORT = int(os.getenv("FLASK_PORT"))
MAX_LOCK_RETRIES = 3
LOCK_TIMEOUT = 10
QUEUE_TIMEOUT = 5
MONITORING_INTERVAL = 0.1

TOPIC_CONSUME_PUBLISH_PHOTO = "launch_publish_photo"
TOPIC_PRODUCE_PUBLISH_PHOTO = "finish_publish_photo"


# ------------------------------------------------------------------ #
# Watchdog — surveille les threads monitors et les redémarre si morts
# ------------------------------------------------------------------ #
class MachineWatchdog:
    """
    Surveille les threads de monitoring (C3 et ZKEM).
    Si un thread meurt → backoff exponentiel → redémarrage automatique.
    S'arrête proprement quand stop_event_monitoring est déclenché.

    Ne juge QUE la vivacité du thread. L'ancienneté du dernier pointage n'est pas
    un critère de santé : une salle vide n'est pas une machine en panne. La santé
    de la session est évaluée par les monitors eux-mêmes (TCP + RegEvent).
    """

    BASE_DELAY = 5
    MAX_DELAY  = 300
    CHECK_INTERVAL = 10

    def __init__(self):
        # { machine_id: { "thread": Thread, "factory": callable, "delay": int } }
        self._entries: dict = {}
        # Le rafraîchissement périodique des machines enregistre à chaud, pendant
        # que _watch_loop parcourt le dict. Sans verrou ni copie, on récolte un
        # "dictionary changed size during iteration" qui tue le watchdog.
        self._lock = threading.Lock()
        # Horodatage du dernier tour de boucle, exposé par /health : c'est ce
        # qui permet à Electron de distinguer « process vivant » de « process
        # vivant mais figé », le cas que personne ne détectait jusqu'ici.
        self.last_tick = time.time()
        self._watcher = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name="MachineWatchdog"
        )

    def register(self, machine_id: int, factory: callable) -> None:
        """
        Démarre le thread et l'enregistre pour surveillance.
        factory = callable sans argument qui retourne un Thread prêt (non démarré).
        """
        t = factory()
        t.start()
        with self._lock:
            self._entries[machine_id] = {
                "thread":   t,
                "factory":  factory,
                "delay":    self.BASE_DELAY,
                "retry_at": 0.0,
            }
        logging.info("🐕 Watchdog enregistré — machine %s (%s)", machine_id, t.name)

    def is_registered(self, machine_id: int) -> bool:
        with self._lock:
            return machine_id in self._entries

    def start(self) -> None:
        self._watcher.start()
        logging.info("🐕 Watchdog démarré")

    def _watch_loop(self) -> None:
        while not stop_event_monitoring.is_set():
            time.sleep(self.CHECK_INTERVAL)
            self.last_tick = time.time()

            try:
                self._check_all()
            except Exception:
                # Le watchdog est le seul à relancer les monitors : s'il meurt,
                # plus aucune machine n'est surveillée et personne n'est prévenu.
                # Il ne doit donc jamais sortir de sa boucle sur une exception.
                logging.exception("💥 [Watchdog] Erreur dans la boucle de surveillance")

        logging.info("🛑 [Watchdog] Arrêté")

    def _check_all(self) -> None:
        # Copie sous verrou : le thread de rafraîchissement peut enregistrer une
        # nouvelle machine pendant qu'on parcourt.
        with self._lock:
            entries = list(self._entries.items())

        now = time.time()
        for mid, entry in entries:
            if stop_event_monitoring.is_set():
                return

            if entry["thread"].is_alive():
                entry["delay"] = self.BASE_DELAY
                entry["retry_at"] = 0.0
                continue

            # Backoff NON BLOQUANT : on note l'échéance et on passe à la machine
            # suivante. L'ancienne version dormait ici jusqu'à MAX_DELAY, donc
            # une seule machine éteinte — situation normale en salle — privait
            # tout le reste du parc de surveillance pendant 5 minutes.
            if now < entry.get("retry_at", 0.0):
                continue

            delay = entry["delay"]
            new_thread = entry["factory"]()
            new_thread.start()
            entry["thread"] = new_thread
            entry["delay"] = min(delay * 2, self.MAX_DELAY)
            entry["retry_at"] = now + entry["delay"]
            logging.info(
                "♻️ [Watchdog] Thread relancé — machine %s (%s), "
                "prochaine tentative dans %ss si nouvel échec",
                mid, new_thread.name, entry["delay"]
            )


# ------------------------------------------------------------------ #
# Liste des machines : récupération résiliente et rafraîchissement
# ------------------------------------------------------------------ #

def is_probe_safe(machine) -> bool:
    """
    Peut-on ouvrir une socket de test vers cette machine sans la perturber ?

    Non pour les C3 : le panneau ne délivre qu'un seul handle à la fois, et
    toute connexion parallèle sur son port SDK fait tomber la session du thread
    temps réel. Les terminaux ZKEM, eux, tolèrent la sonde.
    """
    return getattr(machine, "type", None) != "C3"


def machine_ready_for_requeue(machine) -> bool:
    """
    La machine peut-elle reprendre les tâches mises de côté ?

    Pour un C3, la réponse ne peut pas venir d'une sonde TCP (voir is_probe_safe) :
    l'indicateur est la session du thread temps réel. Si elle est établie, le
    panneau répond.

    Sans ce cas particulier, is_probe_safe écartait les C3 de tout requeue : une
    tâche C3 mise en attente n'était jamais rejouée et finissait purgée au bout de
    quelques heures, silencieusement.
    """
    if not is_probe_safe(machine):
        ctx = DeviceManager.get(machine.id)
        return ctx is not None and ctx.handle is not None
    return tcp_reachable(machine.addresseip, machine.port)


def fetch_machines_with_retry(tenant: str, gym_branch_id: str,
                              max_wait: int = 300) -> list:
    """
    Récupère la liste des machines en insistant tant que le réseau n'est pas prêt.

    Un PC qui démarre lance YoGym avant que le DNS ne réponde : get_access_machines
    levait alors une exception, le `try` de __main__ la relançait, et le pont
    mourait sans que personne ne le redémarre.

    Passé max_wait, on démarre à vide plutôt que de bloquer : Flask et le
    WebSocket montent, et refresh_machines_loop récupère les machines dès que
    le réseau revient.
    """
    delay, waited = 2, 0
    while True:
        try:
            return machineService.get_access_machines(gym_branch_id, tenant)
        except Exception as exc:
            if waited >= max_wait:
                logging.error(
                    "❌ Liste des machines toujours indisponible après %ss (%s) — "
                    "démarrage à vide, le rafraîchissement périodique prendra le relais",
                    waited, exc
                )
                return []
            logging.warning(
                "⏳ Liste des machines indisponible (%s) — nouvel essai dans %ss",
                exc, delay
            )
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 30)


def register_machine_monitor(wd: "MachineWatchdog", m, ctx,
                             tenant: str, gym_branch_id: str,
                             adms_server=None) -> None:
    """Enregistre auprès du watchdog le thread de monitoring adapté au type."""
    if m.type == "C3":
        def factory(ctx=ctx):
            return threading.Thread(
                target=monitor_machine,
                args=(ctx, stop_event_monitoring),
                daemon=True,
                name=f"RT-C3-{ctx.machine.addresseip}")

    elif m.type == "PUSH":
        adapter = ctx.adapter
        if adms_server:
            adms_server.register_adapter(adapter)
        else:
            logging.warning(
                "⚠️ Machine PUSH %s sans serveur ADMS actif — "
                "redémarrer le pont pour l'activer", m.addresseip)

        def factory(m=m, adapter=adapter):
            return threading.Thread(
                target=monitor_adms,
                args=(m, adapter, stop_event_monitoring, tenant, gym_branch_id),
                daemon=True,
                name=f"RT-PUSH-{m.addresseip}")

    else:  # STANDALONE_NEW_FIRMWARE
        def factory(m=m):
            return threading.Thread(
                target=monitor_zkem,
                args=(m, m.addresseip, m.port, 1,
                      stop_event_monitoring, tenant, gym_branch_id),
                daemon=True,
                name=f"RT-ZK-{m.addresseip}")

    wd.register(m.id, factory)


def refresh_machines_loop(wd: "MachineWatchdog", tenant: str, gym_branch_id: str,
                          adms_server=None, interval: int = 300) -> None:
    """
    Relit périodiquement la configuration des machines.

    Elle n'était lue qu'au démarrage : une machine ajoutée en back-office restait
    inconnue du pont, et ses tâches d'accès étaient marquées COMPLETED sans avoir
    jamais été exécutées — 164 pertes constatées en 22 h sur un seul poste.

    On ne traite que les ajouts. Retirer une machine à chaud demanderait un
    stop_event par machine ; on se contente de le signaler.
    """
    # DeviceManager.register construit un ZkemAdapter, donc un objet COM :
    # comme tout thread qui touche au SDK, celui-ci doit initialiser COM.
    pythoncom.CoInitialize()
    try:
        while not stop_event_monitoring.wait(interval):
            try:
                machines = machineService.get_access_machines(gym_branch_id, tenant)
            except Exception as exc:
                logging.warning("⚠️ Rafraîchissement des machines impossible : %s", exc)
                continue

            if not machines:
                continue

            seen = set()
            for m in machines:
                seen.add(m.id)

                if DeviceManager.get(m.id) is None:
                    logging.info(
                        "🆕 Nouvelle machine détectée — id=%s alias=%s ip=%s type=%s",
                        m.id, m.alias, m.addresseip, m.type
                    )
                    ctx = DeviceManager.register(m, tenant, gym_branch_id)
                    register_machine_monitor(wd, m, ctx, tenant, gym_branch_id,
                                             adms_server)

                # Une machine peut être revenue après une coupure de courant :
                # on rejoue alors ce qui avait été mis de côté en son absence.
                if machine_ready_for_requeue(m):
                    requeued = requeue_tasks_for_machine(m.id)
                    if requeued:
                        logging.info("♻️ %s tâche(s) rejouée(s) — machine %s (%s) "
                                     "de nouveau joignable",
                                     requeued, m.id, m.addresseip)

            purged = purge_orphan_deferred_tasks(seen)
            if purged:
                logging.warning("🗑️ %s tâche(s) supprimée(s) : leur machine n'est "
                                "plus déclarée par le cloud (désactivée)", purged)

            for ctx in list(DeviceManager.all()):
                if ctx.machine.id not in seen:
                    logging.warning(
                        "⚠️ Machine %s (%s) absente de la configuration cloud — "
                        "monitoring toujours actif, redémarrer le pont pour l'arrêter",
                        ctx.machine.id, ctx.machine.addresseip
                    )
    finally:
        pythoncom.CoUninitialize()


# ------------------------------------------------------------------ #
# (reste du code inchangé)
# ------------------------------------------------------------------ #

def initialize_task_queue_db():
    """Initialize the SQLite database for task queue."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_data TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def add_task_to_queue(task):
    """Add a task to the SQLite queue."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO task_queue (task_data, status) VALUES (?, 'PENDING')", (json.dumps(task),))
    conn.commit()
    conn.close()


def get_next_task_from_queue():
    """Retrieve the next PENDING task from the SQLite queue."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, task_data FROM task_queue WHERE status = 'PENDING' ORDER BY created_at LIMIT 1")
    task = cursor.fetchone()
    conn.close()
    return task


def mark_task_as_completed(task_id):
    """Mark a task as COMPLETED in the SQLite queue and delete all completed tasks."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE task_queue SET status = 'COMPLETED' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    delete_completedTasks()


def defer_task(task_id):
    """
    Met une tâche de côté en attendant que sa machine soit connue.

    Elle était auparavant marquée COMPLETED, donc perdue définitivement : 41
    demandes d'accès ont ainsi disparu en une journée pour une machine que
    l'API publique ne renvoie pas (statut != Active) mais que le backend
    continue de désigner dans ses messages Kafka. On ne peut pas la laisser en
    PENDING — la file est FIFO, elle bloquerait tout ce qui suit — d'où ce
    statut distinct.
    """
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE task_queue SET status = 'WAITING_MACHINE' WHERE id = ?",
                 (task_id,))
    conn.commit()
    conn.close()


def requeue_tasks_for_machine(machine_id) -> int:
    """Réactive les tâches mises de côté dès que leur machine apparaît."""
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT id, task_data FROM task_queue WHERE status = 'WAITING_MACHINE'"
    ).fetchall()

    ids = []
    for task_id, raw in rows:
        try:
            if json.loads(raw).get("machineId") == machine_id:
                ids.append((task_id,))
        except Exception:
            continue

    if ids:
        conn.executemany("UPDATE task_queue SET status = 'PENDING' WHERE id = ?", ids)
        conn.commit()
    conn.close()
    return len(ids)


def purge_orphan_deferred_tasks(valid_machine_ids, max_age_hours: int = 6) -> int:
    """
    Supprime les tâches en attente d'une machine que le cloud ne déclare plus.

    Une machine désactivée en back-office n'est plus renvoyée par l'API, mais
    `giveAccessToClient` continue de la citer dans ses messages Kafka — il liste
    `gymBranch.getMachines()` sans filtrer sur le statut. Sans cette purge, ses
    tâches s'empileraient indéfiniment.

    Le délai de grâce protège une machine qui vient d'être ajoutée et que le
    rafraîchissement n'a pas encore vue : on ne jette que ce qui est à la fois
    ancien ET absent de la configuration.
    """
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT id, task_data FROM task_queue "
        "WHERE status = 'WAITING_MACHINE' AND created_at < datetime('now', ?)",
        (f"-{max_age_hours} hours",)
    ).fetchall()

    doomed = []
    for task_id, raw in rows:
        try:
            if json.loads(raw).get("machineId") not in valid_machine_ids:
                doomed.append((task_id,))
        except Exception:
            doomed.append((task_id,))

    if doomed:
        conn.executemany("DELETE FROM task_queue WHERE id = ?", doomed)
        conn.commit()
    conn.close()
    return len(doomed)


def delete_completedTasks():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_queue WHERE status = 'COMPLETED'")
    conn.commit()
    conn.close()


def get_device_context(machine_id):
    """Retourne le DeviceContext pour une machine, v1 ou v2."""
    if app_version == "v2":
        return DeviceManagerV2.get(machine_id)
    else:
        return DeviceManager.get(machine_id)


def get_all_device_contexts():
    """Retourne tous les DeviceContext, v1 ou v2."""
    if app_version == "v2":
        return DeviceManagerV2.all()
    else:
        return list(DeviceManager._registry.values())


def free_port(port, retries=3):
    """Find and kill any process using the specified port."""
    current_pid = os.getpid()  # ← on s'exclut
    for _ in range(retries):
        found_process = False

        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.pid != current_pid:  # ← ajout
                found_process = True
                try:
                    proc = psutil.Process(conn.pid)
                    if proc.is_running():
                        proc.terminate()
                        proc.wait(timeout=3)
                        logging.info("Terminated process %s using port %s", conn.pid, port)
                    else:
                        logging.info("Process %s was not running", conn.pid)
                except psutil.NoSuchProcess:
                    logging.warning("Process %s not found", conn.pid)
                except psutil.AccessDenied:
                    logging.warning("Access denied to process %s. Attempting force kill...", conn.pid)
                    try:
                        os.kill(conn.pid, signal.SIGKILL)
                        logging.info("Force-killed process %s", conn.pid)
                    except Exception as e:
                        logging.error("Unable to force kill process %s: %s", conn.pid, e)
                except Exception as e:
                    logging.error("Could not terminate process %s on port %s: %s", conn.pid, port, e)

        if not found_process:
            logging.info("No processes found on port %s", port)
            return
        else:
            time.sleep(1)
    logging.warning("Retries exhausted. Unable to clear the port completely.")


def process_device_queue() -> None:
    POLL_SLEEP = 0.5
    MAX_RETRIES = 3
    RETRY_SLEEP = 1
    try:
        while not stop_event_monitoring.is_set():
            row = get_next_task_from_queue()
            if not row:
                time.sleep(POLL_SLEEP)
                continue

            task_id, raw = row
            try:
                task = json.loads(raw)
                ctx = DeviceManager.get(task["machineId"])
                op = task["operation"]
                pin = task["user_pin"]
            except Exception as exc:
                logging.error("Tâche #%s invalide : %s", task_id, exc)
                mark_task_as_completed(task_id)
                continue

            if ctx is None:
                logging.warning("⏸️ Tâche #%s : machine %s inconnue, mise en attente "
                                "(sera rejouée dès que la machine apparaît)",
                                task_id, task.get("machineId"))
                defer_task(task_id)
                continue

            adapter = ctx.adapter

            ok = False
            unreachable = False
            last_exc = None

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    # ⚠️ La préparation de la session et la commande sont dans LE
                    # MÊME verrou. Les séparer laissait le thread temps réel
                    # s'intercaler entre les deux et consommer, avec un GetRTLog,
                    # le premier appel de la session fraîchement rouverte — le
                    # seul qui aboutit sur un C3.
                    with ctx.lock:
                        if isinstance(adapter, PlcommAdapter):
                            # Une session C3 cesse de servir les commandes après
                            # quelques secondes de polling temps réel : seule la
                            # première commande qui suit un Connect aboutit, les
                            # suivantes expirent (-2). La version de juin masquait
                            # ce comportement en reconnectant sur chaque -2 de
                            # GetRTLog ; en traitant -2 comme bénin, ce
                            # rafraîchissement implicite a disparu.
                            #
                            # On rafraîchit donc explicitement la session avant
                            # chaque commande. attempt_c3_reconnection déconnecte
                            # AVANT de reconnecter : la règle du handle unique est
                            # respectée, et le handle neuf est republié dans ctx
                            # pour le thread temps réel comme pour l'adapter.
                            if not attempt_c3_reconnection(ctx, max_retries=2):
                                raise ConnectionError(
                                    f"C3 {ctx.machine.addresseip} : session non "
                                    f"rétablie pour la commande")
                        elif isinstance(adapter, ADMSAdapter):
                            # PUSH: pas besoin de connect, l'appareil est déjà connecté
                            if not adapter.is_connected():
                                raise RuntimeError("Appareil PUSH non connecté")
                        # Pas de adapter.connect() ici pour les ZKEM : _ensure_conn
                        # s'en charge, et sous zk_sdk_lock. L'appel direct entrait
                        # dans le SDK sans verrou — exactement ce qui fait planter
                        # le process quand le thread RT y est déjà.

                        if op == "ADD_USER":
                            ok = adapter.add_user(pin,
                                                  task["user_name"],
                                                  task["card_no"],
                                                  task["start_date"],
                                                  task["end_date"])
                            if ok:
                                ok = adapter.authorize_user(pin)

                        elif op == "DELETE_USER":
                            ok = adapter.delete_user(pin)

                        elif op == "AUTHORIZE_USER":
                            ok = adapter.authorize_user(pin)

                        elif op == "UNAUTHORIZE_USER":
                            ok = adapter.unauthorize_user(pin)

                        elif op == "ADD_FINGERPRINT":
                            fingerprint_template_base64 = task["fingerprint_template"]
                            template_bytes = base64.b64decode(fingerprint_template_base64)
                            logging.info(f"Decoded fingerprint template: {len(template_bytes)} bytes")
                            ok = adapter.add_fingerprint(
                                pin,
                                fingerprint_template=template_bytes,
                                finger_id=task["finger_id"]
                            )
                        elif op == "REMOVE_FINGERPRINT":
                            ok = adapter.delete_fingerprint(pin, task["finger_id"])
                        else:
                            raise ValueError(f"Opération inconnue : {op}")

                    if ok:
                        logging.info("✅ Tâche #%s terminée (%s / %s)",
                                     task_id, ctx.machine.alias, op)
                        mark_task_as_completed(task_id)
                        break
                    else:
                        raise RuntimeError("SDK a renvoyé False")

                except ConnectionError as exc:
                    # La machine ne répond même pas au TCP (coupure secteur,
                    # câble débranché…). Insister coûterait 3 timeouts pour
                    # CETTE tâche, et la file étant FIFO, toutes les tâches des
                    # autres machines attendraient derrière. On sort aussitôt.
                    last_exc, unreachable = exc, True
                    break

                except Exception as exc:
                    last_exc = exc
                    log_memory_usage(message=f"task_#{task_id}_fail")
                    logging.warning("⚠️ Tâche #%s échec essai %s/%s : %s",
                                    task_id, attempt, MAX_RETRIES, exc,
                                    exc_info=True)
                    time.sleep(RETRY_SLEEP)

            if ok:
                continue

            if unreachable:
                # Surtout pas COMPLETED : la tâche était jusqu'ici détruite, donc
                # l'adhérent n'était jamais programmé sur une machine éteinte,
                # même après son retour. Elle est rejouée dès que la machine
                # redevient joignable (voir refresh_machines_loop).
                logging.warning("⏸️ Tâche #%s : %s injoignable, mise en attente "
                                "(rejouée au retour de la machine)",
                                task_id, ctx.machine.addresseip)
                defer_task(task_id)
            else:
                logging.error("❌ Tâche #%s abandonnée après %s échecs : %s",
                              task_id, MAX_RETRIES, last_exc)
                mark_task_as_completed(task_id)
    finally:
        pythoncom.CoUninitialize()


def run_device_queue_supervised() -> None:
    """
    Relance la boucle de traitement des tâches si elle meurt.

    process_device_queue appelle SQLite hors de tout try — get_next_task_from_queue,
    defer_task, mark_task_as_completed. Une base verrouillée suffisait donc à tuer
    le thread pour de bon : plus aucune demande d'accès traitée, sans que rien ne
    le signale (le process reste vivant, /health répondait « ok »). Le risque a
    augmenté depuis que le rafraîchissement écrit lui aussi dans cette base.
    """
    while not stop_event_monitoring.is_set():
        try:
            pythoncom.CoInitialize()
            process_device_queue()   # son propre finally fait le CoUninitialize
            return                   # sortie normale = arrêt demandé
        except Exception:
            logging.exception("💥 File de tâches interrompue — relance dans 3s")
            time.sleep(3)


def process_message_pointage(message):
    try:
        json_message = json.loads(message)
        logging.info("Received message from new access request topic from tenant %s: %s", tenant, message)
        required_keys = ["userPin", "operation", "cardNo", "startDate", "endDate", "machines"]
        if all(key in json_message for key in required_keys) and str(json_message.get("gymBranchId")) == gym_branch_id:
            for machine in json_message.get("machines", []):
                if "addresseip" in machine and "port" in machine:
                    add_task_to_queue({
                        "machineId": machine["id"],
                        "ip_address": machine["addresseip"],
                        "port": str(machine["port"]),
                        "user_pin": json_message["userPin"],
                        "user_name": json_message["username"],
                        "operation": json_message["operation"],
                        "card_no": json_message["cardNo"],
                        "start_date": json_message["startDate"],
                        "end_date": json_message["endDate"]
                    })
                else:
                    logging.error("Missing required fields in machine configuration.")
    except Exception as e:
        log_memory_usage(message="process_pointage_error")
        logging.error("Error processing pointage_client json_message: %s", e)


def process_fingerprint_actions(message):
    json_message = json.loads(message)
    logging.info("Received message from fingerprint actions topic from tenant %s: %s", tenant, message)
    required_keys = ["pin", "operation", "fingerprint_template", "finger_id"]

    # Filtre de branche.
    #
    # Le topic fingerprint_actions_<tenant> est consommé par TOUS les postes du
    # tenant, chacun poussant ensuite vers ses propres machines : un message
    # atteignait donc toutes les branches. Or un adhérent ne doit être enrôlé
    # que sur les branches couvertes par ses abonnements — et un SenseFace ne
    # retient que 60 empreintes au total, capacité vite épuisée si tout le monde
    # est diffusé partout.
    #
    # Le message porte désormais gymBranchId. Absent, on garde l'ancien
    # comportement pour rester compatible avec les postes non encore à jour.
    cible = json_message.get("gymBranchId")
    if cible is not None and str(cible) != str(gym_branch_id):
        logging.info("Empreinte ignorée : message destiné à la branche %s, "
                     "ce poste est sur la branche %s", cible, gym_branch_id)
        return

    machines = machineService.get_access_machines(gym_branch_id, tenant)
    if all(key in json_message for key in required_keys):
        for machine in machines:
            add_task_to_queue({
                "machineId": machine.id,
                "ip_address": machine.addresseip,
                "port": str(machine.port),
                "user_pin": json_message["pin"],
                "operation": json_message["operation"],
                "finger_id": json_message["finger_id"],
                "fingerprint_template": json_message["fingerprint_template"]
            })


logger = logging.getLogger(__name__)


def consume_pointage_client():
    while not stop_event_kafka.is_set():
        try:
            pointage_kafka.consume(topic='new_access_request_' + tenant, on_message=process_message_pointage)
        except Exception as e:
            log_memory_usage(message="pointage_consumer_error")
            logging.error("Error in pointage client consumption loop: %s", e)
            time.sleep(1)


def consume_fingerprint_client():
    while not stop_event_kafka.is_set():
        try:
            fingerprint_kafka.consume(topic='fingerprint_actions_' + tenant, on_message=process_fingerprint_actions)
        except Exception as e:
            log_memory_usage(message="fingerprint_consumer_error")
            logging.error("Error in fingerprint actions consumption loop: %s", e)
            time.sleep(1)


@app.route('/getFace/<int:user_pin>/<int:gym_branch_id>/<int:machine_id>', methods=['GET'])
def capture_fingerprint_api(user_pin, gym_branch_id, machine_id):
    try:
        payload = process_user_photo(str(user_pin), str(gym_branch_id), machine_id)
        if payload:
            return jsonify(payload), 200
        else:
            return jsonify({"error": "Photo not found or machine not valid"}), 400
    except Exception as e:
        logging.error(f"❌ Error in capture_fingerprint_api: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/curentconf', methods=['GET'])
def curentconf():
    try:
        payload = {
            "gymbranchId": currentGymBranchId,
            "tenant": tenant
        }
        if payload:
            return jsonify(payload), 200
        else:
            return jsonify({"error": "Photo not found or machine not valid"}), 400
    except Exception as e:
        logging.error(f"❌ Error in capture_fingerprint_api: {e}")
        return jsonify({"error": str(e)}), 500


def consume_publish_photo():
    def handle_photo_publish(message):
        try:
            data = json.loads(message)
            gym_branch_id = data.get("gymBranchId")
            user_pin = str(data.get("userPin"))
            machine_id = data.get("machineId")
            ip = data.get("addresseip")
            port = data.get("port")

            process_user_photo(user_pin, gym_branch_id, machine_id, ip, port)

        except Exception as e:
            logging.error(f"❌ Error processing launch_publish_photo message: {e}")

    backoff = 1
    while not stop_event_kafka.is_set():
        try:
            publish_photo_kafka.consume(topic=TOPIC_CONSUME_PUBLISH_PHOTO, on_message=handle_photo_publish)
            backoff = 1
        except Exception as e:
            msg = str(e)
            log_memory_usage(message="photo_publish_consumer_error")
            logging.error("Error in publish photo consumption loop: %s", e)

            if "UNKNOWN_TOPIC_OR_PART" in msg or "UnknownTopicOrPartition" in msg:
                time.sleep(60)
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


def process_user_photo(user_pin: str, gym_branch_id: str, machine_id: int, ip: str = None, port: int = None):
    # Comparaison en str : l'appelant Kafka fournit un int, l'URL Flask un int,
    # currentGymBranchId est une str. Sans cast la condition était toujours vraie
    # et la fonction sortait systématiquement.
    if str(gym_branch_id) != str(currentGymBranchId):
        return None

    ctx = get_device_context(machine_id)
    if not ctx:
        logging.error(f"No context found for machine ID {machine_id}")
        return None

    adapter = ctx.adapter
    with ctx.lock:
        photo_path = f"C:/temp/{user_pin}.jpg"
        success = adapter.download_user_photo(user_pin, photo_path)

        if success:
            with open(photo_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")

            payload = {
                "userPin": user_pin,
                "photo": encoded
            }
            publish_photo_kafka.produce(TOPIC_PRODUCE_PUBLISH_PHOTO, json.dumps(payload))
            logging.info(f"✅ Published photo for user {user_pin} to Kafka.")
            return payload
        else:
            logging.error(f"❌ Failed to download photo for user {user_pin}.")
            return None


def start_kafka_consumers():
    topics = [
        f'new_access_request_{tenant}',
        TOPIC_CONSUME_PUBLISH_PHOTO,
        f'fingerprint_actions_{tenant}',
    ]
    for topic in topics:
        pointage_kafka.ensure_topic(topic)

    pointage_thread = threading.Thread(target=consume_pointage_client, daemon=True, name="PointageClientThread")
    photo_publish_thread = threading.Thread(target=consume_publish_photo, daemon=True, name="PhotoPublishThread")
    fingerprint_actions_thread = threading.Thread(target=consume_fingerprint_client, daemon=True,
                                                  name="FingerprintActionsThread")

    pointage_thread.start()
    photo_publish_thread.start()
    fingerprint_actions_thread.start()
    logging.info("Kafka consumer threads started")


def cleanup_resources(driver=None):
    """Clean up application resources."""
    logging.info("Initiating cleanup...")
    stop_event_monitoring.set()
    stop_event_kafka.set()
    logging.info("Application shutdown complete")


from werkzeug.utils import secure_filename
from pathlib import Path

UPLOAD_DIR = Path(TEMP_DIR) / "faces"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

document_manager_service = DocumentManagerService()

JPEG_MAGIC = b"\xff\xd8\xff"


def _write_photo_atomically(dst_path: Path, data: bytes) -> None:
    """Écrit en .part puis renomme : on ne laisse jamais un JPG tronqué derrière."""
    tmp = dst_path.with_name(dst_path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dst_path)


@app.route('/face/upload', methods=['POST'])
def upload_face_multipart():
    pin = request.form.get('pin')
    gym_branchId = request.form.get('gymBranchId')
    file = request.files.get('photo')

    if not all([pin, gym_branchId]):
        abort(400, "pin et gymBranchId sont requis")

    if str(gym_branchId) != str(currentGymBranchId):
        abort(400, "gymBranchId ne correspond pas à cette instance")

    dst_name = f"verify_biophoto_9_{pin}.jpg"
    dst_path = UPLOAD_DIR / secure_filename(dst_name)

    # Résolution de la photo : celle envoyée dans la requête, sinon MinIO.
    # On ne réutilise PLUS un fichier déjà présent sur ce PC : un JPG vide écrit
    # une seule fois empoisonnait le poste définitivement, la branche « le
    # fichier existe » l'emportant à tous les envois suivants.
    if file and file.filename:
        data = file.read()
        if not data:
            logging.error("❌ Photo vide reçue pour le PIN %s", pin)
            return jsonify({"pin": pin, "error": "La photo reçue est vide"}), 400
        if not data.startswith(JPEG_MAGIC):
            logging.error("❌ Fichier non-JPEG reçu pour le PIN %s (%s octets)",
                          pin, len(data))
            return jsonify({"pin": pin,
                            "error": "Le fichier reçu n'est pas un JPEG"}), 400
        _write_photo_atomically(dst_path, data)
        logging.info("📥 Photo enregistrée : %s (%s octets)", dst_path, len(data))

    elif document_manager_service.download_faceid_photo(pin, tenant, dst_path):
        logging.info("☁️ Biophoto récupérée depuis MinIO : %s", dst_path)

    else:
        logging.error("❌ Aucune biophoto disponible pour le PIN %s", pin)
        return jsonify({
            "pin": pin,
            "error": "Aucune biophoto pour ce PIN, ni dans la requête ni dans "
                     "MinIO : une nouvelle saisie est nécessaire"
        }), 404

    report = {}
    MAX_UPLOAD_RETRIES = 3
    RETRY_DELAY = 1

    for ctx in get_all_device_contexts():
        branch = ctx.gym_branch_id if app_version == "v2" else ctx.gymBranchId
        if str(branch) != str(currentGymBranchId):
            continue

        adapter = ctx.adapter
        ok, last_error = False, None

        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                ok = adapter.upload_user_photo(pin, str(dst_path))
                if ok:
                    break
                last_error = "le SDK a renvoyé False"
                logging.warning("⏱️ Tentative %s/%s échouée pour %s",
                                attempt, MAX_UPLOAD_RETRIES, ctx.machine.addresseip)
            except Exception as ex:
                last_error = str(ex)
                logging.exception("❌ Upload vers %s (essai %s) : %s",
                                  ctx.machine.addresseip, attempt, ex)
            time.sleep(RETRY_DELAY)

        report[ctx.machine.addresseip] = "OK" if ok else f"KO: {last_error}"

    total = len(report)
    succeeded = sum(1 for v in report.values() if v == "OK")

    # On ne répond plus 200 quand tout a échoué : le front y lisait un succès et
    # l'agent d'accueil n'avait aucun moyen de savoir que rien n'était parti.
    if total == 0:
        status = 503
    elif succeeded == 0:
        status = 500
    elif succeeded < total:
        status = 207
    else:
        status = 200

    logging.info("📸 Face photo PIN %s → %s/%s machine(s) OK", pin, succeeded, total)

    return jsonify({
        "pin": pin,
        "result": report,
        "succeeded": succeeded,
        "total": total,
    }), status


def pousser_gabarit_local(pin, template_bytes, finger_id):
    """Pousse un gabarit vers toutes les machines de la branche COURANTE.

    Extrait de /fingerprint/upload pour être partagé avec /fingerprint/push :
    la logique de session C3 avait déjà été oubliée une fois dans un chemin sur
    deux, ce qui a coûté un diagnostic entier. Un seul endroit désormais.

    Retourne (resultats_par_ip, nb_succes, nb_machines).
    """
    resultats, succes, total = {}, 0, 0

    for ctx in get_all_device_contexts():
        branch_id = ctx.gym_branch_id if app_version == "v2" else ctx.gymBranchId
        if str(branch_id) != str(currentGymBranchId):
            continue

        total += 1
        adapter = ctx.adapter
        ip = ctx.machine.addresseip

        try:
            logging.info("Envoi de l'empreinte vers %s (id %s)", ip, ctx.machine.id)
            with ctx.lock:
                # Une session C3 ne répond que ~3,5 s : on la renouvelle DANS le
                # verrou, sinon le thread temps réel consomme le premier appel.
                if isinstance(adapter, PlcommAdapter):
                    if not attempt_c3_reconnection(ctx, max_retries=2):
                        raise ConnectionError(
                            f"C3 {ip} : session non rétablie pour l'envoi d'empreinte")

                ok = adapter.add_fingerprint(user_id=pin,
                                             fingerprint_template=template_bytes,
                                             finger_id=finger_id)

            if ok:
                resultats[ip] = "SUCCESS"
                succes += 1
                logging.info("Empreinte déposée sur %s", ip)
            else:
                resultats[ip] = "FAILED"
                logging.error("Échec du dépôt d'empreinte sur %s", ip)
        except Exception as exc:
            resultats[ip] = f"ERROR: {exc}"
            logging.error("Exception lors de l'envoi vers %s : %s", ip, exc)

    return resultats, succes, total


@app.route('/fingerprint/push', methods=['POST'])
def push_fingerprint():
    """Renvoie une empreinte DÉJÀ enregistrée vers les machines de cette branche.

    Contrairement à /fingerprint/upload, ne capture rien : le gabarit vient du
    serveur. Le fan-out vers les autres branches de l'adhérent est publié sur
    Kafka par gym-management, qui seul connaît ses abonnements actifs.
    """
    try:
        data = request.get_json() or {}
        manquants = [c for c in ("pin", "template") if not data.get(c)]
        if manquants:
            return jsonify({"error": f"champ(s) requis manquant(s) : {', '.join(manquants)}"}), 400

        pin = data["pin"]
        finger_id = data.get("fingerId", data.get("finger_id"))
        if finger_id is None:
            return jsonify({"error": "champ requis manquant : fingerId"}), 400

        try:
            template_bytes = base64.b64decode(data["template"])
        except Exception as exc:
            return jsonify({"error": f"gabarit illisible (base64 attendu) : {exc}"}), 400

        logging.info("Renvoi d'empreinte : PIN=%s doigt=%s (%s octets)",
                     pin, finger_id, len(template_bytes))

        resultats, succes, total = pousser_gabarit_local(pin, template_bytes, finger_id)

        if total == 0:
            return jsonify({"message": "Aucune machine active sur cette branche",
                            "results": {}, "succeeded": 0, "total": 0}), 200
        if succes == 0:
            return jsonify({"error": f"Échec sur les {total} machine(s)",
                            "results": resultats, "succeeded": 0, "total": total}), 500
        statut = 200 if succes == total else 207
        return jsonify({"message": f"Empreinte envoyée à {succes}/{total} machine(s)",
                        "results": resultats, "succeeded": succes, "total": total}), statut

    except Exception as exc:
        logging.error("Erreur dans /fingerprint/push : %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route('/fingerprint/upload', methods=['POST'])
def upload_fingerprint():
    try:
        data = request.get_json()
        if not data or "pin" not in data:
            return jsonify({"error": "Missing 'pin' in request"}), 400

        pin = data["pin"]
        gym_branch_id = data.get("gymBranchId", currentGymBranchId)
        finger_id = data.get("fingerId")

        if str(gym_branch_id) != str(currentGymBranchId):
            return jsonify({"error": "gymBranchId mismatch"}), 400

        logging.info(f" Starting fingerprint upload for PIN {pin}")

        capture = FingerprintCapture()
        template_bytes, images = capture.capture_fingerprint(save_file=False)

        if not template_bytes:
            # « Failed to capture fingerprint » couvrait indistinctement une DLL
            # absente, un lecteur débranché et un doigt mal posé. Le front — et
            # surtout la personne devant l'écran — ne pouvait rien en faire.
            cause = getattr(capture, "derniere_erreur", "") or \
                "Capture interrompue : aucun template exploitable après 3 essais."
            logging.error("Capture d'empreinte échouée pour le PIN %s — %s", pin, cause)
            return jsonify({"error": "Failed to capture fingerprint",
                            "cause": cause}), 500

        logging.info(f" Fingerprint captured successfully. Template size: {len(template_bytes)} bytes")

        results, success_count, total_count = pousser_gabarit_local(
            pin, template_bytes, finger_id)

        python_bytes = bytes(bytearray(template_bytes))
        encoded_template = base64.b64encode(python_bytes).decode('utf-8')
        # Volontairement SANS gymBranchId : le filtre de branche ajouté dans
        # process_fingerprint_actions ignore les messages destinés à une autre
        # branche. Le renseigner ici ferait porter au message la branche qui
        # vient déjà de recevoir l'empreinte en direct — il ne servirait plus à
        # rien, et le fan-out de la capture disparaîtrait en silence.
        #
        # La capture continue donc de diffuser à toutes les branches du tenant,
        # comme avant. Seuls les messages émis par gym-management (bouton de
        # renvoi) portent une branche et sont donc ciblés.
        # ⚠️ À revoir : cette diffusion large a le même défaut de capacité que
        # celui qui a motivé le filtre (60 empreintes max sur un SenseFace).
        payload = {"fingerprint_template": encoded_template, "pin": pin,
                   "operation": Operation.ADD_FINGERPRINT.value, "finger_id": finger_id}

        if success_count == 0:
            return jsonify({
                "error": f"Failed to upload fingerprint to all {total_count} machines",
                "results": results
            }), 500
        elif success_count < total_count:
            fingerprint_kafka.produce("fingerprint_actions_" + tenant, payload)
            return jsonify({
                "warning": f"Partial success: {success_count}/{total_count} machines",
                "results": results,
                "pin": pin,
                "fingerprint_template": encoded_template
            }), 207
        else:
            fingerprint_kafka.produce("fingerprint_actions_" + tenant, payload)
            return jsonify({
                "message": f"Fingerprint uploaded successfully to all {total_count} machines",
                "results": results,
                "pin": pin,
                "fingerprint_template": encoded_template
            }), 200

    except Exception as e:
        logging.exception(f" Fatal error in fingerprint upload: {e}")
        return jsonify({"error": f"Exception during fingerprint upload: {str(e)}"}), 500


@app.route('/config/gymBranchId', methods=['GET'])
def get_gym_branch_id():
    return {"gymBranchId": currentGymBranchId}


from flask import request, abort


@app.route('/health', methods=['GET'])
def health():
    """
    Sonde de vivacité, consommée par Electron toutes les 10 s.

    N'effectue AUCUN accès réseau — ni TCP vers les machines, ni HTTP vers le
    cloud : une sonde qui peut bloquer ne sert à rien. Elle ne lit que des états
    déjà en mémoire, plus un COUNT SQLite.

    `watchdogStaleSeconds` est ce qui permet de détecter un process vivant mais
    figé — le cas qui laissait les salles sans pointage sans que personne ne le
    voie, puisque le process répondait toujours.
    """
    try:
        contexts = list(get_all_device_contexts())
        machines = []
        for ctx in contexts:
            adapter = ctx.adapter
            machines.append({
                "id": ctx.machine.id,
                "ip": ctx.machine.addresseip,
                "type": ctx.machine.type,
                "rtConnected": bool(getattr(adapter, "connected", False)),
            })

        try:
            conn = sqlite3.connect(DB_FILE, timeout=2)
            pending = conn.execute(
                "SELECT COUNT(*) FROM task_queue WHERE status = 'PENDING'"
            ).fetchone()[0]
            conn.close()
        except Exception:
            pending = None

        stale = None
        if watchdog is not None:
            stale = round(time.time() - watchdog.last_tick, 1)

        # Un thread de fond mort ne tue pas le process : la file de tâches ou un
        # consumer Kafka pouvait disparaître sans que rien ne le signale, et il
        # fallait redémarrer l'application à la main. On l'expose pour qu'Electron
        # relance de lui-même.
        alive = {t.name for t in threading.enumerate()}
        expected = ("DeviceQueueThread", "MachineWatchdog", "MachineRefresh",
                    "PointageClientThread", "PhotoPublishThread",
                    "FingerprintActionsThread")
        threads = {name: (name in alive) for name in expected}
        dead = [name for name, ok in threads.items() if not ok]

        if watchdog is None:
            # Flask répond avant que les machines ne soient chargées : on le dit,
            # pour qu'Electron patiente au lieu de conclure à une panne.
            status = "starting"
        elif dead:
            status = "degraded"
            logging.error("🩺 /health : thread(s) critique(s) absent(s) : %s",
                          ", ".join(dead))
        elif stale > 120:
            # Le watchdog tourne toutes les 10s et rafraîchit son tick même
            # pendant son backoff : au-delà de 120s il est réellement figé.
            status = "degraded"
        else:
            status = "ok"

        return jsonify({
            "status": status,
            "threads": threads,
            "threadsDead": dead,
            "uptimeSeconds": round(time.time() - APP_STARTED_AT, 1),
            "tenant": tenant,
            "gymBranchId": currentGymBranchId,
            "version": app_version,
            "watchdogStaleSeconds": stale,
            "machines": machines,
            "machinesConnected": sum(1 for m in machines if m["rtConnected"]),
            "machinesTotal": len(machines),
            "queuePending": pending,
        }), 200

    except Exception as exc:
        logging.exception("Erreur /health : %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route('/api/version', methods=['GET'])
def get_version():
    """Retourne la version en cours (v1 ou v2)."""
    return jsonify({"version": app_version}), 200


@app.route('/api/test/add_user', methods=['POST'])
def test_add_user():
    """
    Endpoint de test — ajoute un utilisateur directement sur une machine.
    Utile pour tester sans passer par Kafka ou Angular.

    Body JSON :
    {
        "machineId": 2044,
        "pin": "99999",
        "name": "Test User",
        "cardNo": "",
        "startDate": "20250101",
        "endDate": "20261231"
    }
    """
    try:
        data = request.get_json(force=True)
        machine_id = data.get("machineId")
        pin = str(data.get("pin", ""))
        name = data.get("name", "Test")
        card_no = data.get("cardNo", "")
        start_date = data.get("startDate", "")
        end_date = data.get("endDate", "")

        if not machine_id or not pin:
            return jsonify({"error": "machineId et pin sont requis"}), 400

        ctx = get_device_context(machine_id)
        if not ctx:
            return jsonify({"error": f"Machine {machine_id} non trouvée"}), 404

        adapter = ctx.adapter

        # Vérifier connexion
        connected = adapter.is_connected() if hasattr(adapter, 'is_connected') else getattr(adapter, 'connected', False)
        if not connected:
            return jsonify({
                "error": "Machine non connectée",
                "machine": ctx.machine.alias,
                "ip": ctx.machine.addresseip,
                "hint": "Vérifiez que la machine est en mode PUSH et pointe vers ce serveur sur le port 8088"
            }), 503

        # Tester add_user
        ok = adapter.add_user(pin, name, card_no, start_date, end_date)

        result = {
            "operation": "ADD_USER",
            "success": ok,
            "machine": ctx.machine.alias,
            "ip": ctx.machine.addresseip,
            "pin": pin,
            "name": name,
        }

        if ok:
            # Aussi tester authorize
            ok_auth = adapter.authorize_user(pin)
            result["authorize_success"] = ok_auth

        return jsonify(result), 200 if ok else 500

    except Exception as ex:
        logging.exception("Erreur test add_user: %s", ex)
        return jsonify({"error": str(ex)}), 500


@app.route('/api/test/delete_user', methods=['POST'])
def test_delete_user():
    """
    Endpoint de test — supprime un utilisateur.
    Body JSON : { "machineId": 2044, "pin": "99999" }
    """
    try:
        data = request.get_json(force=True)
        machine_id = data.get("machineId")
        pin = str(data.get("pin", ""))

        ctx = get_device_context(machine_id)
        if not ctx:
            return jsonify({"error": f"Machine {machine_id} non trouvée"}), 404

        adapter = ctx.adapter
        ok = adapter.delete_user(pin)
        return jsonify({"operation": "DELETE_USER", "success": ok, "pin": pin}), 200 if ok else 500

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.route('/api/test/open_door', methods=['POST'])
def test_open_door():
    """
    Endpoint de test — ouvre une porte.
    Body JSON : { "machineId": 2044, "duration": 5 }
    """
    try:
        data = request.get_json(force=True)
        machine_id = data.get("machineId")
        duration = int(data.get("duration", 5))

        ctx = get_device_context(machine_id)
        if not ctx:
            return jsonify({"error": f"Machine {machine_id} non trouvée"}), 404

        adapter = ctx.adapter

        if app_version == "v2":
            ok = adapter.open_door(door_no=1, duration=duration)
        elif hasattr(adapter, 'open_door'):
            ok = adapter.open_door(duration_seconds=duration) if hasattr(adapter, 'ACUnlock') else adapter.open_door(door_no=1, duration=duration)
        else:
            ok = False

        return jsonify({"operation": "OPEN_DOOR", "success": ok, "duration": duration}), 200 if ok else 500

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.route('/api/test/queue', methods=['GET'])
def test_queue_status():
    """Endpoint de test — affiche l'état de la queue et des commandes en attente."""
    try:
        # Commandes en attente dans la SQLite queue
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, task_data, status, created_at FROM task_queue ORDER BY created_at DESC LIMIT 20")
        tasks = []
        for row in cursor.fetchall():
            tasks.append({
                "id": row[0],
                "task": json.loads(row[1]) if row[1] else None,
                "status": row[2],
                "created_at": row[3]
            })
        conn.close()

        # Commandes ADMS en attente dans les adapters
        adapter_queues = {}
        for ctx in get_all_device_contexts():
            adapter = ctx.adapter
            queue_len = len(getattr(adapter, '_command_queue', []))
            adapter_queues[ctx.machine.alias] = {
                "machine_id": ctx.machine.id,
                "connected": adapter.is_connected() if hasattr(adapter, 'is_connected') else None,
                "sn": getattr(adapter, 'sn', None),
                "commands_pending": queue_len,
            }

        return jsonify({
            "version": app_version,
            "sqlite_tasks": tasks,
            "adapter_queues": adapter_queues
        }), 200

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.route('/api/devices', methods=['GET'])
def get_devices():
    """Retourne l'état de toutes les machines connectées."""
    devices = []
    for ctx in get_all_device_contexts():
        adapter = ctx.adapter
        info = {
            "id": ctx.machine.id,
            "alias": ctx.machine.alias,
            "ip": ctx.machine.addresseip,
            "port": ctx.machine.port,
            "original_type": ctx.machine.type,
            "mode": "PUSH/ADMS" if app_version == "v2" else ctx.machine.type,
        }
        if app_version == "v2":
            info["connected"] = adapter.is_connected()
            info["sn"] = adapter.sn
            info["last_seen"] = adapter.last_seen.isoformat() if adapter.last_seen else None
        else:
            info["connected"] = getattr(adapter, "connected", None)
        devices.append(info)
    return jsonify({"version": app_version, "devices": devices}), 200


@app.route('/api/machines/status', methods=['GET'])
def get_machines_status():
    machines = []
    for ctx in get_all_device_contexts():
        adapter = ctx.adapter
        machine = ctx.machine
        if app_version == "v2":
            connected = adapter.is_connected()
            last_seen_ts = int(adapter.last_seen.timestamp()) if adapter.last_seen else None
        else:
            connected = getattr(adapter, "connected", False) or (getattr(adapter, "handle", None) is not None)
            last_seen_ts = None
        machines.append({
            "type": "machine_status_changed",
            "machineId": machine.id,
            "alias": machine.alias,
            "ip": machine.addresseip,
            "port": machine.port,
            "machineType": machine.type,
            "connected": connected,
            "lastError": "",
            "lastSeen": last_seen_ts,
            "onlineSince": None,
            "offlineSince": None,
            "reconnectCount": 0,
            "eventCount": 0,
            "reason": "connected" if connected else "disconnected",
            "timestamp": int(time.time())
        })
    return jsonify({"count": len(machines), "machines": machines}), 200


@app.route('/api/machines/<int:machine_id>/reconnect', methods=['POST', 'OPTIONS'])
def reconnect_machine(machine_id):
    ctx = get_device_context(machine_id)
    if not ctx:
        return jsonify({"error": f"Machine {machine_id} not found"}), 404
    mtype = ctx.machine.type
    logging.info(f"🔄 Reconnect demandé pour machine {machine_id} type={mtype}")
    try:
        if app_version == "v2":
            logging.info(f"  v2 machine {machine_id} — reconnexion automatique via ADMS heartbeat")
            return jsonify({"status": "acknowledged", "message": "v2 device reconnection is automatic via heartbeat"}), 200

        if mtype == MachineType.C3.name or mtype == "C3":
            success = attempt_c3_reconnection(ctx)
            if success:
                return jsonify({"status": "reconnected"}), 200
            return jsonify({"error": "C3 reconnection failed"}), 500

        if mtype == MachineType.STANDALONE_NEW_FIRMWARE.name or mtype == "STANDALONE_NEW_FIRMWARE":
            adapter = ctx.adapter
            # disconnect()/connect() ne passent pas par _ensure_conn : il faut
            # prendre zk_sdk_lock explicitement. Ordre ctx.lock → zk_sdk_lock,
            # identique partout ailleurs, donc pas d'interblocage possible.
            with ctx.lock, zk_sdk_lock:
                adapter.disconnect()
                success = adapter.connect()
            if success:
                logging.info(f"✅ Standalone {machine_id} reconnecté")
                return jsonify({"status": "reconnected"}), 200
            return jsonify({"error": "Standalone reconnection failed"}), 500

        if mtype == "PUSH":
            if isinstance(ctx.adapter, ADMSAdapter):
                with ctx.lock:
                    ctx.adapter.disconnect()
                logging.info(f"🔌 PUSH {machine_id} marqué déconnecté — en attente du handshake")
                return jsonify({"status": "disconnected", "message": "PUSH device marked disconnected, waiting for handshake"}), 200
            return jsonify({"error": "PUSH adapter not found"}), 500

        return jsonify({"error": f"Unknown machine type: {mtype}"}), 400
    except Exception as e:
        logging.exception(f"❌ Reconnect error for machine {machine_id}: {e}")
        return jsonify({"error": str(e)}), 500


REQUIRED_TOP = {
    "gymBranchId", "operation",
    "userPin", "cardNo",
    "startDate", "endDate",
    "machines"
}
REQUIRED_MACHINE = {"id", "addresseip", "port", "type"}


@app.route('/tasks/access', methods=['POST'])
def enqueue_access_tasks():
    try:
        data = request.get_json(force=True)
    except Exception:
        abort(400, "Payload JSON invalide")

    if not REQUIRED_TOP.issubset(data):
        missing = REQUIRED_TOP - data.keys()
        abort(400, f"Champs manquants : {', '.join(missing)}")

    if str(data["gymBranchId"]) != currentGymBranchId:
        abort(400, "gymBranchId ne correspond pas à la configuration locale")

    if not isinstance(data["machines"], list) or not data["machines"]:
        abort(400, "machines doit être une liste non vide")

    queued = 0
    for m in data["machines"]:
        if not REQUIRED_MACHINE.issubset(m):
            abort(400, "Chaque machine doit contenir id, addresseip, port, type")

        task = {
            "machineId": m["id"],
            "ip_address": m["addresseip"],
            "port": str(m["port"]),
            "user_pin": data["userPin"],
            "user_name": data["username"],
            "operation": data["operation"],
            "card_no": data["cardNo"],
            "start_date": data["startDate"],
            "end_date": data["endDate"]
        }
        add_task_to_queue(task)
        queued += 1

    return jsonify({"status": "queued", "tasksQueued": queued}), 201


REQUIRED_OPEN = {"gymBranchId", "machineId"}


@app.route('/door/open', methods=['POST'])
def open_door_api():
    try:
        data = request.get_json(force=True) or {}

        if not REQUIRED_OPEN.issubset(data):
            missing = REQUIRED_OPEN - data.keys()
            abort(400, f"Champs manquants : {', '.join(missing)}")

        gym_branch_id = str(data["gymBranchId"])
        machine_id = data["machineId"]
        duration = int(data.get("duration", 5))
        door_no = int(data.get("door", 1))
        pin = data.get("pin")
        porte_type = data.get("porte_type", "ENTREE")

        ctx = get_device_context(machine_id)
        if not ctx:
            abort(404, f"Machine {machine_id} inconnue dans DeviceManager")

        adapter = ctx.adapter

        with ctx.lock:
            if app_version == "v2":
                ok = adapter.open_door(door_no=door_no, duration=duration)
            elif isinstance(adapter, PlcommAdapter):
                ok = adapter.open_door(door_no=door_no, duration=duration)
            elif isinstance(adapter, ZkemAdapter):
                ok = adapter.open_door(duration_seconds=duration)
            elif isinstance(adapter, ADMSAdapter):
                ok = adapter.open_door(door_no=door_no, duration=duration)
            else:
                abort(400, f"Type d'adapter non supporté : {type(adapter).__name__}")

        if not pin:
            return jsonify({
                "status": "OK" if ok else "ERROR",
                "machineId": machine_id,
                "duration": duration
            }), 200 if ok else 500

        try:
            now = datetime.now()
            dt_str = now.strftime("%Y-%m-%d %H:%M:%S")
            state_code = 0

            payload_json = make_rt_json(
                machine_id=ctx.machine.id,
                ip=ctx.machine.addresseip,
                mtype=ctx.machine.type,
                pin=int(pin),
                state_code=state_code,
                dt=dt_str,
                door_id=door_no,
                card_no=None,
                gym_branch_id=gym_branch_id,
                porte_type=porte_type,
                tenant=tenant,
            )

            payload = json.loads(payload_json)
            kafka.produce(KAFKA_TOPIC, payload)
            send_pointage(payload, gym_branch_id)

        except Exception as ex:
            logging.exception("Erreur lors de l'envoi du pointage manuel : %s", ex)

        return jsonify({
            "status": "OK" if ok else "ERROR",
            "machineId": machine_id,
            "duration": duration
        }), 200 if ok else 500

    except Exception as ex:
        logging.exception("Erreur /door/open : %s", ex)
        return jsonify({"status": "ERROR", "message": str(ex)}), 500


@app.route('/getFingerprints/<int:user_pin>/<int:gym_branch_id>/<int:machine_id>', methods=['GET'])
def get_fingerprints_api(user_pin, gym_branch_id, machine_id):
    try:
        if int(gym_branch_id) != int(currentGymBranchId):
            return jsonify({"error": "Machine non valide pour cette gymBranchId"}), 400

        ctx = get_device_context(machine_id)
        if not ctx:
            try:
                ms = AccessMachineService()
                machines = ms.get_access_machines(str(gym_branch_id), str(tenant))
                for m in machines:
                    if app_version == "v2":
                        DeviceManagerV2.register(m, tenant, gym_branch_id)
                    else:
                        DeviceManager.register(m, tenant, gym_branch_id)
                ctx = get_device_context(machine_id)
            except Exception as e:
                logging.error(f"❌ fallback register machines failed: {e}")

        if not ctx:
            return jsonify({"error": f"Machine ID {machine_id} introuvable côté service"}), 404

        adapter = ctx.adapter

        with ctx.lock:
            fps = adapter.get_fingerprints(str(user_pin)) or []

        payload = {
            "userPin": str(user_pin),
            "gymBranchId": int(gym_branch_id),
            "machineId": int(machine_id),
            "machineType": getattr(ctx.machine, "type", None),
            "fingerprints": fps
        }
        return jsonify(payload), 200

    except Exception as e:
        logging.error(f"❌ Error in get_fingerprints_api: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/adms/status', methods=['GET'])
def adms_status():
    try:
        server = ADMSServer()
        return jsonify(server.get_connected_devices()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/restart', methods=['POST'])
def soft_restart():
    """
    Recharge la configuration des machines et démarre le monitoring de celles
    qui manquent.

    Ne touche plus à stop_event_monitoring. L'ancienne version le levait puis le
    rabaissait 3 s plus tard, avec trois conséquences : le watchdog — qui dort
    10 s entre deux tours — ne voyait jamais le drapeau et survivait, pendant
    qu'un second watchdog était créé (threads RT en double vers chaque
    terminal) ; process_device_queue sortait définitivement de sa boucle, donc
    plus aucune tâche d'accès n'était traitée après un /restart ; et le
    monitoring mémoire s'arrêtait pour de bon.

    Un monitor mort est de toute façon relancé par le watchdog sous 10 s : il
    n'y a rien à arrêter ici.
    """
    logging.info("🔄 Rechargement de la configuration demandé...")

    def _do_reload():
        pythoncom.CoInitialize()
        try:
            machines = fetch_machines_with_retry(tenant, gym_branch_id, max_wait=30)
            if not machines:
                logging.error("❌ Rechargement : aucune machine récupérée")
                return
            logging.info("🔌 Machines rechargées : %s", machines)

            if app_version == "v2":
                from services.v2.adms_server_v2 import ADMSServerV2
                from services.v2.monitor_v2 import setup_attendance_callback
                adms_srv = ADMSServerV2()
                for m in machines:
                    ctx = DeviceManagerV2.register(m, tenant, gym_branch_id)
                    adms_srv.register_adapter(ctx.adapter)
                setup_attendance_callback(adms_srv, tenant, gym_branch_id)
                logging.info("✅ Rechargement v2 terminé")
                return

            added = 0
            for m in machines:
                if DeviceManager.get(m.id) is not None:
                    continue
                ctx = DeviceManager.register(m, tenant, gym_branch_id)
                register_machine_monitor(watchdog, m, ctx,
                                         tenant, gym_branch_id, adms_server)
                added += 1

            logging.info("✅ Rechargement terminé — %s machine(s) ajoutée(s). "
                         "Les monitors morts sont relancés par le watchdog.", added)
        except Exception as exc:
            logging.exception("❌ Rechargement de la configuration échoué : %s", exc)
        finally:
            pythoncom.CoUninitialize()

    threading.Thread(target=_do_reload, daemon=True, name="ConfigReload").start()
    return jsonify({"status": "reloading"}), 202


if __name__ == '__main__':
    def handle_sigterm(signum, frame):
        logging.info("🚨 Reçu SIGTERM, fermeture propre en cours...")
        cleanup_resources()
        sys.exit(0)

    # setup_logging() EN PREMIER : tant qu'il n'a pas posé le handler fichier,
    # tout ce qui est journalisé avant n'atteint jamais app.log. La ligne
    # "Current tenant" partait ainsi dans le vide, rendant impossible de compter
    # les redémarrages ou de vérifier l'identité du poste.
    setup_logging()

    # Environnement effectif, en clair et en premier.
    #
    # Un installeur construit depuis un poste configuré sur l'intégration a
    # expédié les pointages d'un client vers le mauvais broker pendant plusieurs
    # jours sans que rien ne le signale : Electron affichait la production (il
    # se replie sur app.yogym.co quand YOGYM_BASE_URL manque) pendant que le
    # pont publiait sur l'intégration. Ces deux lignes rendent l'incohérence
    # visible au premier coup d'œil dans app.log.
    from config import YOGYM_BASE_URL as _base_url
    _broker = os.getenv("KAFKA_BROKER", "<absent>")
    _env_nom = ("PRODUCTION" if "app.yogym.co" in (_base_url or "")
                else "INTEGRATION" if "integration.yogym.co" in (_base_url or "")
                else "INCONNU")
    logging.info("🌐 Environnement : %s", _env_nom)
    logging.info("🌐 YOGYM_BASE_URL=%s | KAFKA_BROKER=%s | KAFKA_TOPIC=%s",
                 _base_url, _broker, os.getenv("KAFKA_TOPIC", "rt_pointage"))
    if _env_nom == "INCONNU":
        logging.warning("⚠️ YOGYM_BASE_URL ne correspond à aucun environnement connu — "
                        "vérifier le .env livré à côté de YoGym.exe")

    signal.signal(signal.SIGTERM, handle_sigterm)
    start_ws_server()
    driver = None
    machineService = AccessMachineService()

    if len(sys.argv) < 3:
        logging.error("Expected arguments: TENANT GYM_BRANCH_ID [--version v1|v2]")
        sys.exit(1)

    # tenant / currentGymBranchId sont déjà résolus au chargement du module
    # (ils conditionnent les group.id Kafka) — on ne fait que les refléter ici.
    gym_branch_id = currentGymBranchId

    # Parse --version argument
    if "--version" in sys.argv:
        idx = sys.argv.index("--version")
        if idx + 1 < len(sys.argv):
            app_version = sys.argv[idx + 1].lower()
        else:
            logging.error("--version requires a value (v1 or v2)")
            sys.exit(1)
    else:
        app_version = "v1"

    if app_version not in ("v1", "v2"):
        logging.error("Version invalide: %s. Attendu: v1 ou v2", app_version)
        sys.exit(1)

    logging.info("Current tenant: %s    gymbranchid: %s    version: %s", tenant, gym_branch_id, app_version)

    try:
        # Surveillance mémoire en observation seule. Pas de redémarrage
        # automatique : le seuil portait sur la RAM SYSTÈME (Chrome et Electron
        # inclus), donc sans rapport avec ce process, et la relance était de
        # toute façon inopérante en build PyInstaller.
        start_memory_monitor(interval=30, stop_event=stop_event_monitoring)

        # Le serveur HTTP local démarre AVANT la récupération des machines. Sur
        # un poste dont le réseau n'est pas prêt, fetch_machines_with_retry peut
        # patienter plusieurs minutes : pendant ce temps Electron doit pouvoir
        # interroger /health, sinon il conclut à un pont mort et le tue en
        # boucle sans jamais lui laisser le temps d'aboutir.
        free_port(FLASK_PORT)
        threading.Thread(target=lambda: app.run(
            debug=False, host=FLASK_HOST, port=FLASK_PORT),
                         daemon=True, name="FlaskThread").start()

        machines = fetch_machines_with_retry(tenant, gym_branch_id)
        logging.info("Machines disponibles: %s", machines)

        if app_version == "v2":
            # ── v2 : Full PUSH/ADMS ──────────────────────────────────
            start_v2(
                machines=machines,
                tenant=tenant,
                gym_branch_id=gym_branch_id,
                stop_event=stop_event_monitoring,
                task_queue_functions={
                    "initialize_db": initialize_task_queue_db,
                    "add_task": add_task_to_queue,
                    "get_next": get_next_task_from_queue,
                    "mark_completed": mark_task_as_completed,
                }
            )
        else:
            # ── v1 : Legacy multi-protocole (C3 / STANDALONE / PUSH) ──
            initialize_task_queue_db()

            # Les machines DOIVENT être enregistrées avant le démarrage du thread
            # de queue. Sinon il dépile immédiatement les tâches héritées de la
            # session précédente alors que DeviceManager est encore vide, et les
            # écarte comme « machine inconnue » : 10 tâches perdues en une
            # journée, une à chaque redémarrage.
            for m in machines:
                DeviceManager.register(m, tenant, gym_branch_id)
                # Uniquement si la machine répond : sinon chaque tâche remise en
                # file coûterait une sonde TCP pour être aussitôt remise de côté.
                # Un C3 n'est jamais prêt à cet instant (son thread temps réel
                # n'a pas encore ouvert de session) : c'est refresh_machines_loop
                # qui rejouera ses tâches au cycle suivant.
                if machine_ready_for_requeue(m):
                    requeued = requeue_tasks_for_machine(m.id)
                    if requeued:
                        logging.info("♻️ %s tâche(s) en attente réactivée(s) "
                                     "pour la machine %s", requeued, m.id)

            threading.Thread(target=run_device_queue_supervised,
                             daemon=True,
                             name="DeviceQueueThread").start()

            watchdog = MachineWatchdog()

            adms_server = None
            if any(m.type == "PUSH" for m in machines):
                adms_server = ADMSServer(port=8088)
                adms_server.start()
                logging.info("🚀 Serveur ADMS démarré (port 8088)")

            for m in machines:
                register_machine_monitor(watchdog, m, DeviceManager.get(m.id),
                                         tenant, gym_branch_id, adms_server)

            watchdog.start()

            # Sans ça, une machine ajoutée en back-office reste inconnue jusqu'au
            # prochain lancement de l'application, et ses tâches d'accès sont
            # silencieusement perdues.
            threading.Thread(
                target=refresh_machines_loop,
                args=(watchdog, tenant, gym_branch_id, adms_server),
                daemon=True,
                name="MachineRefresh").start()
        # ───────────────────────────────────────────────────────────────

        start_kafka_consumers()

        def _get_all_devices_with_version():
            return [
                (ctx.machine, ctx.adapter, app_version)
                for ctx in get_all_device_contexts()
            ]
        start_machine_status_broadcast(_get_all_devices_with_version, interval=5)

        while True:
            time.sleep(0.01)

    except KeyboardInterrupt:
        logging.info("⏹️ Arrêt demandé par l'utilisateur")
        cleanup_resources(driver)
    except Exception as ex:
        log_memory_usage(message="fatal_error")
        logging.exception("Erreur fatale : %s", ex)
        cleanup_resources(driver)
        raise
