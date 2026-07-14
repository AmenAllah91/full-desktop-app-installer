import base64
import os
import signal
import socket
import sys
import threading
from datetime import datetime

import psutil
import logging
import time
import json
import sqlite3

from domain import Operation
from kafka_service.kafkaservice import KafkaService
from services.DeviceMAnager import DeviceManager
from services.MonitorZkem import monitor_zkem
from services.adapters import PlcommAdapter
from services.captureFingerPrint import FingerprintCapture
from services.machinesService import AccessMachineService
from services.MachineMonitor import monitor_machine, make_rt_json, kafka, POINTAGE_TOPIC

from flask import Flask, jsonify
from flask_cors import CORS
from queue import Queue
from dotenv import load_dotenv, set_key

from services.websocket import start_ws_server, send_pointage
from services.zkem_adapter import ZkemAdapter
from services import machine_status


def get_app_data_dir():
    print("[INFO] Trying to get APPDATA environment variable...")
    app_data = os.environ.get('APPDATA')

    if not app_data:
        print("[WARN] APPDATA not found. Falling back to default path...")
        app_data = os.path.expanduser('~\\AppData\\Roaming')
    else:
        print(f"[INFO] APPDATA found: {app_data}")

    app_dir = os.path.join(app_data, 'desktop-app')  # Replace 'desktop-app' with your actual app name
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

# Create the .env with defaults BEFORE loading it, otherwise the first run
# starts with no variables at all (GYM_BRANCH_ID=None -> crash).
initialize_env_file()
load_dotenv(dotenv_path=ENV_FILE_PATH)

KafkaBroker = os.getenv("KAFKA_BROKER")

# Flask application initialization
app = Flask(__name__)
CORS(app)
currentGymBranchId = int(os.getenv("GYM_BRANCH_ID", "0") or 0)
tenant =os.getenv("TENANT")
# Kafka service configuration
# el kafka service service bech nal9aw fiha el connection m3a el server eli fyha kafka "broker" w nal9aw methods kima el produce w el consume
# NOTE: ces instances sont recréées dans __main__ une fois le vrai gym_branch_id
# connu (argv). Le group id doit être unique par branche ET par poste, sinon les
# PC de branches différentes se partagent les messages d'un même groupe Kafka et
# des demandes d'accès sont consommées puis jetées par la mauvaise branche.
pointage_kafka = KafkaService(kafka_broker=KafkaBroker, group_id=f"pointage_group{currentGymBranchId}")
publish_photo_kafka = KafkaService(kafka_broker=KafkaBroker, group_id=f"photo_publish_group{currentGymBranchId}")
fingerprint_kafka = KafkaService(kafka_broker=KafkaBroker, group_id=f"fingerprint_group{currentGymBranchId}")

machineService = AccessMachineService()

# Global variables and synchronization primitives
# device handel 7tinaha global bech nconictiw mara barka m3a el machine
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
    return task  # Returns (id, task_data) or None


def mark_task_as_completed(task_id):
    """Mark a task as COMPLETED in the SQLite queue and delete all completed tasks."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Mark the specified task as COMPLETED
    cursor.execute("UPDATE task_queue SET status = 'COMPLETED' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    # Call delete_completed_tasks to clean up completed tasks
    delete_completedTasks()


def delete_completedTasks():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_queue WHERE status = 'COMPLETED'")
    conn.commit()
    conn.close()


def free_port(port, retries=3):
    """Find and kill any process using the specified port."""
    for _ in range(retries):
        found_process = False  # Track if any process was found using the port

        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port:
                found_process = True
                try:
                    proc = psutil.Process(conn.pid)
                    if proc.is_running():
                        # Attempt graceful termination
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
            # Delay and retry in case of transient errors or reallocation of the port
            time.sleep(1)
    print("Retries exhausted. Unable to clear the port completely.")




def process_device_queue() -> None:
    POLL_SLEEP = 0.5  # s – pause si queue vide
    MAX_RETRIES = 3
    RETRY_SLEEP = 1  # s – pause entre deux essais
    WAIT_HANDLE = 1  # s – délai max pour qu’un C3 ouvre le handle

    while not stop_event_monitoring.is_set():
        row = get_next_task_from_queue()
        if not row:
            time.sleep(POLL_SLEEP)
            continue

        task_id, raw = row
        try:
            task = json.loads(raw)
            ctx = DeviceManager.get(task["machineId"])
            if ctx is None:
                logging.warning("⏭️ Tâche #%s : machine %s non enregistrée sur cette instance, ignorée",
                                task_id, task["machineId"])
                mark_task_as_completed(task_id)
                continue
            op = task["operation"]
            pin = task["user_pin"]
        except Exception as exc:
            logging.error("Tâche #%s invalide : %s", task_id, exc)
            mark_task_as_completed(task_id)
            continue

        adapter = ctx.adapter

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # ---------- 1. Disponibilité de la connexion ----------
                if isinstance(adapter, PlcommAdapter):
                    waited = 0.0
                    while ctx.handle is None and waited < WAIT_HANDLE:
                        time.sleep(0.5);
                        waited += 0.5

                    if ctx.handle is None:
                        raise RuntimeError("Pas de handle C3 disponible")

                    # à ce stade adapter.handle pointe déjà vers ctx.handle
                else:
                    adapter.connect()

                    # ---------- 2. Section critique protégée -------------
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

                # ---------- 3. Résultat ------------------------------
                if ok:
                    logging.info("✅ Tâche #%s terminée (%s / %s)",
                                 task_id, ctx.machine.alias, op)
                    mark_task_as_completed(task_id)
                    break  # succès
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
                        "user_name": json_message.get("username") or "",
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
    required_keys = ["pin", "operation", "fingerprint_template",
                     "finger_id"]
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
        payload = payload = {
            "gymbranchId": int(currentGymBranchId),
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

    while not stop_event_kafka.is_set():
        try:
            publish_photo_kafka.consume(topic=TOPIC_CONSUME_PUBLISH_PHOTO, on_message=handle_photo_publish)
        except Exception as e:
            logging.error("Error in publish photo consumption loop: %s", e)
            time.sleep(3)


def process_user_photo(user_pin: str, gym_branch_id: str, machine_id: int, ip: str = None, port: int = None):
    # Comparaison en str : gym_branch_id arrive tantôt en int (JSON), tantôt en
    # str, et currentGymBranchId est une str (argv) au runtime.
    if str(gym_branch_id) != str(currentGymBranchId):
        return None

    ctx = DeviceManager._registry.get(machine_id)
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


def ensure_all_kafka_topics():
    """Proactively create all required Kafka topics at startup."""
    topics = [
        'new_access_request_' + tenant,
        TOPIC_CONSUME_PUBLISH_PHOTO,
        TOPIC_PRODUCE_PUBLISH_PHOTO,
        'fingerprint_actions_' + tenant,
        POINTAGE_TOPIC,
    ]
    for topic in topics:
        pointage_kafka.ensure_topic(topic)
        time.sleep(0.1)


# Start separate Kafka consumer threads
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


# imports (en haut de ton main)
from werkzeug.utils import secure_filename
from pathlib import Path

UPLOAD_DIR = Path(TEMP_DIR) / "faces"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.route('/face/upload', methods=['POST'])
def upload_face_multipart():
    pin = request.form.get('pin')
    gym_branchId = request.form.get('gymBranchId')
    file = request.files.get('photo')

    # ----- validations basiques -----
    if not all([pin, gym_branchId, file]):
        abort(400, "pin, gymBranchId et fichier photo sont requis")

    if str(gym_branchId) != str(currentGymBranchId):
        abort(400, "gymBranchId ne correspond pas à cette instance")

    # ----- nom conforme attendu par la machine -----
    dst_name = f"verify_biophoto_9_{pin}.jpg"
    dst_path = UPLOAD_DIR / secure_filename(dst_name)
    file.save(dst_path)
    logging.info("📥 Photo enregistrée : %s", dst_path)

    # ----- envoi à chaque machine de la branche -----
    report = {}
    MAX_UPLOAD_RETRIES = 3
    RETRY_DELAY = 1  # secondes

    for ctx in DeviceManager._registry.values():
        if str(ctx.gymBranchId) != str(currentGymBranchId):
            continue

        adapter = ctx.adapter
        ok = False

        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                ok = adapter.upload_user_photo(pin, str(dst_path))
                if ok:
                    break  # succès
                else:
                    logging.warning(f"⏱️ Tentative {attempt}/{MAX_UPLOAD_RETRIES} échouée pour {ctx.machine.addresseip}")
            except Exception as ex:
                logging.exception(f"❌ Exception lors de l’upload vers {ctx.machine.addresseip} (essai {attempt}) : {ex}")

            time.sleep(RETRY_DELAY)

        report[ctx.machine.addresseip] = "OK" if ok else "KO"

    return jsonify({"pin": pin, "result": report}), 200



@app.route('/fingerprint/upload', methods=['POST'])
def upload_fingerprint():
    """
    Upload fingerprint to ALL adapters/machines for the current gym branch
    """
    try:
        data = request.get_json()
        if not data or "pin" not in data:
            return jsonify({"error": "Missing 'pin' in request"}), 400

        pin = data["pin"]
        gym_branch_id = data.get("gymBranchId", currentGymBranchId)
        finger_id = data.get("fingerId")
        # Validate gym branch
        if str(gym_branch_id) != str(currentGymBranchId):
            return jsonify({"error": "gymBranchId mismatch"}), 400
        logging.info(f" Starting fingerprint upload for PIN {pin}")

        # Capture fingerprint once
        capture = FingerprintCapture()
        template_bytes, images = capture.capture_fingerprint(save_file=False)

        if not template_bytes:
            return jsonify({"error": "Failed to capture fingerprint"}), 500

        logging.info(f" Fingerprint captured successfully. Template size: {len(template_bytes)} bytes")

        # Upload to ALL adapters in the current gym branch
        results = {}
        success_count = 0
        total_count = 0

        for machine_id, ctx in DeviceManager._registry.items():
            # Skip machines not belonging to current gym branch
            if str(ctx.gymBranchId) != str(currentGymBranchId):
                continue

            total_count += 1
            adapter = ctx.adapter
            machine_ip = ctx.machine.addresseip

            try:
                logging.info(f" Uploading to machine {machine_ip} (ID: {machine_id})")
                with ctx.lock:
                    success = adapter.add_fingerprint(
                        user_id=pin,
                        fingerprint_template=template_bytes,
                        finger_id=finger_id
                    )

                if success:
                    results[machine_ip] = "SUCCESS"
                    success_count += 1
                    logging.info(f" Fingerprint uploaded successfully to {machine_ip}")
                else:
                    results[machine_ip] = "FAILED"
                    logging.error(f" Failed to upload fingerprint to {machine_ip}")
            except Exception as e:
                results[machine_ip] = f"ERROR: {str(e)}"
                logging.error(f" Exception uploading to {machine_ip}: {e}")

        python_bytes = bytes(bytearray(template_bytes))
        encoded_template = base64.b64encode(python_bytes).decode('utf-8')
        payload = {"fingerprint_template": encoded_template, "pin": pin, "gymBranchId": gym_branch_id,
                   "operation": Operation.ADD_FINGERPRINT.value, "finger_id": finger_id}
        # Prepare response
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
    return {"gymBranchId": int(currentGymBranchId)}


# -------------------------------------------------------------
#  ROUTE :  POST /tasks/access
# -------------------------------------------------------------
from flask import request, abort

REQUIRED_TOP = {
    "gymBranchId", "operation",
    "userPin", "cardNo",
    "startDate", "endDate",
    "machines"
}
REQUIRED_MACHINE = {"id", "addresseip", "port", "type"}


@app.after_request
def allow_private_network(response):
    # Chrome "Private Network Access" : un front servi en HTTPS public doit
    # recevoir ce header sur le preflight pour pouvoir appeler ce service local.
    if request.method == 'OPTIONS':
        response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response


@app.route('/tasks/access', methods=['POST'])
def enqueue_access_tasks():
    try:
        data = request.get_json(force=True)
    except Exception:
        abort(400, "Payload JSON invalide")

    # 1) Validation de base
    if not REQUIRED_TOP.issubset(data):
        missing = REQUIRED_TOP - data.keys()
        abort(400, f"Champs manquants : {', '.join(missing)}")

    if str(data["gymBranchId"]) != str(currentGymBranchId):
        abort(400, "gymBranchId ne correspond pas à la configuration locale")

    if not isinstance(data["machines"], list) or not data["machines"]:
        abort(400, "machines doit être une liste non vide")

    # 2) Boucle sur les machines
    queued = 0
    for m in data["machines"]:
        if not REQUIRED_MACHINE.issubset(m):
            abort(400, "Chaque machine doit contenir id, addresseip, port, type")

        task = {
            "machineId": m["id"],
            "ip_address": m["addresseip"],
            "port": str(m["port"]),
            "user_pin": data["userPin"],
            "user_name": data.get("username") or "",
            "operation": data["operation"],
            "card_no": data["cardNo"],
            "start_date": data["startDate"],
            "end_date": data["endDate"]
        }
        add_task_to_queue(task)
        queued += 1

    return jsonify({"status": "queued", "tasksQueued": queued}), 201

@app.route('/api/machines/status', methods=['GET', 'OPTIONS'])
def api_machines_status():
    statuses = []
    for ctx in DeviceManager.all():
        adapter = ctx.adapter
        connected = False
        try:
            connected = adapter.is_connected() if hasattr(adapter, 'is_connected') else adapter.handle is not None
        except Exception:
            pass
        statuses.append({
            "id": ctx.machine.id,
            "alias": ctx.machine.alias,
            "ip": ctx.machine.addresseip,
            "port": ctx.machine.port,
            "type": ctx.machine.type,
            "connected": connected,
        })
    return jsonify(statuses), 200


@app.route('/api/machines/<int:machine_id>/reconnect', methods=['POST'])
def api_machine_reconnect(machine_id):
    """Reconnexion manuelle demandée depuis le front (bouton de la sidebar)."""
    ctx = DeviceManager.get(machine_id)
    if not ctx:
        abort(404, f"Machine {machine_id} inconnue sur cette instance")

    def _do_reconnect():
        try:
            if isinstance(ctx.adapter, PlcommAdapter):
                from services.MachineMonitor import attempt_c3_reconnection
                attempt_c3_reconnection(ctx)
            else:
                import pythoncom
                pythoncom.CoInitialize()
                try:
                    with ctx.lock:
                        try:
                            ctx.adapter.disconnect()
                        except Exception:
                            pass
                        ok = ctx.adapter.connect()
                    if ok:
                        machine_status.mark_connected(ctx.machine, "reconnected")
                    else:
                        machine_status.mark_disconnected(ctx.machine, "Reconnexion manuelle échouée", "error")
                finally:
                    pythoncom.CoUninitialize()
        except Exception as ex:
            logging.exception("Erreur reconnexion manuelle machine %s : %s", machine_id, ex)
            machine_status.mark_disconnected(ctx.machine, str(ex), "error")

    threading.Thread(target=_do_reconnect, daemon=True,
                     name=f"Reconnect-{machine_id}").start()
    return jsonify({"status": "requested", "machineId": machine_id}), 202


REQUIRED_OPEN = {"gymBranchId", "machineId"}

@app.route('/door/open', methods=['POST'])
def open_door_api():
    try:
        data = request.get_json(force=True) or {}

        # --- champs obligatoires ---
        if not REQUIRED_OPEN.issubset(data):
            missing = REQUIRED_OPEN - data.keys()
            abort(400, f"Champs manquants : {', '.join(missing)}")

        gym_branch_id = str(data["gymBranchId"])
        machine_id = data["machineId"]

        # --- champs optionnels / défauts ---
        duration = int(data.get("duration", 5))
        door_no = int(data.get("door", 1))   # pour C3 : 1..4

        # 👇 nouveau : pin & porte_type optionnels
        pin = data.get("pin")                # string ou int
        porte_type = data.get("porte_type", "ENTREE")

        ctx = DeviceManager.get(machine_id)
        if not ctx:
            abort(404, f"Machine {machine_id} inconnue dans DeviceManager")

        adapter = ctx.adapter

        with ctx.lock:
            if isinstance(adapter, PlcommAdapter):
                ok = adapter.open_door(door_no=door_no, duration=duration)
            elif isinstance(adapter, ZkemAdapter):
                ok = adapter.open_door(duration_seconds=duration)
            else:
                abort(400, f"Type d'adapter non supporté : {type(adapter).__name__}")

        # --- SI pas de PIN → on s’arrête là ---
        if not pin:
            return jsonify({
                "status": "OK" if ok else "ERROR",
                "machineId": machine_id,
                "duration": duration
            }), 200 if ok else 500

        # --- SINON : on crée un pointage comme si l’adhérent avait pointé ---
        try:
            now = datetime.now()
            dt_str = now.strftime("%Y-%m-%d %H:%M:%S")

            # code event "ouverture distante" (choisis ce que tu veux)
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
            kafka.produce(POINTAGE_TOPIC, payload)

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
# ---------------------------------------------------------------------------
# 2) main  — initialisation complète de l’application
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    def handle_sigterm(signum, frame):
        logging.info("🚨 Reçu SIGTERM, fermeture propre en cours...")
        cleanup_resources()
        sys.exit(0)


    signal.signal(signal.SIGTERM, handle_sigterm)
    start_ws_server()
    driver = None  # ← pour le finally
    machineService = AccessMachineService()

    if len(sys.argv) >= 3:
        tenant = sys.argv[1]
        gym_branch_id = sys.argv[2]
    else:
        print("Expected arguments: TENANT GYM_BRANCH_ID", file=sys.stderr)
        sys.exit(1)

    print(f"current tenant  : {tenant}    gymbranchid : {gym_branch_id}")
    currentGymBranchId = gym_branch_id

    # Recréation des services Kafka avec le VRAI gym_branch_id (argv) et un
    # group id unique par poste : sémantique broadcast — chaque PC de la branche
    # reçoit tous les messages et ne traite que ses propres machines.
    # auto_offset_reset='latest' évite de rejouer tout l'historique du topic
    # lors du premier démarrage avec un nouveau group id.
    _hostname = socket.gethostname()
    pointage_kafka = KafkaService(
        kafka_broker=KafkaBroker,
        group_id=f"pointage_group_{gym_branch_id}_{_hostname}",
        auto_offset_reset='latest')
    publish_photo_kafka = KafkaService(
        kafka_broker=KafkaBroker,
        group_id=f"photo_publish_group_{gym_branch_id}_{_hostname}",
        auto_offset_reset='latest')
    fingerprint_kafka = KafkaService(
        kafka_broker=KafkaBroker,
        group_id=f"fingerprint_group_{gym_branch_id}_{_hostname}",
        auto_offset_reset='latest')

    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s — %(levelname)s — %(message)s")

        machines = machineService.get_access_machines(gym_branch_id, tenant)
        print("machines dispo   :", machines)
        for m in machines:
            DeviceManager.register(m, tenant, gym_branch_id)
            # État initial "déconnecté" : la sidebar du front reçoit le snapshot
            # dès sa connexion websocket, avant même le premier événement.
            machine_status.register_machine(m)

        # SQLite et thread DeviceQueue (pas changé)
        initialize_task_queue_db()
        threading.Thread(target=process_device_queue,
                         daemon=True,
                         name="DeviceQueueThread").start()

        for m in machines:
            ctx = DeviceManager.register(m, tenant, gym_branch_id)
            if m.type == "C3":
                threading.Thread(target=monitor_machine,
                                 args=(ctx, stop_event_monitoring),
                                 daemon=True,
                                 name=f"RT-C3-{m.addresseip}").start()
            else:
                threading.Thread(target=monitor_zkem,
                                 args=(m, m.addresseip, m.port, 1,
                                       stop_event_monitoring, tenant, gym_branch_id),
                                 daemon=True,
                                 name=f"RT-ZK-{m.addresseip}").start()

        # Ensure all Kafka topics exist before starting consumers
        ensure_all_kafka_topics()

        # Kafka consumers
        start_kafka_consumers()

        # Flask (inchangé)
        free_port(FLASK_PORT)
        threading.Thread(target=lambda: app.run(
            debug=False, host=FLASK_HOST, port=FLASK_PORT),
                         daemon=True, name="FlaskThread").start()

        while True:
            time.sleep(0.01)

    except KeyboardInterrupt:
        logging.info("⏹️ Arrêt demandé par l’utilisateur")
        cleanup_resources(driver)
    except Exception as ex:
        logging.exception("Erreur fatale : %s", ex)
        cleanup_resources(driver)
        raise
