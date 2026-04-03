"""
services/adms_server.py
========================
Serveur ADMS HTTP intégré au bridge YoGym.
Tourne sur le port 8088 et gère la communication avec les appareils PUSH.

Ce serveur est un singleton — une seule instance par processus.
Chaque ADMSAdapter s'enregistre auprès du serveur via register_adapter().
"""

import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Callable

from flask import Flask, request, Response

logger = logging.getLogger("ADMS-Server")


class ADMSServer:
    """
    Serveur HTTP ADMS (singleton).
    Gère les endpoints /iclock/* pour les appareils ZKTeco en mode PUSH.
    """

    _instance: Optional["ADMSServer"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, port: int = 8088):
        if self._initialized:
            return
        self._initialized = True

        self.port = port
        self.app = Flask("ADMS")

        # SN -> ADMSAdapter mapping
        self._adapters: Dict[str, "ADMSAdapter"] = {}
        # IP -> ADMSAdapter mapping (pour le handshake avant de connaître le SN)
        self._ip_adapters: Dict[str, "ADMSAdapter"] = {}

        # Callback pour les pointages (appelé avec payload dict)
        self.on_attendance: Optional[Callable] = None

        # Callback pour les query results
        self.on_query_result: Optional[Callable] = None

        self._setup_routes()
        self._thread: Optional[threading.Thread] = None

    def register_adapter(self, adapter: "ADMSAdapter"):
        """
        Enregistre un ADMSAdapter pour une machine.
        Appelé par DeviceManager lors de l'initialisation.
        """
        ip = adapter.machine.addresseip
        self._ip_adapters[ip] = adapter

        # Si on connaît déjà le SN (re-register), l'ajouter aussi
        if adapter.sn:
            self._adapters[adapter.sn] = adapter

        logger.info("📋 ADMS adapter enregistré: IP=%s (alias=%s)", ip, adapter.machine.alias)

    def _find_adapter(self, sn: str, ip: str = None) -> Optional["ADMSAdapter"]:
        """Trouve l'adapter par SN, ou par IP si SN pas encore connu."""
        # D'abord par SN
        adapter = self._adapters.get(sn)
        if adapter:
            return adapter

        # Sinon par IP (premier handshake)
        if ip:
            adapter = self._ip_adapters.get(ip)
            if adapter:
                # Associer le SN pour les prochaines fois
                adapter.sn = sn
                self._adapters[sn] = adapter
                return adapter

        # Fallback: si un seul adapter, c'est probablement lui
        if len(self._ip_adapters) == 1:
            adapter = list(self._ip_adapters.values())[0]
            adapter.sn = sn
            self._adapters[sn] = adapter
            return adapter

        return None

    def _setup_routes(self):
        app = self.app

        @app.route("/iclock/cdata", methods=["GET", "POST"])
        def cdata():
            sn = request.args.get("SN", "?")
            remote_ip = request.remote_addr

            if request.method == "GET":
                # Handshake
                push_ver = request.args.get("pushver", "")
                device_type = request.args.get("DeviceType", "")

                adapter = self._find_adapter(sn, remote_ip)
                if adapter:
                    adapter.on_handshake(sn, push_ver, device_type, remote_ip)

                logger.info("✅ HANDSHAKE: SN=%s, PushVer=%s, IP=%s", sn, push_ver, remote_ip)

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

            # POST — données entrantes
            table = request.args.get("table", "")
            body = request.data.decode("utf-8", errors="replace")

            adapter = self._find_adapter(sn)
            if adapter:
                adapter.on_heartbeat()

            if table == "ATTLOG":
                self._handle_attendance(sn, body)
            elif table == "OPERLOG":
                self._handle_operlog(sn, body)
            elif table == "ATTPHOTO":
                logger.info("📸 PHOTO [%s]: %d bytes", sn, len(body))
            elif table == "options":
                logger.info("⚙️ OPTIONS [%s]: %s", sn, body[:300])
            else:
                logger.info("📦 DATA [%s] table=%s: %s", sn, table, body[:200])

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/registry", methods=["GET", "POST"])
        def registry():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📡 REGISTRY: SN=%s", sn)
            if body:
                logger.info("   %s", body[:200])
            return Response(f"RegistryCode=\t{sn}", status=200, content_type="text/plain")

        @app.route("/iclock/getrequest", methods=["GET"])
        def getrequest():
            sn = request.args.get("SN", "?")
            adapter = self._find_adapter(sn)
            if adapter:
                adapter.on_heartbeat()
                cmd = adapter.get_next_command()
                if cmd:
                    logger.info("📤 CMD → %s: %s", sn, cmd)
                    return Response(cmd, status=200, content_type="text/plain")

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/devicecmd", methods=["POST"])
        def devicecmd():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📨 CMD RESULT [%s]: %s", sn, body[:300])

            adapter = self._find_adapter(sn)
            if adapter:
                adapter.on_command_result(body)

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/querydata", methods=["POST"])
        def querydata():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📊 QUERY [%s]: %s", sn, body[:500])

            if self.on_query_result:
                self.on_query_result(sn, body)

            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/fdata", methods=["POST"])
        def fdata():
            sn = request.args.get("SN", "?")
            logger.info("📁 FILE [%s]: %d bytes", sn, len(request.data))
            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/push", methods=["POST"])
        def push():
            return Response("OK", status=200, content_type="text/plain")

        @app.route("/iclock/ping", methods=["GET"])
        def ping():
            sn = request.args.get("SN", "?")
            adapter = self._find_adapter(sn)
            if adapter:
                adapter.on_heartbeat()
            return Response("OK", status=200, content_type="text/plain")

    def _handle_attendance(self, sn: str, body: str):
        """Parse les pointages et appelle le callback."""
        for line in body.strip().split("\n"):
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue

            record = {
                "sn": sn,
                "pin": parts[0],
                "datetime": parts[1],
                "status": parts[2] if len(parts) > 2 else "0",
                "verify": parts[3] if len(parts) > 3 else "0",
            }
            logger.info(
                "📋 POINTAGE: PIN=%s, Date=%s, Status=%s",
                record["pin"], record["datetime"], record["status"]
            )

            if self.on_attendance:
                try:
                    self.on_attendance(sn, record)
                except Exception as e:
                    logger.error("Callback attendance error: %s", e)

    def _handle_operlog(self, sn: str, body: str):
        for line in body.strip().split("\n"):
            if line.strip():
                logger.info("📝 OPERLOG [%s]: %s", sn, line.strip()[:200])

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def start(self):
        """Démarre le serveur ADMS dans un thread dédié."""
        if self._thread and self._thread.is_alive():
            logger.warning("ADMS server déjà démarré")
            return

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ADMSServerThread",
        )
        self._thread.start()
        logger.info("🚀 ADMS server démarré sur port %s", self.port)

    def _run(self):
        self.app.run(
            host="0.0.0.0",
            port=self.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_connected_devices(self) -> Dict[str, dict]:
        """Retourne les infos des appareils connectés."""
        result = {}
        for sn, adapter in self._adapters.items():
            result[sn] = {
                "sn": sn,
                "ip": adapter.machine.addresseip,
                "alias": adapter.machine.alias,
                "connected": adapter.is_connected(),
                "push_version": adapter.push_version,
                "last_seen": adapter.last_seen,
            }
        return result
