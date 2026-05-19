import base64
import os
import signal
import sys
import threading

import psutil
import logging
import time
import json
import sqlite3

import pythoncom

from kafka_service.kafkaservice import KafkaService
from services.DeviceMAnager import DeviceManager
from services.MonitorZkem import monitor_zkem
from services.adapters import PlcommAdapter
from services.machinesService import AccessMachineService
from services.MachineMonitor import monitor_machine

from flask import Flask, jsonify
from flask_cors import CORS
from queue import Queue
from dotenv import load_dotenv

from services.websocket import start_ws_server
from services.adms_adapter import ADMSAdapter
from services.adms_server import ADMSServer
from services.MonitorADMS import monitor_adms

# v2 imports
from services.v2.startup import start_v2
from services.v2.device_manager_v2 import DeviceManagerV2

# Version globale — "v1" (legacy multi-protocole) ou "v2" (full PUSH/ADMS)
app_version = "v1"

def get_app_data_dir():
    print("[INFO] Trying to get APPDATA environment variable...")
    app_data = os.environ.get('APPDATA')

    if not app_data:
        print("[WARN] APPDATA not found. Falling back to default path...")
        app_data = os.path.expanduser('~\\AppData\\Roaming')
    else:
        print(f"[INFO] APPDATA found: {app_data}")

    app_dir = os.path.join(app_data, 'desktop-app')
    print(f"[INFO] Full application directory path: {app_dir}")

    os.makedirs(app_dir, exist_ok=True)
    print("[INFO] Directory ensured (created if it didn't exist).")

    return app_dir


def initialize_env_file():
    """Initialize the .env file with default values if it doesn't exist."""
    if not os.path.exists(ENV_FILE_PATH):
        default_env_content = """# .env
KAFKA_BROKER=54.38.35.221:9094
KAFKA_GROUP_ID=group_c
KAFKA_TOPIC=rt_
GYM_BRANCH_ID=0

FLASK_HOST=0.0.0.0
FLASK_PORT=9998

PLCOMPRO_URL=plcommpro.dll
"""
        with open(ENV_FILE_PATH, 'w') as f:
            f.write(default_env_content)
        print(f"Created default .env file at: {ENV_FILE_PATH}")


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

# Flask application initialization
app = Flask(__name__)
CORS(app)

# Register route blueprints
from routes import register_blueprints
register_blueprints(app)

currentGymBranchId = int(os.getenv("GYM_BRANCH_ID"))
tenant = os.getenv("TENANT")

# Kafka service configuration
pointage_kafka = KafkaService(kafka_broker=KafkaBroker, group_id=f"pointage_group{currentGymBranchId}")
publish_photo_kafka = KafkaService(kafka_broker=KafkaBroker, group_id=f"photo_publish_group{currentGymBranchId}")
fingerprint_kafka = KafkaService(kafka_broker=KafkaBroker, group_id=f"fingerprint_group{currentGymBranchId}")

machineService = AccessMachineService()

# Global variables and synchronization primitives
device_handle = None
handle_lock = threading.RLock()
stop_event_monitoring = threading.Event()
stop_event_kafka = threading.Event()
monitoring_success = threading.Event()
device_queue = Queue()

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

# ── Sync shared state to app_context (used by route blueprints) ────────────
import app_context as _ctx
_ctx.app_version = app_version
_ctx.currentGymBranchId = currentGymBranchId
_ctx.tenant = tenant
_ctx.DB_FILE = DB_FILE
_ctx.TEMP_DIR = TEMP_DIR
_ctx.UPLOAD_DIR = os.path.join(TEMP_DIR, "faces")
_ctx.pointage_kafka = pointage_kafka
_ctx.publish_photo_kafka = publish_photo_kafka
_ctx.fingerprint_kafka = fingerprint_kafka
_ctx.machineService = machineService


# ------------------------------------------------------------------ #
# Watchdog — surveille les threads monitors et les redémarre si morts
# ------------------------------------------------------------------ #
class MachineWatchdog:
    """
    Surveille les threads de monitoring (C3 et ZKEM).
    Si un thread meurt → backoff exponentiel → redémarrage automatique.
    S'arrête proprement quand stop_event_monitoring est déclenché.
    """

    BASE_DELAY = 5
    MAX_DELAY  = 120
    CHECK_INTERVAL = 10  # secondes entre chaque vérification

    def __init__(self):
        # { machine_id: { "thread": Thread, "factory": callable, "delay": int } }
        self._entries: dict = {}
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
        self._entries[machine_id] = {
            "thread":  t,
            "factory": factory,
            "delay":   self.BASE_DELAY,
        }
        logging.info("🐕 Watchdog enregistré — machine %s (%s)", machine_id, t.name)

    def start(self) -> None:
        self._watcher.start()
        logging.info("🐕 Watchdog démarré")

    def _watch_loop(self) -> None:
        while not stop_event_monitoring.is_set():
            time.sleep(self.CHECK_INTERVAL)

            for mid, entry in self._entries.items():
                if stop_event_monitoring.is_set():
                    break

                if not entry["thread"].is_alive():
                    delay = entry["delay"]
                    logging.error(
                        "💀 [Watchdog] Thread mort — machine %s, "
                        "redémarrage dans %ss", mid, delay
                    )

                    # Sleep interruptible
                    elapsed = 0
                    while elapsed < delay and not stop_event_monitoring.is_set():
                        time.sleep(1)
                        elapsed += 1

                    if stop_event_monitoring.is_set():
                        break

                    new_thread = entry["factory"]()
                    new_thread.start()
                    entry["thread"] = new_thread
                    # Backoff exponentiel, reset à BASE_DELAY au prochain succès
                    entry["delay"] = min(delay * 2, self.MAX_DELAY)
                    logging.info(
                        "♻️ [Watchdog] Thread redémarré — machine %s (%s)",
                        mid, new_thread.name
                    )
                else:
                    # Thread vivant → reset du backoff
                    entry["delay"] = self.BASE_DELAY

        logging.info("🛑 [Watchdog] Arrêté")


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


def delete_completedTasks():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_queue WHERE status = 'COMPLETED'")
    conn.commit()
    conn.close()


# get_device_context / get_all_device_contexts → moved to app_context.py
get_device_context = _ctx.get_device_context
get_all_device_contexts = _ctx.get_all_device_contexts


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
                        print(f"Terminated process {conn.pid} using port {port}")
                    else:
                        print(f"Process {conn.pid} was not running")
                except psutil.NoSuchProcess:
                    print(f"Process {conn.pid} not found")
                except psutil.AccessDenied:
                    print(f"Access denied to process {conn.pid}. Attempting force kill...")
                    try:
                        os.kill(conn.pid, signal.SIGKILL)
                        print(f"Force-killed process {conn.pid}")
                    except Exception as e:
                        print(f"Unable to force kill process {conn.pid}: {e}")
                except Exception as e:
                    print(f"Could not terminate process {conn.pid} on port {port}: {e}")

        if not found_process:
            print(f"No processes found on port {port}")
            return
        else:
            time.sleep(1)
    print("Retries exhausted. Unable to clear the port completely.")


def process_device_queue() -> None:
    POLL_SLEEP = 0.5
    MAX_RETRIES = 3
    RETRY_SLEEP = 1
    WAIT_HANDLE = 1
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
                logging.warning("⚠️ Tâche #%s : machine %s non enregistrée, tâche ignorée",
                                task_id, task.get("machineId"))
                mark_task_as_completed(task_id)
                continue

            adapter = ctx.adapter

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if isinstance(adapter, PlcommAdapter):
                        waited = 0.0
                        while ctx.handle is None and waited < WAIT_HANDLE:
                            time.sleep(0.5)
                            waited += 0.5
                        if ctx.handle is None:
                            raise RuntimeError("Pas de handle C3 disponible")
                    elif isinstance(adapter, ADMSAdapter):
                        # PUSH: pas besoin de connect, l'appareil est déjà connecté
                        if not adapter.is_connected():
                            raise RuntimeError("Appareil PUSH non connecté")
                    else:
                        adapter.connect()

                    with ctx.lock:
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

                except Exception as exc:
                    logging.warning("⚠️ Tâche #%s échec essai %s/%s : %s",
                                    task_id, attempt, MAX_RETRIES, exc,
                                    exc_info=True)
                    time.sleep(RETRY_SLEEP)

            else:
                logging.error("❌ Tâche #%s abandonnée après %s échecs",
                              task_id, MAX_RETRIES)
                mark_task_as_completed(task_id)
    finally:
        pythoncom.CoUninitialize()


def process_message_pointage(message):
    try:
        json_message = json.loads(message)
        print(f"Received message from new access request  topic from trenant {tenant}:", message)
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
                    print("Error: Missing required fields in machine configuration.")
    except Exception as e:
        print(f"Error processing pointage_client json_message: {e}")


def process_fingerprint_actions(message):
    json_message = json.loads(message)
    print(f"Received message from new access request  topic from trenant {tenant}:", message)
    required_keys = ["pin", "operation", "fingerprint_template", "finger_id"]
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


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def consume_pointage_client():
    while not stop_event_kafka.is_set():
        try:
            pointage_kafka.consume(topic='new_access_request_' + tenant, on_message=process_message_pointage)
        except Exception as e:
            print(f"Error in pointage client consumption loop: {e}")
            time.sleep(1)


def consume_fingerprint_client():
    while not stop_event_kafka.is_set():
        try:
            fingerprint_kafka.consume(topic='fingerprint_actions_' + tenant, on_message=process_fingerprint_actions)
        except Exception as e:
            print(f"Error in fingerprint actions consumption loop: {e}")
            time.sleep(1)


# Routes /getFace, /curentconf → moved to routes/biometric_routes.py, routes/config_routes.py


def consume_publish_photo():
    def handle_photo_publish(message):
        try:
            data = json.loads(message)
            gym_branch_id = data.get("gymBranchId")
            user_pin = str(data.get("userPin"))
            machine_id = data.get("machineId")
            ip = data.get("addresseip")
            port = data.get("port")

            _ctx.process_user_photo(user_pin, gym_branch_id, machine_id, ip, port)

        except Exception as e:
            logging.error(f"❌ Error processing launch_publish_photo message: {e}")

    backoff = 1
    while not stop_event_kafka.is_set():
        try:
            publish_photo_kafka.consume(topic=TOPIC_CONSUME_PUBLISH_PHOTO, on_message=handle_photo_publish)
            backoff = 1
        except Exception as e:
            msg = str(e)
            logging.error("Error in publish photo consumption loop: %s", e)

            if "UNKNOWN_TOPIC_OR_PART" in msg or "UnknownTopicOrPartition" in msg:
                time.sleep(60)
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


# process_user_photo → moved to app_context.py


def start_kafka_consumers():
    pointage_thread = threading.Thread(target=consume_pointage_client, daemon=True, name="PointageClientThread")
    photo_publish_thread = threading.Thread(target=consume_publish_photo, daemon=True, name="PhotoPublishThread")
    fingerprint_actions_thread = threading.Thread(target=consume_fingerprint_client, daemon=True,
                                                  name="FingerprintActionsThread")

    pointage_thread.start()
    photo_publish_thread.start()
    fingerprint_actions_thread.start()
    print("Kafka consumer threads started")


def cleanup_resources(driver=None):
    """Clean up application resources."""
    print("Initiating cleanup...")
    stop_event_monitoring.set()
    stop_event_kafka.set()
    print("Application shutdown complete")

@app.route('/restart', methods=['POST'])
def soft_restart():
    logging.info("🔄 Soft restart demandé...")

    def _do_soft_restart():
        global watchdog
        pythoncom.CoInitialize()  # ← ajout

        # 1 — Arrêter les threads monitors
        stop_event_monitoring.set()
        time.sleep(3)

        # 2 — Remettre le stop_event à zéro
        stop_event_monitoring.clear()

        # 3 — Vider le DeviceManager
        if app_version == "v2":
            DeviceManagerV2.clear()
        else:
            DeviceManager._registry.clear()

        # 4 — Recharger les machines
        try:
            machines = machineService.get_access_machines(gym_branch_id, tenant)
            logging.info("🔌 Machines rechargées : %s", machines)
        except Exception as e:
            logging.error("❌ Erreur rechargement machines : %s", e)
            return

        if app_version == "v2":
            # v2 restart : réenregistrer toutes les machines en PUSH
            from services.v2.adms_server_v2 import ADMSServerV2
            adms_srv = ADMSServerV2()
            for m in machines:
                ctx = DeviceManagerV2.register(m, tenant, gym_branch_id)
                adms_srv.register_adapter(ctx.adapter)
            from services.v2.monitor_v2 import setup_attendance_callback
            setup_attendance_callback(adms_srv, tenant, gym_branch_id)
            logging.info("✅ Soft restart v2 terminé")
            return

        for m in machines:
            DeviceManager.register(m, tenant, gym_branch_id)

        # 5 — Nouveau watchdog
        watchdog = MachineWatchdog()

        for m in machines:
            ctx = DeviceManager.register(m, tenant, gym_branch_id)
            if m.type == "C3":
                def make_c3(ctx=ctx):
                    return threading.Thread(
                        target=monitor_machine,
                        args=(ctx, stop_event_monitoring),
                        daemon=True,
                        name=f"RT-C3-{ctx.machine.addresseip}"
                    )
                watchdog.register(m.id, make_c3)
            else:
                def make_zk(m=m):
                    return threading.Thread(
                        target=monitor_zkem,
                        args=(m, m.addresseip, m.port, 1,
                              stop_event_monitoring, tenant, gym_branch_id),
                        daemon=True,
                        name=f"RT-ZK-{m.addresseip}"
                    )
                watchdog.register(m.id, make_zk)

        watchdog.start()
        logging.info("✅ Soft restart terminé")

    threading.Thread(target=_do_soft_restart, daemon=True, name="SoftRestart").start()
    return jsonify({"status": "restarting"}), 200
# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    def handle_sigterm(signum, frame):
        logging.info("🚨 Reçu SIGTERM, fermeture propre en cours...")
        cleanup_resources()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    start_ws_server()
    driver = None
    machineService = AccessMachineService()

    if len(sys.argv) >= 3:
        tenant = sys.argv[1]
        gym_branch_id = sys.argv[2]
    else:
        print("Expected arguments: TENANT GYM_BRANCH_ID [--version v1|v2]", file=sys.stderr)
        sys.exit(1)

    # Parse --version argument
    if "--version" in sys.argv:
        idx = sys.argv.index("--version")
        if idx + 1 < len(sys.argv):
            app_version = sys.argv[idx + 1].lower()
        else:
            print("--version requires a value (v1 or v2)", file=sys.stderr)
            sys.exit(1)
    else:
        app_version = "v1"

    if app_version not in ("v1", "v2"):
        print(f"Version invalide: {app_version}. Attendu: v1 ou v2", file=sys.stderr)
        sys.exit(1)

    print(f"current tenant  : {tenant}    gymbranchid : {gym_branch_id}    version : {app_version}")
    currentGymBranchId = gym_branch_id

    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s — %(levelname)s — %(message)s")

        machines = machineService.get_access_machines(gym_branch_id, tenant)
        logging.info("machines dispo   :", machines)

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
            for m in machines:
                DeviceManager.register(m, tenant, gym_branch_id)

            initialize_task_queue_db()
            threading.Thread(target=process_device_queue,
                             daemon=True,
                             name="DeviceQueueThread").start()

            global watchdog
            watchdog = MachineWatchdog()
            adms_server = None
            has_push = any(m.type == "PUSH" for m in machines)
            if has_push:
                adms_server = ADMSServer(port=8088)
                adms_server.start()
                logging.info("🚀 Serveur ADMS démarré (port 8088)")
            for m in machines:
                ctx = DeviceManager.register(m, tenant, gym_branch_id)

                if m.type == "C3":
                    def make_c3(ctx=ctx):
                        return threading.Thread(
                            target=monitor_machine,
                            args=(ctx, stop_event_monitoring),
                            daemon=True,
                            name=f"RT-C3-{ctx.machine.addresseip}"
                        )

                    watchdog.register(m.id, make_c3)

                elif m.type == "PUSH":
                    adapter = ctx.adapter
                    if adms_server:
                        adms_server.register_adapter(adapter)

                    def make_push(m=m, adapter=adapter):
                        return threading.Thread(
                            target=monitor_adms,
                            args=(m, adapter, stop_event_monitoring, tenant, gym_branch_id),
                            daemon=True,
                            name=f"RT-PUSH-{m.addresseip}"
                        )

                    watchdog.register(m.id, make_push)

                else:  # STANDALONE_NEW_FIRMWARE
                    def make_zk(m=m):
                        return threading.Thread(
                            target=monitor_zkem,
                            args=(m, m.addresseip, m.port, 1,
                                  stop_event_monitoring, tenant, gym_branch_id),
                            daemon=True,
                            name=f"RT-ZK-{m.addresseip}"
                        )

                    watchdog.register(m.id, make_zk)

            watchdog.start()
        # ───────────────────────────────────────────────────────────────

        start_kafka_consumers()

        free_port(FLASK_PORT)
        threading.Thread(target=lambda: app.run(
            debug=False, host=FLASK_HOST, port=FLASK_PORT),
                         daemon=True, name="FlaskThread").start()

        while True:
            time.sleep(0.01)

    except KeyboardInterrupt:
        logging.info("⏹️ Arrêt demandé par l'utilisateur")
        cleanup_resources(driver)
    except Exception as ex:
        logging.exception("Erreur fatale : %s", ex)
        cleanup_resources(driver)
        raise
