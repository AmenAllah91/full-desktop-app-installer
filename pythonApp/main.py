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
import subprocess
import socket
from http.server import HTTPServer, SimpleHTTPRequestHandler
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from pathlib import Path
import pythoncom

from domain import Operation
from services.DeviceMAnager import DeviceManager
from services.MonitorZkem import monitor_zkem
from services.adapters import PlcommAdapter, MachineType
from services.captureFingerPrint import FingerprintCapture
from services.machinesService import AccessMachineService
from services.MachineMonitor import monitor_machine, make_rt_json,attempt_c3_reconnection
from services.http_client import send_pointage as send_pointage_http

from flask import Flask, jsonify
from flask_cors import CORS
from queue import Queue
from dotenv import load_dotenv, set_key

from services.websocket import start_ws_server, send_pointage, start_machine_status_broadcast
from services.zkem_adapter import ZkemAdapter
from services.adms_adapter import ADMSAdapter
from services.adms_server import ADMSServer
from services.MonitorADMS import monitor_adms

class SPARequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/" and not os.path.exists(self.translate_path(self.path)):
            self.path = "/index.html"
        return super().do_GET()

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
GYM_BRANCH_ID=1
TENANT=empire

FLASK_HOST=0.0.0.0
FLASK_PORT=9998

PLCOMPRO_URL=plcommpro.dll

SPRING_BOOT_URL=http://localhost:8081
DESKTOP_AGENT_URL=http://localhost:9998
"""
        with open(ENV_FILE_PATH, 'w') as f:
            f.write(default_env_content)
        print(f"Created default .env file at: {ENV_FILE_PATH}")


APP_DATA_DIR = get_app_data_dir()
ENV_FILE_PATH = os.path.join(APP_DATA_DIR, '.env')
DB_FILE = os.path.join(APP_DATA_DIR, 'task_queue.db')
TEMP_DIR = os.path.join(APP_DATA_DIR, 'temp')

os.makedirs(TEMP_DIR, exist_ok=True)

load_dotenv(dotenv_path=ENV_FILE_PATH)
initialize_env_file()

currentGymBranchId = int(os.getenv("GYM_BRANCH_ID"))
tenant =os.getenv("TENANT")

machineService = AccessMachineService()

device_handle = None
handle_lock = threading.RLock()
stop_event_monitoring = threading.Event()

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

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "http://localhost:4200"}}, supports_credentials=True)
@app.after_request
def add_private_network_headers(response):
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

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
    CHECK_INTERVAL = 10
    ZOMBIE_TIMEOUT = 300

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

    def _get_adapter(self, machine_id: int):
        ctx = DeviceManager.get(machine_id)
        if ctx:
            return ctx.adapter
        try:
            from services.v2.device_manager_v2 import DeviceManagerV2
            ctx2 = DeviceManagerV2.get(machine_id)
            if ctx2:
                return ctx2.adapter
        except Exception:
            pass
        return None

    def _watch_loop(self) -> None:
        while not stop_event_monitoring.is_set():
            time.sleep(self.CHECK_INTERVAL)

            for mid, entry in self._entries.items():
                if stop_event_monitoring.is_set():
                    break

                thread = entry["thread"]
                thread_alive = thread.is_alive()
                is_zombie = False

                if thread_alive:
                    adapter = self._get_adapter(mid)
                    if adapter is not None and adapter.last_seen is not None:
                        elapsed = time.time() - adapter.last_seen
                        if elapsed >= self.ZOMBIE_TIMEOUT:
                            is_zombie = True
                            logging.error(
                                "🧟 [Watchdog] Thread ZOMBIE — machine %s, "
                                "dernier événement il y a %.0fs (seuil=%ss)",
                                mid, elapsed, self.ZOMBIE_TIMEOUT
                            )

                if not thread_alive or is_zombie:
                    delay = entry["delay"]

                    if not thread_alive:
                        logging.error(
                            "💀 [Watchdog] Thread mort — machine %s, "
                            "redémarrage dans %ss", mid, delay
                        )
                    else:
                        logging.error(
                            "🧟 [Watchdog] Redémarrage thread zombie — "
                            "machine %s dans %ss", mid, delay
                        )

                    elapsed = 0
                    while elapsed < delay and not stop_event_monitoring.is_set():
                        time.sleep(1)
                        elapsed += 1

                    if stop_event_monitoring.is_set():
                        break

                    if thread_alive and is_zombie:
                        logging.info(
                            "🪦 [Watchdog] Attente arrêt ancien thread zombie %s...", mid
                        )

                    new_thread = entry["factory"]()
                    new_thread.start()
                    entry["thread"] = new_thread
                    entry["delay"] = min(delay * 2, self.MAX_DELAY)
                    logging.info(
                        "♻️ [Watchdog] Thread redémarré — machine %s (%s)",
                        mid, new_thread.name
                    )
                else:
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


def resource_path(relative_path: str, subfolder: str = None) -> str:
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    if subfolder:
        base_dir = os.path.join(base_dir, subfolder)

    return os.path.join(base_dir, relative_path)


def check_file(path: str, description: str) -> bool:
    if os.path.exists(path):
        return True
    else:
        print(f"{description} introuvable : {path}")
        return False

def start_http_server(directory: str, port: int) -> bool:
    if not os.path.isdir(directory):
        return False

    os.chdir(directory)
    httpd = HTTPServer(("127.0.0.1", port), SPARequestHandler)

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Angular frontend démarré sur http://localhost:{port}")
    return True
def start_angular():
    angular_dir = resource_path("", subfolder="angular-dist/yo-gym")
    start_http_server(angular_dir, 4200)
    if not wait_for_angular(host="127.0.0.1", port=4200, timeout=60):
        print("Angular n'a pas démarré correctement, vérifiez les logs")
    else:
        print("Angular frontend started")

def encrypt_key(key: str, password: str) -> str:
    iv = get_random_bytes(12)
    cipher = AES.new(password.encode("utf-8"), AES.MODE_GCM, nonce=iv)
    ciphertext, tag = cipher.encrypt_and_digest(key.encode("utf-8"))
    encrypted_blob = iv + tag + ciphertext
    return base64.b64encode(encrypted_blob).decode("utf-8")

def start_spring():
    spring_jar = resource_path("gym-management-app-0.0.1-SNAPSHOT.jar", subfolder="spring-boot")
    if check_file(spring_jar, "Spring Boot JAR"):

        h2_key = "eV03^a&T2W2E9r4xG0a^L&Yfc"
        encryption_password = "yNFDmMUvrR9TypQ9kTqHOXwKFVROAKpj"

        encrypted_key = encrypt_key(h2_key, encryption_password)

        subprocess.Popen(
            [
                "java",
                f"-DGYM_KEY={encrypted_key}",
                f"-DGYM_KEY_PASS={encryption_password}",
                "-jar",
                spring_jar,
                "--spring.profiles.active=desktop"
            ],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        os.environ.pop("GYM_KEY", None)
        os.environ.pop("GYM_KEY_PASS", None)
        encrypted_key = None
        encryption_password = None

    if not wait_for_spring_boot(host="127.0.0.1", port=8081, timeout=300):
        print("Spring Boot n'a pas démarré correctement, vérifiez les logs")
    else:
        print("Spring Boot backend started")

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


def get_device_context(machine_id):
    return DeviceManager.get(machine_id)


def get_all_device_contexts():
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


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)




@app.route('/getFace/<int:user_pin>/<int:gym_branch_id>/<int:machine_id>', methods=['GET'])
def capture_fingerprint_api(user_pin, gym_branch_id, machine_id):
    try:
        payload = process_user_photo(str(user_pin), str(gym_branch_id), machine_id)
        if payload:
            return jsonify(payload), 200
        else:
            return jsonify({"error": "Photo not found or machine not valid"}), 400
    except Exception as e:
        logging.error(f" Error in capture_fingerprint_api: {e}")
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


def process_user_photo(user_pin: str, gym_branch_id: str, machine_id: int, ip: str = None, port: int = None):
    if gym_branch_id != currentGymBranchId:
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
            logging.info(f"✅ Published photo for user {user_pin}.")
            return payload
        else:
            logging.error(f"❌ Failed to download photo for user {user_pin}.")
            return None



def cleanup_resources(driver=None):
    """Clean up application resources."""
    print("Initiating cleanup...")
    stop_event_monitoring.set()
    print("Application shutdown complete")


from werkzeug.utils import secure_filename
from pathlib import Path

UPLOAD_DIR = Path(TEMP_DIR) / "faces"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.route('/face/upload', methods=['POST'])
def upload_face_multipart():
    pin = request.form.get('pin')
    gym_branchId = request.form.get('gymBranchId')
    file = request.files.get('photo')

    if not all([pin, gym_branchId, file]):
        abort(400, "pin, gymBranchId et fichier photo sont requis")

    if str(gym_branchId) != str(currentGymBranchId):
        abort(400, "gymBranchId ne correspond pas à cette instance")

    dst_name = f"verify_biophoto_9_{pin}.jpg"
    dst_path = UPLOAD_DIR / secure_filename(dst_name)
    file.save(dst_path)
    logging.info("📥 Photo enregistrée : %s", dst_path)

    report = {}
    MAX_UPLOAD_RETRIES = 3
    RETRY_DELAY = 1

    for ctx in get_all_device_contexts():
        if str(ctx.gymBranchId) != str(currentGymBranchId):
            continue

        adapter = ctx.adapter
        ok = False

        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                ok = adapter.upload_user_photo(pin, str(dst_path))
                if ok:
                    break
                else:
                    logging.warning(f"⏱️ Tentative {attempt}/{MAX_UPLOAD_RETRIES} échouée pour {ctx.machine.addresseip}")
            except Exception as ex:
                logging.exception(f"❌ Exception lors de l’upload vers {ctx.machine.addresseip} (essai {attempt}) : {ex}")

            time.sleep(RETRY_DELAY)

        report[ctx.machine.addresseip] = "OK" if ok else "KO"

    return jsonify({"pin": pin, "result": report}), 200


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
            return jsonify({"error": "Failed to capture fingerprint"}), 500

        logging.info(f" Fingerprint captured successfully. Template size: {len(template_bytes)} bytes")

        results = {}
        success_count = 0
        total_count = 0

        for ctx in get_all_device_contexts():
            machine_id = ctx.machine.id
            branch_id = ctx.gymBranchId
            if str(branch_id) != str(currentGymBranchId):
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

        if success_count == 0:
            return jsonify({
                "error": f"Failed to upload fingerprint to all {total_count} machines",
                "results": results
            }), 500
        elif success_count < total_count:
            return jsonify({
                "warning": f"Partial success: {success_count}/{total_count} machines",
                "results": results,
                "pin": pin,
                "fingerprint_template": encoded_template
            }), 207
        else:
            return jsonify({
                "message": f"Fingerprint uploaded successfully to all {total_count} machines",
                "results": results,
                "pin": pin,
                "fingerprint_template": encoded_template
            }), 200

    except Exception as e:
        logging.exception(f" Fatal error in fingerprint upload: {e}")
        return jsonify({"error": f"Exception during fingerprint upload: {str(e)}"}), 500


@app.route('/fingerprint/actions', methods=['POST'])
def fingerprint_actions():
    try:
        data = request.get_json(force=True)
    except Exception:
        abort(400, "Payload JSON invalide")

    if not data or "pin" not in data or "operation" not in data:
        abort(400, "pin et operation requis")

    machines = machineService.get_access_machines(str(currentGymBranchId), tenant)
    for machine in machines:
        add_task_to_queue({
            "machineId": machine.id,
            "ip_address": machine.addresseip,
            "port": str(machine.port),
            "user_pin": str(data["pin"]),
            "operation": data["operation"],
            "finger_id": data.get("finger_id", 0),
            "fingerprint_template": data.get("fingerprint_template", ""),
        })
    return jsonify({"status": "queued", "tasksQueued": len(machines)}), 201


@app.route('/config/gymBranchId', methods=['GET'])
def get_gym_branch_id():
    return {"gymBranchId": currentGymBranchId}


from flask import request, abort


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

        if hasattr(adapter, 'open_door'):
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
            "mode": ctx.machine.type,
        }
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
            with ctx.lock:
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
            if isinstance(adapter, PlcommAdapter):
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
            )

            payload = json.loads(payload_json)
            send_pointage(payload, gym_branch_id)
            send_pointage_http(payload, ctx.machine.id)

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
        DeviceManager._registry.clear()

        # 4 — Recharger les machines
        try:
            machines = machineService.get_access_machines(gym_branch_id, tenant)
            logging.info("🔌 Machines rechargées : %s", machines)
        except Exception as e:
            logging.error("❌ Erreur rechargement machines : %s", e)
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
def wait_for_spring_boot(host="127.0.0.1", port=8081, timeout=120):
    """Attend que Spring Boot écoute sur le port donné, timeout en secondes."""
    start_time = time.time()
    while True:
        try:
            with socket.create_connection((host, port), timeout=1):
                print(f"Spring Boot prêt sur {host}:{port}")
                return True
        except OSError:
            if time.time() - start_time > timeout:
                print(f"Timeout : Spring Boot non disponible sur {host}:{port} après {timeout}s")
                return False
            time.sleep(1)

def wait_for_angular(host="127.0.0.1", port=4200, timeout=120):
    """Attend que Angular écoute sur le port donné, timeout en secondes."""
    start_time = time.time()
    while True:
        try:
            with socket.create_connection((host, port), timeout=1):
                print(f"Angular prêt sur {host}:{port}")
                return True
        except OSError:
            if time.time() - start_time > timeout:
                print(f"Timeout : Angular non disponible sur {host}:{port} après {timeout}s")
                return False
            time.sleep(1)
if __name__ == '__main__':
 # TODO: remove comments
    # spring_thread = threading.Thread(target=start_spring)
    # spring_thread.start()
    # angular_thread = threading.Thread(target=start_angular)
    # angular_thread.start()
    # angular_thread.join()
    # spring_thread.join()
    # threading.Thread(target=angular_watchdog).start()

    def handle_sigterm(signum, frame):
        logging.info("Reçu SIGTERM, fermeture propre en cours...")
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
        tenant = "empire"
        gym_branch_id = "1"
        print(f"Using defaults: tenant={tenant}, gym_branch_id={gym_branch_id}")

    if "--version" in sys.argv:
        idx = sys.argv.index("--version")
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1].lower() == "v2":
            print("v2 n'est plus supporté, utilisation de v1", file=sys.stderr)

    print(f"current tenant  : {tenant}    gymbranchid : {gym_branch_id}    version : v1")
    currentGymBranchId = gym_branch_id

    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s — %(levelname)s — %(message)s")

        machines = machineService.get_access_machines(gym_branch_id, tenant)
        print("machines dispo   :", machines)

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


        def _get_all_devices_with_version():
            return [
                (ctx.machine, ctx.adapter, app_version)
                for ctx in get_all_device_contexts()
            ]
        start_machine_status_broadcast(_get_all_devices_with_version, interval=5)

        def _get_all_devices_with_version():
            return [
                (ctx.machine, ctx.adapter, app_version)
                for ctx in get_all_device_contexts()
            ]
        start_machine_status_broadcast(_get_all_devices_with_version, interval=5)

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
