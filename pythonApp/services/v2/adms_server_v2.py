"""
services/v2/adms_server_v2.py
==============================
Serveur ADMS HTTP v2 — réécriture propre.

Écoute sur le port 8088 et gère le protocole PUSH ZKTeco :
  - Handshake (GET /iclock/cdata)
  - Réception de pointages (POST /iclock/cdata table=ATTLOG)
  - Distribution de commandes (GET /iclock/getrequest)
  - Réception des résultats (POST /iclock/devicecmd)
  - Queries asynchrones (POST /iclock/querydata)

Toutes les machines sont enregistrées ici — pas de distinction de type.
"""

import hashlib
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Callable, List

from flask import Flask, request, Response

logger = logging.getLogger("ADMS-v2-Server")


class ADMSServerV2:
    """Serveur ADMS HTTP — singleton par processus."""

    _instance: Optional["ADMSServerV2"] = None
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
        self.app = Flask("ADMS-v2")
        self.app.logger.setLevel(logging.WARNING)

        # Registres d'adapters
        self._adapters_by_sn: Dict[str, "ADMSAdapterV2"] = {}
        self._adapters_by_ip: Dict[str, "ADMSAdapterV2"] = {}

        # Callbacks
        self.on_attendance: Optional[Callable] = None
        self.on_query_result: Optional[Callable] = None

        self._thread: Optional[threading.Thread] = None
        self._setup_routes()

    # ----------------------------------------------------------------
    # Enregistrement des adapters
    # ----------------------------------------------------------------

    def register_adapter(self, adapter: "ADMSAdapterV2"):
        ip = adapter.machine.addresseip
        self._adapters_by_ip[ip] = adapter
        if adapter.sn:
            self._adapters_by_sn[adapter.sn] = adapter
        logger.info("📋 Adapter enregistré: IP=%s, alias=%s",
                     ip, adapter.machine.alias)

    def get_adapters(self) -> List["ADMSAdapterV2"]:
        """Retourne tous les adapters enregistrés (unique par id)."""
        seen = set()
        result = []
        for a in list(self._adapters_by_ip.values()) + list(self._adapters_by_sn.values()):
            if a.machine.id not in seen:
                seen.add(a.machine.id)
                result.append(a)
        return result

    def _find_adapter(self, sn: str, ip: str = None) -> Optional["ADMSAdapterV2"]:
        """Trouve l'adapter par SN puis par IP."""
        adapter = self._adapters_by_sn.get(sn)
        if adapter:
            return adapter

        if ip:
            adapter = self._adapters_by_ip.get(ip)
            if adapter:
                adapter.sn = sn
                self._adapters_by_sn[sn] = adapter
                return adapter

        # Fallback : si un seul adapter enregistré
        if len(self._adapters_by_ip) == 1:
            adapter = list(self._adapters_by_ip.values())[0]
            adapter.sn = sn
            self._adapters_by_sn[sn] = adapter
            return adapter

        return None

    # ----------------------------------------------------------------
    # Routes HTTP
    # ----------------------------------------------------------------

    def _setup_routes(self):
        app = self.app

        # ---- Handshake + réception de données ----
        @app.route("/iclock/cdata", methods=["GET", "POST"])
        def cdata():
            sn = request.args.get("SN", "?")
            remote_ip = request.remote_addr

            if request.method == "GET":
                push_ver = request.args.get("pushver", "")
                device_type = request.args.get("DeviceType", "")

                adapter = self._find_adapter(sn, remote_ip)
                if adapter:
                    adapter.on_handshake(sn, push_ver, device_type, remote_ip)

                session_id = hashlib.md5(
                    f"{sn}{time.time()}".encode()
                ).hexdigest().upper()

                config = "\r\n".join([
                    f"GET OPTION FROM: {sn}",
                    "Stamp=0",
                    "OpStamp=0",
                    "PhotoStamp=0",
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
                logger.info("📸 Photo [%s]: %d bytes", sn, len(body))
            else:
                logger.debug("📦 Data [%s] table=%s: %s", sn, table, body[:200])

            return Response("OK", status=200, content_type="text/plain")

        # ---- Enregistrement appareil ----
        @app.route("/iclock/registry", methods=["GET", "POST"])
        def registry():
            sn = request.args.get("SN", "?")
            logger.info("📡 Registry: SN=%s", sn)
            return Response(f"RegistryCode=\t{sn}", status=200,
                            content_type="text/plain")

        # ---- Distribution de commandes ----
        @app.route("/iclock/getrequest", methods=["GET"])
        def getrequest():
            sn = request.args.get("SN", "?")
            adapter = self._find_adapter(sn)
            if adapter:
                adapter.on_heartbeat()
                cmd = adapter.get_next_command()
                if cmd:
                    logger.info("📤 CMD → %s:\n   %s", sn, cmd)
                    return Response(cmd, status=200, content_type="text/plain")
            return Response("OK", status=200, content_type="text/plain")

        # ---- Résultat de commande ----
        @app.route("/iclock/devicecmd", methods=["POST"])
        def devicecmd():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📨 Result [%s]: FULL BODY:\n   %s", sn, body)

            adapter = self._find_adapter(sn)
            if adapter:
                adapter.on_command_result(body)
            return Response("OK", status=200, content_type="text/plain")

        # ---- Résultat de query ----
        @app.route("/iclock/querydata", methods=["POST"])
        def querydata():
            sn = request.args.get("SN", "?")
            body = request.data.decode("utf-8", errors="replace")
            logger.info("📊 Query [%s]: %s", sn, body[:500])

            if self.on_query_result:
                self.on_query_result(sn, body)
            return Response("OK", status=200, content_type="text/plain")

        # ---- Transfert de fichiers ----
        @app.route("/iclock/fdata", methods=["POST"])
        def fdata():
            sn = request.args.get("SN", "?")
            logger.info("📁 File [%s]: %d bytes", sn, len(request.data))
            return Response("OK", status=200, content_type="text/plain")

        # ---- Push / Ping ----
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

    # ----------------------------------------------------------------
    # Traitement des pointages
    # ----------------------------------------------------------------

    def _handle_attendance(self, sn: str, body: str):
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
            logger.info("📋 Pointage: SN=%s, PIN=%s, Date=%s",
                         sn, record["pin"], record["datetime"])

            if self.on_attendance:
                try:
                    self.on_attendance(sn, record)
                except Exception as e:
                    logger.error("Attendance callback error: %s", e)

    def _handle_operlog(self, sn: str, body: str):
        for line in body.strip().split("\n"):
            if line.strip():
                logger.debug("📝 Operlog [%s]: %s", sn, line.strip()[:200])

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning("Serveur ADMS déjà en cours")
            return

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ADMS-v2-Server")
        self._thread.start()
        logger.info("🚀 Serveur ADMS v2 démarré sur port %s", self.port)

    def _run(self):
        self.app.run(
            host="0.0.0.0", port=self.port,
            debug=False, threaded=True, use_reloader=False)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_status(self) -> dict:
        """Retourne l'état de tous les appareils connectés."""
        devices = {}
        for adapter in self.get_adapters():
            key = adapter.sn or adapter.machine.addresseip
            devices[key] = {
                "sn": adapter.sn,
                "ip": adapter.machine.addresseip,
                "alias": adapter.machine.alias,
                "original_type": adapter.machine.type,
                "connected": adapter.is_connected(),
                "push_version": adapter.push_version,
                "last_seen": adapter.last_seen.isoformat() if adapter.last_seen else None,
            }
        return devices
