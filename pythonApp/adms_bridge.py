"""
YoGym ADMS Bridge Server
=========================
Remplacement du bridge Python SDK (pyzk/zkemkeeper) pour les appareils
ZKTeco SpeedFace-V3L en mode PUSH (protocole ADMS).

Architecture:
  - L'appareil PUSH se connecte à ce serveur HTTP
  - Les pointages arrivent en temps réel et sont envoyés sur HTTP (Spring Boot) + WebSocket
  - Les commandes (add_user, delete, open_door...) sont mises en queue
    et envoyées à l'appareil via le heartbeat /iclock/getrequest
  - Compatible avec l'API REST existante de YoGym

Config appareil:
  Protocole: PUSH | Type: PUSH A&C
  Adresse serveur: IP de ce PC | Port: 8088

Lancer:
  python adms_bridge.py <TENANT> <GYM_BRANCH_ID>
  Exemple: python adms_bridge.py empire 1003
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, request, jsonify, Response, abort
from flask_cors import CORS

# ============================================================
# Configuration
# ============================================================
ADMS_PORT = 8088
FLASK_API_PORT = 9998
WS_PORT = 8765

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ADMS-Bridge")


# ============================================================
# Domain Models
# ============================================================
class Operation(Enum):
    ADD_USER = "ADD_USER"
    AUTHORIZE_USER = "AUTHORIZE_USER"
    DELETE_USER = "DELETE_USER"
    UNAUTHORIZE_USER = "UNAUTHORIZE_USER"
    ADD_FINGERPRINT = "ADD_FINGERPRINT"
    REMOVE_FINGERPRINT = "REMOVE_FINGERPRINT"
    OPEN_DOOR = "OPEN_DOOR"
    UPLOAD_PHOTO = "UPLOAD_PHOTO"
    SYNC_TIME = "SYNC_TIME"
    REBOOT = "REBOOT"
    GET_USERS = "GET_USERS"
    GET_INFO = "GET_INFO"


@dataclass
class DeviceInfo:
    sn: str
    push_version: str = ""
    device_type: str = ""
    ip: str = ""
    last_seen: str = ""
    connected: bool = False
    machine_id: Optional[int] = None
    alias: str = ""
    porte_type: str = "ENTREE"


# ============================================================
# WebSocket Service
# ============================================================
_ws_loop = None
_ws_clients = set()


async def _ws_handler(ws):
    _ws_clients.add(ws)
    try:
        async for message in ws:
            try:
                data = json.loads(message)
                logger.debug("[WS] Received: %s", data)
            except json.JSONDecodeError:
                pass
    finally:
        _ws_clients.discard(ws)


async def _ws_broadcast(payload: dict):
    if _ws_clients:
        msg = json.dumps(payload, ensure_ascii=False)
        import websockets
        await asyncio.gather(*(ws.send(msg) for ws in _ws_clients), return_exceptions=True)


def _ws_thread():
    global _ws_loop
    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)

    async def _start():
        try:
            import websockets
            server = await websockets.serve(_ws_handler, "localhost", WS_PORT)
            logger.info("WebSocket démarré sur ws://localhost:%s", WS_PORT)
            await server.wait_closed()
        except ImportError:
            logger.warning("websockets non installé, WS désactivé")
        except Exception as e:
            logger.error("WebSocket error: %s", e)

    _ws_loop.create_task(_start())
    _ws_loop.run_forever()


def start_ws_server():
    t = threading.Thread(target=_ws_thread, daemon=True, name="WebSocketThread")
    t.start()
    while _ws_loop is None:
        time.sleep(0.05)


def broadcast_ws(payload: dict):
    if _ws_loop and not _ws_loop.is_closed():
        asyncio.run_coroutine_threadsafe(_ws_broadcast(payload), _ws_loop)


def send_pointage(pointage_data: dict, gym_branch_id: str):
    broadcast_ws({
        "type": "pointage",
        "channel": "pointage",
        "gymBranchId": gym_branch_id,
        "data": pointage_data,
        "timestamp": time.time(),
    })


# ============================================================
# ADMS Command Queue (SQLite backed)
# ============================================================
class CommandQueue:
    """Queue de commandes à envoyer aux appareils, persistée en SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
        # In-memory queue pour les commandes ADMS prêtes à envoyer
        self._adms_queue: Dict[str, List[str]] = defaultdict(list)
        # Tracking des résultats de commandes
        self._results: Dict[int, Optional[str]] = {}
        self._lock = threading.Lock()
        self._cmd_counter = 1000

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sn TEXT NOT NULL,
                task_data TEXT NOT NULL,
                adms_cmd TEXT,
                cmd_id INTEGER,
                status TEXT NOT NULL DEFAULT 'PENDING',
                result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def next_cmd_id(self) -> int:
        with self._lock:
            self._cmd_counter += 1
            return self._cmd_counter

    def add_task(self, sn: str, task: dict) -> int:
        """Ajoute une tâche et génère la commande ADMS correspondante."""
        cmd_id = self.next_cmd_id()
        adms_cmd = self._build_adms_command(task, cmd_id)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO task_queue (sn, task_data, adms_cmd, cmd_id, status) VALUES (?, ?, ?, ?, 'PENDING')",
            (sn, json.dumps(task), adms_cmd, cmd_id),
        )
        conn.commit()
        conn.close()

        # Aussi en mémoire pour envoi rapide
        with self._lock:
            self._adms_queue[sn].append(adms_cmd)

        logger.info("📥 Task queued [%s] cmd=%s: %s", sn, cmd_id, task.get("operation", "?"))
        return cmd_id

    def get_next_command(self, sn: str) -> Optional[str]:
        """Récupère la prochaine commande ADMS à envoyer à l'appareil."""
        with self._lock:
            if self._adms_queue[sn]:
                return self._adms_queue[sn].pop(0)
        return None

    def mark_result(self, cmd_id: int, result: str):
        """Enregistre le résultat d'une commande."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE task_queue SET status='COMPLETED', result=?, completed_at=CURRENT_TIMESTAMP WHERE cmd_id=?",
            (result, cmd_id),
        )
        conn.commit()
        conn.close()
        # Nettoyage
        self._cleanup_old(conn=None)

    def _cleanup_old(self, conn=None):
        c = conn or sqlite3.connect(self.db_path)
        c.execute("DELETE FROM task_queue WHERE status='COMPLETED'")
        c.commit()
        if conn is None:
            c.close()

    def reload_pending(self):
        """Au démarrage, recharger les tâches PENDING dans la queue mémoire."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT sn, adms_cmd FROM task_queue WHERE status='PENDING' ORDER BY created_at"
        ).fetchall()
        conn.close()
        with self._lock:
            for sn, cmd in rows:
                if cmd:
                    self._adms_queue[sn].append(cmd)
        logger.info("♻️ %d tâches PENDING rechargées", len(rows))

    def _build_adms_command(self, task: dict, cmd_id: int) -> str:
        """Convertit une tâche métier en commande ADMS."""
        op = task.get("operation", "").upper()

        if op == "ADD_USER":
            pin = task["user_pin"]
            name = task.get("user_name", "")
            card = task.get("card_no", "")
            start = task.get("start_date", "")
            end = task.get("end_date", "")
            # DATA UPDATE user avec les champs
            parts = [f"C:{cmd_id}:DATA UPDATE user"]
            parts.append(f"\tPin={pin}")
            parts.append(f"\tName={name}")
            if card:
                parts.append(f"\tCardNo={card}")
            parts.append(f"\tPri=0")
            parts.append(f"\tGrp=1")
            if start:
                parts.append(f"\tStartTime={start}")
            if end:
                parts.append(f"\tEndTime={end}")
            return "".join(parts)

        elif op == "DELETE_USER":
            pin = task["user_pin"]
            return f"C:{cmd_id}:DATA DELETE user\tPin={pin}"

        elif op == "AUTHORIZE_USER":
            pin = task["user_pin"]
            start = task.get("start_date", "")
            end = task.get("end_date", "")
            return f"C:{cmd_id}:DATA UPDATE user\tPin={pin}\tStartTime={start}\tEndTime={end}"

        elif op == "UNAUTHORIZE_USER":
            pin = task["user_pin"]
            # Mettre une date expirée
            return f"C:{cmd_id}:DATA UPDATE user\tPin={pin}\tStartTime=2020-01-01 00:00:00\tEndTime=2020-01-02 23:59:59"

        elif op == "ADD_FINGERPRINT":
            pin = task["user_pin"]
            fid = task.get("finger_id", 0)
            template = task.get("fingerprint_template", "")
            return f"C:{cmd_id}:DATA UPDATE biodata\tPin={pin}\tNo={fid}\tIndex=0\tValid=1\tDuress=0\tType=1\tMajorVer=5\tMinorVer=50\tFormat=0\tTmp={template}"

        elif op == "REMOVE_FINGERPRINT":
            pin = task["user_pin"]
            fid = task.get("finger_id", 0)
            return f"C:{cmd_id}:DATA DELETE biodata\tPin={pin}\tNo={fid}\tType=1"

        elif op == "OPEN_DOOR":
            door = task.get("door", 1)
            duration = task.get("duration", 5)
            return f"C:{cmd_id}:CONTROL DEVICE 01:{door}:{duration}"

        elif op == "UPLOAD_PHOTO":
            # Photo upload via ADMS nécessite un mécanisme de fichier
            pin = task["user_pin"]
            photo_b64 = task.get("photo", "")
            return f"C:{cmd_id}:DATA UPDATE biophoto\tPin={pin}\tType=9\tFormat=0\tUrl=\tContent={photo_b64}"

        elif op == "SYNC_TIME":
            return f"C:{cmd_id}:SET OPTIONS DateTime={int(time.time())}"

        elif op == "REBOOT":
            return f"C:{cmd_id}:CONTROL DEVICE 02"

        elif op == "GET_USERS":
            return f"C:{cmd_id}:DATA QUERY tablename=user,fielddesc=*,filter=*"

        elif op == "GET_INFO":
            return f"C:{cmd_id}:GET OPTIONS ~SerialNumber,FirmVer,~DeviceName,IPAddress,MACAddress,~Platform"

        elif op == "RAW":
            return f"C:{cmd_id}:{task.get('raw', '')}"

        else:
            logger.warning("Opération inconnue: %s", op)
            return f"C:{cmd_id}:INFO"


# ============================================================
# ADMS Server (Flask)
# ============================================================
class ADMSBridge:
    """
    Serveur ADMS + API REST pour YoGym.
    Gère la communication bidirectionnelle avec les appareils ZKTeco en mode PUSH.
    """

    def __init__(self, tenant: str, gym_branch_id: str):
        self.tenant = tenant
        self.gym_branch_id = gym_branch_id
        self.stop_event = threading.Event()

        # Devices
        self.devices: Dict[str, DeviceInfo] = {}
        # SN -> machine_id mapping (chargé depuis l'API YoGym)
        self.sn_to_machine: Dict[str, dict] = {}

        # Attendance logs en mémoire (buffer circulaire)
        self.attendance_logs: List[dict] = []
        self.MAX_LOGS = 10000

        # Query results
        self.query_results: Dict[str, str] = defaultdict(str)

        # DB
        app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
        app_dir = os.path.join(app_data, "desktop-app")
        os.makedirs(app_dir, exist_ok=True)
        db_path = os.path.join(app_dir, "adms_queue.db")

        self.cmd_queue = CommandQueue(db_path)
        self.cmd_queue.reload_pending()

        # Flask apps
        self.adms_app = self._create_adms_app()
        self.api_app = self._create_api_app()

    # --------------------------------------------------------
    # ADMS Flask App (port 8088)
    # --------------------------------------------------------
    def _create_adms_app(self) -> Flask:
        app = Flask("ADMS")

        @app.route("/iclock/cdata", methods=["GET", "POST"])
        def cdata():
            sn = request.args.get("SN", "?")

            if request.method == "GET":
                push_ver = request.args.get("pushver", "")
                device_type = request.args.get("DeviceType", "")
                self.devices[sn] = DeviceInfo(
                    sn=sn,
                    push_version=push_ver,
                    device_type=device_type,
                    ip=request.remote_addr,
                    last_seen=datetime.now().isoformat(),
                    connected=True,
                )
                logger.info("✅ HANDSHAKE: SN=%s, PushVer=%s, IP=%s", sn, push_ver, request.remote_addr)

                session_id = hashlib.md5(f"{sn}{time.time()}".encode()).hexdigest().upper()
                config = "\r\n".join([
                    f"GET OPTION FROM: {sn}",
                    "Stamp=0", "OpStamp=0", "PhotoStamp=0",
                    "ErrorDelay=60",
                    "Delay=2",
                    "RequestDelay=2",
                    "TransTimes=00:00;14:05",
                    "TransInterval=1",
                    "TransFlag=1111000000",
                    "Realtime=1",
                    "Encrypt=0",
                    "ServerVer=3.4.1",
                    "PushProtVer=3.1.2",
                    f"SessionID={session_id}",
                    "TimeoutSec=30",
                    "PushOptionsFlag=1",
                ])
                return Response(config, status=200, content_type="text/plain")

            # POST - données entrantes
            table = request.args.get("table", "")
            body = request.data.decode("utf-8", errors="replace")
            self._touch_device(sn)

            if table == "ATTLOG":
                self._handle_attendance(sn, body)
            elif table == "OPERLOG":
                self._handle_operlog(sn, body)
            elif table == "ATTPHOTO":
                self._handle_att_photo(sn, body)
            elif table == "options":
                logger.info("⚙️ OPTIONS [%s]: %s", sn, body[:300])
            else:
                logger.info("📦 DATA [%s] table=%s: %s", sn, table, body[:200])

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/registry", methods=["GET", "POST"])
        def registry():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📡 REGISTRY: SN=%s %s", sn, body[:200] if body else "")
            return Response(f"RegistryCode=\t{sn}", status=200, content_type="text/plain")

        @app.route("/iclock/getrequest", methods=["GET"])
        def getrequest():
            sn = request.args.get("SN", "?")
            self._touch_device(sn)

            cmd = self.cmd_queue.get_next_command(sn)
            if cmd:
                logger.info("📤 CMD → %s: %s", sn, cmd)
                return Response(cmd, status=200, content_type="text/plain")

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/devicecmd", methods=["POST"])
        def devicecmd():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📨 CMD RESULT [%s]: %s", sn, body[:300])

            # Parser le résultat: ID=xxx&Return=0&CMD=...
            self._handle_cmd_result(sn, body)

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/querydata", methods=["POST"])
        def querydata():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📊 QUERY [%s]: %s", sn, body[:500])
            self.query_results[sn] = body
            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/fdata", methods=["POST"])
        def fdata():
            sn = request.args.get("SN", "?")
            logger.info("📁 FILE [%s]: %d bytes", sn, len(request.data))
            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/push", methods=["POST"])
        def push():
            return Response("OK", status=200, content_type="text/plain")

        return app

    # --------------------------------------------------------
    # API Flask App (port 9998) — compatible avec l'API existante
    # --------------------------------------------------------
    def _create_api_app(self) -> Flask:
        app = Flask("API")
        CORS(app)

        # ---- Endpoint compatible: /tasks/access ----
        @app.route("/tasks/access", methods=["POST"])
        def enqueue_access_tasks():
            data = request.get_json(force=True)
            required = {"gymBranchId", "operation", "userPin", "cardNo", "startDate", "endDate", "machines"}
            if not required.issubset(data):
                missing = required - data.keys()
                abort(400, f"Champs manquants: {', '.join(missing)}")

            if str(data["gymBranchId"]) != str(self.gym_branch_id):
                abort(400, "gymBranchId mismatch")

            queued = 0
            for m in data.get("machines", []):
                sn = self._find_sn_for_machine(m.get("id"))
                if not sn:
                    # Essayer avec tous les appareils connectés
                    for s in self.devices:
                        sn = s
                        break

                if sn:
                    self.cmd_queue.add_task(sn, {
                        "operation": data["operation"],
                        "user_pin": data["userPin"],
                        "user_name": data.get("username", ""),
                        "card_no": data["cardNo"],
                        "start_date": data["startDate"],
                        "end_date": data["endDate"],
                    })
                    queued += 1

            return jsonify({"status": "queued", "tasksQueued": queued}), 201

        # ---- Endpoint compatible: /door/open ----
        @app.route("/door/open", methods=["POST"])
        def open_door():
            data = request.get_json(force=True) or {}
            required = {"gymBranchId", "machineId"}
            if not required.issubset(data):
                abort(400, "Champs manquants")

            duration = int(data.get("duration", 5))
            door_no = int(data.get("door", 1))
            pin = data.get("pin")
            porte_type = data.get("porte_type", "ENTREE")

            sn = self._find_sn_for_machine(data["machineId"])
            if not sn:
                for s in self.devices:
                    sn = s
                    break

            if not sn:
                abort(404, "Aucun appareil connecté")

            self.cmd_queue.add_task(sn, {
                "operation": "OPEN_DOOR",
                "door": door_no,
                "duration": duration,
            })

            # Envoyer pointage si pin fourni
            if pin:
                try:
                    now = datetime.now()
                    payload = {
                        "id_machine": data["machineId"],
                        "ip_adress": self.devices.get(sn, DeviceInfo(sn="")).ip,
                        "machine_type": "PUSH_AC",
                        "is_access_valid": True,
                        "pin": int(pin),
                        "access_date_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "door_id": door_no,
                        "gym_branch_id": str(self.gym_branch_id),
                        "cardNo": "",
                        "porte_type": porte_type,
                    }
                    send_pointage(payload, str(self.gym_branch_id))
                    from services.http_client import send_pointage as send_pointage_http
                    send_pointage_http(payload, data["machineId"])
                except Exception as ex:
                    logger.error("Erreur envoi pointage manuel: %s", ex)

            return jsonify({"status": "OK", "machineId": data["machineId"], "duration": duration})

        # ---- Fingerprint actions (from Spring Boot) ----
        @app.route("/fingerprint/actions", methods=["POST"])
        def fingerprint_actions():
            data = request.get_json(force=True)
            if not data or "pin" not in data or "operation" not in data:
                abort(400, "pin et operation requis")

            results = {}
            for sn in self.devices:
                self.cmd_queue.add_task(sn, {
                    "operation": data["operation"],
                    "user_pin": str(data["pin"]),
                    "finger_id": data.get("finger_id", 0),
                    "fingerprint_template": data.get("fingerprint_template", ""),
                })
                results[sn] = "QUEUED"

            return jsonify({"status": "queued", "results": results}), 201

        # ---- Face upload compatible ----
        @app.route("/face/upload", methods=["POST"])
        def upload_face():
            pin = request.form.get("pin")
            gym_bid = request.form.get("gymBranchId")
            file = request.files.get("photo")

            if not all([pin, gym_bid, file]):
                abort(400, "pin, gymBranchId et photo requis")

            photo_bytes = file.read()
            photo_b64 = base64.b64encode(photo_bytes).decode("utf-8")

            report = {}
            for sn in self.devices:
                self.cmd_queue.add_task(sn, {
                    "operation": "UPLOAD_PHOTO",
                    "user_pin": pin,
                    "photo": photo_b64,
                })
                report[sn] = "QUEUED"

            return jsonify({"pin": pin, "result": report})

        # ---- Fingerprint upload ----
        @app.route("/fingerprint/upload", methods=["POST"])
        def upload_fingerprint():
            data = request.get_json()
            if not data or "pin" not in data:
                return jsonify({"error": "Missing pin"}), 400

            pin = data["pin"]
            finger_id = data.get("fingerId", 0)
            template = data.get("fingerprint_template", "")

            results = {}
            for sn in self.devices:
                self.cmd_queue.add_task(sn, {
                    "operation": "ADD_FINGERPRINT",
                    "user_pin": pin,
                    "finger_id": finger_id,
                    "fingerprint_template": template,
                })
                results[sn] = "QUEUED"

            return jsonify({"pin": pin, "results": results})

        # ---- Get fingerprints (query device) ----
        @app.route("/getFingerprints/<int:user_pin>/<int:gym_bid>/<int:machine_id>", methods=["GET"])
        def get_fingerprints(user_pin, gym_bid, machine_id):
            sn = self._find_sn_for_machine(machine_id) or next(iter(self.devices), None)
            if not sn:
                return jsonify({"error": "No device connected"}), 404

            # Query via ADMS
            self.query_results[sn] = ""
            self.cmd_queue.add_task(sn, {
                "operation": "RAW",
                "raw": f"DATA QUERY tablename=biodata,fielddesc=*,filter=Pin={user_pin}",
            })

            # Attendre le résultat (max 10s)
            for _ in range(100):
                if self.query_results[sn]:
                    break
                time.sleep(0.1)

            return jsonify({
                "userPin": str(user_pin),
                "gymBranchId": gym_bid,
                "machineId": machine_id,
                "data": self.query_results.get(sn, ""),
            })

        # ---- Get face photo ----
        @app.route("/getFace/<int:user_pin>/<int:gym_bid>/<int:machine_id>", methods=["GET"])
        def get_face(user_pin, gym_bid, machine_id):
            sn = self._find_sn_for_machine(machine_id) or next(iter(self.devices), None)
            if not sn:
                return jsonify({"error": "No device connected"}), 404

            self.query_results[sn] = ""
            self.cmd_queue.add_task(sn, {
                "operation": "RAW",
                "raw": f"DATA QUERY tablename=biophoto,fielddesc=*,filter=Pin={user_pin}$\tType=9",
            })

            for _ in range(100):
                if self.query_results[sn]:
                    break
                time.sleep(0.1)

            return jsonify({
                "userPin": str(user_pin),
                "data": self.query_results.get(sn, ""),
            })

        # ---- Config ----
        @app.route("/config/gymBranchId", methods=["GET"])
        def get_config():
            return jsonify({"gymBranchId": self.gym_branch_id})

        @app.route("/curentconf", methods=["GET"])
        def current_conf():
            return jsonify({"gymbranchId": self.gym_branch_id, "tenant": self.tenant})

        # ---- Status & devices ----
        @app.route("/api/status", methods=["GET"])
        def api_status():
            return jsonify({
                "server": "YoGym ADMS Bridge",
                "tenant": self.tenant,
                "gymBranchId": self.gym_branch_id,
                "devices": {sn: {"ip": d.ip, "connected": d.connected, "last_seen": d.last_seen}
                            for sn, d in self.devices.items()},
                "attendance_count": len(self.attendance_logs),
            })

        @app.route("/api/devices", methods=["GET"])
        def api_devices():
            return jsonify({sn: {"sn": d.sn, "ip": d.ip, "push_version": d.push_version,
                                  "connected": d.connected, "last_seen": d.last_seen}
                            for sn, d in self.devices.items()})

        @app.route("/api/attendance", methods=["GET"])
        def api_attendance():
            return jsonify(self.attendance_logs[-100:])

        # ---- Direct command API ----
        @app.route("/api/command/<sn>", methods=["POST"])
        def api_command(sn):
            data = request.get_json(force=True, silent=True) or {}
            cmd_type = data.get("command", "").upper()
            if not cmd_type:
                return jsonify({"error": "Missing 'command'"}), 400

            task = {"operation": cmd_type}
            task.update(data)
            # Remap common fields
            if "pin" in data:
                task["user_pin"] = data["pin"]
            if "name" in data:
                task["user_name"] = data["name"]

            cmd_id = self.cmd_queue.add_task(sn, task)
            return jsonify({"status": "queued", "cmd_id": cmd_id, "sn": sn})

        # ---- Soft restart ----
        @app.route("/restart", methods=["POST"])
        def soft_restart():
            logger.info("🔄 Soft restart demandé")
            return jsonify({"status": "restarting"})

        return app

    # --------------------------------------------------------
    # Internal handlers
    # --------------------------------------------------------
    def _touch_device(self, sn: str):
        if sn in self.devices:
            self.devices[sn].last_seen = datetime.now().isoformat()
            self.devices[sn].connected = True

    def _find_sn_for_machine(self, machine_id) -> Optional[str]:
        mid = str(machine_id)
        for sn, info in self.sn_to_machine.items():
            if str(info.get("id")) == mid:
                return sn
        return None

    def _handle_attendance(self, sn: str, body: str):
        """Parse les pointages ATTLOG et envoie sur HTTP + WebSocket."""
        for line in body.strip().split("\n"):
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue

            pin = parts[0]
            dt_str = parts[1]
            status = parts[2] if len(parts) > 2 else "0"
            verify = parts[3] if len(parts) > 3 else "0"

            record = {
                "pin": pin,
                "datetime": dt_str,
                "status": status,
                "verify": verify,
                "sn": sn,
                "received_at": datetime.now().isoformat(),
            }
            self.attendance_logs.append(record)
            if len(self.attendance_logs) > self.MAX_LOGS:
                self.attendance_logs = self.attendance_logs[-self.MAX_LOGS:]

            logger.info("📋 POINTAGE: PIN=%s, Date=%s, Status=%s, Verify=%s", pin, dt_str, status, verify)

            # Envoi HTTP + WebSocket
            device = self.devices.get(sn, DeviceInfo(sn=sn))
            payload = {
                "id_machine": device.machine_id or 0,
                "ip_adress": device.ip,
                "machine_type": "PUSH_AC",
                "is_access_valid": True,
                "pin": int(pin) if pin.isdigit() else 0,
                "access_date_time": dt_str,
                "door_id": 1,
                "gym_branch_id": str(self.gym_branch_id),
                "cardNo": "",
                "porte_type": device.porte_type,
            }

            send_pointage(payload, str(self.gym_branch_id))
            from services.http_client import send_pointage as send_pointage_http
            send_pointage_http(payload, device.machine_id or 0)

    def _handle_operlog(self, sn: str, body: str):
        for line in body.strip().split("\n"):
            line = line.strip()
            if line:
                logger.info("📝 OPERLOG [%s]: %s", sn, line[:200])

    def _handle_att_photo(self, sn: str, body: str):
        pin = request.args.get("PIN", "?")
        size = request.args.get("size", "0")
        logger.info("📸 PHOTO [%s]: PIN=%s, %s bytes", sn, pin, size)

        if len(body) > 0:
            try:
                photo_b64 = base64.b64encode(body.encode("latin-1")).decode("utf-8")
                device = self.devices.get(sn, DeviceInfo(sn=sn))
                from services.http_client import send_photo
                send_photo(
                    pin=pin,
                    photo_b64=photo_b64,
                    machine_id=device.machine_id or 0,
                    gym_branch_id=str(self.gym_branch_id),
                    addresseip=device.ip,
                    port=8088,
                    machine_type="PUSH_AC",
                )
            except Exception as e:
                logger.error("Erreur publication photo: %s", e)

    def _handle_cmd_result(self, sn: str, body: str):
        """Parse ID=xxx&Return=0&CMD=... depuis le résultat."""
        try:
            parts = {}
            for segment in body.strip().split("&"):
                if "=" in segment:
                    k, v = segment.split("=", 1)
                    parts[k.strip()] = v.strip()

            cmd_id = int(parts.get("ID", 0))
            ret = parts.get("Return", "?")
            cmd = parts.get("CMD", "?")

            if ret == "0":
                logger.info("✅ CMD OK [%s]: ID=%s, CMD=%s", sn, cmd_id, cmd)
            else:
                logger.warning("❌ CMD FAIL [%s]: ID=%s, Return=%s, CMD=%s", sn, cmd_id, ret, cmd)

            self.cmd_queue.mark_result(cmd_id, body)
        except Exception as e:
            logger.error("Parse cmd result error: %s (%s)", e, body[:100])

    # --------------------------------------------------------
    # Start
    # --------------------------------------------------------
    def start(self):
        print("=" * 60)
        print("  YoGym ADMS Bridge Server")
        print(f"  Tenant: {self.tenant}")
        print(f"  GymBranchId: {self.gym_branch_id}")
        print(f"  ADMS: http://0.0.0.0:{ADMS_PORT}")
        print(f"  API:  http://0.0.0.0:{FLASK_API_PORT}")
        print(f"  WS:   ws://localhost:{WS_PORT}")
        print("=" * 60)

        # WebSocket
        start_ws_server()

        # API Flask (port 9998)
        threading.Thread(
            target=lambda: self.api_app.run(
                host="0.0.0.0", port=FLASK_API_PORT, debug=False, threaded=True
            ),
            daemon=True,
            name="APIThread",
        ).start()

        # ADMS Flask (port 8088) — main thread
        self.adms_app.run(host="0.0.0.0", port=ADMS_PORT, debug=False, threaded=True)


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python adms_bridge.py <TENANT> <GYM_BRANCH_ID>")
        print("Exemple: python adms_bridge.py empire 1003")
        sys.exit(1)

    tenant = sys.argv[1]
    gym_branch_id = sys.argv[2]

    bridge = ADMSBridge(
        tenant=tenant,
        gym_branch_id=gym_branch_id,
    )

    def handle_sigterm(signum, frame):
        logger.info("🚨 SIGTERM reçu, arrêt...")
        bridge.stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        bridge.start()
    except KeyboardInterrupt:
        logger.info("⏹️ Arrêt demandé")
        bridge.stop_event.set()
