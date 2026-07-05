"""
services/v2/monitor_v2.py
==========================
Monitoring des machines en mode PUSH/ADMS v2.

En PUSH, pas de polling actif — l'appareil envoie les pointages.
Ce module :
  1. Branche le callback de pointage sur le serveur ADMS
  2. Publie sur Kafka + WebSocket
  3. Vérifie périodiquement la connectivité
"""

import json
import logging
import time
from datetime import datetime
from os import getenv
from typing import Optional

from domain.AccessMachine import AccessMachine
from kafka_service.kafkaservice import KafkaService
from services.MachineMonitor import make_rt_json
from services.websocket import send_pointage, send_machine_status
from services.v2.adms_adapter_v2 import ADMSAdapterV2
from services.v2.adms_server_v2 import ADMSServerV2

logger = logging.getLogger("Monitor-v2")


def setup_attendance_callback(adms_server: ADMSServerV2,
                              tenant: str, gym_branch_id: str):
    """
    Configure le callback global de pointage sur le serveur ADMS.
    Chaque pointage reçu est publié sur Kafka et WebSocket.

    Appelé une seule fois au démarrage v2 (pas par machine).
    """
    kafka = KafkaService(getenv("KAFKA_BROKER"), f"group_rt_v2_{tenant}")

    def on_attendance(sn: str, record: dict):
        try:
            # Trouver l'adapter correspondant
            from services.v2.device_manager_v2 import DeviceManagerV2
            ctx = DeviceManagerV2.get_by_sn(sn)

            if ctx is None:
                # Fallback : premier adapter qui match
                for c in DeviceManagerV2.all():
                    if c.adapter.sn == sn:
                        ctx = c
                        break

            if ctx is None:
                logger.warning("⚠️ Pointage reçu de SN=%s mais machine inconnue", sn)
                return

            machine = ctx.machine
            pin_str = record.get("pin", "0")
            pin = int(pin_str) if pin_str.isdigit() else 0
            dt_str = record.get("datetime", "")
            status = record.get("status", "0")

            payload = make_rt_json(
                machine_id=machine.id,
                ip=machine.addresseip,
                mtype="PUSH",
                pin=pin,
                state_code=int(status) if status.isdigit() else 0,
                dt=dt_str,
                door_id=1,
                card_no=None,
                gym_branch_id=gym_branch_id,
                porte_type=machine.porte_type or "ENTREE",
            )

            kafka.produce(f"rt_{tenant}", payload)

            try:
                send_pointage(json.loads(payload), gym_branch_id)
            except Exception as ex:
                logger.error("[WebSocket] Erreur: %s", ex)

            try:
                ctx.adapter.event_count += 1
            except Exception:
                pass

            logger.info("📡 Pointage: SN=%s, PIN=%s → Kafka + WS", sn, pin)

        except Exception as e:
            logger.error("Erreur callback pointage: %s", e, exc_info=True)

    adms_server.on_attendance = on_attendance
    logger.info("✅ Callback de pointage configuré (tenant=%s)", tenant)


def monitor_v2_connectivity(adms_server: ADMSServerV2,
                            stop_evt, check_interval: int = 10):
    """
    Thread de surveillance de la connectivité.
    Log l'état des machines périodiquement.
    """
    logged_states = {}  # machine_id -> was_connected

    while not stop_evt.is_set():
        for adapter in adms_server.get_adapters():
            mid = adapter.machine.id
            connected = adapter.is_connected()
            was_connected = logged_states.get(mid)

            if connected and not was_connected:
                logger.info("✅ [%s] %s connecté (SN=%s)",
                            adapter.machine.alias,
                            adapter.machine.addresseip,
                            adapter.sn)
                logged_states[mid] = True
                adapter.online_since = datetime.now()
                adapter.last_seen = datetime.now()
                adapter.offline_since = None
                adapter.last_error = ""
                send_machine_status(adapter.machine, adapter, app_version="v2")

            elif not connected and was_connected:
                logger.warning("⚠️ [%s] %s déconnecté",
                               adapter.machine.alias,
                               adapter.machine.addresseip)
                logged_states[mid] = False
                adapter.offline_since = datetime.now()
                adapter.last_error = "Connexion perdue (heartbeat timeout)"
                send_machine_status(adapter.machine, adapter, app_version="v2")

            elif connected and was_connected is None:
                # Premier check — déjà connecté
                logger.info("✅ [%s] %s connecté (SN=%s)",
                            adapter.machine.alias,
                            adapter.machine.addresseip,
                            adapter.sn)
                logged_states[mid] = True
                adapter.online_since = datetime.now()
                adapter.last_seen = datetime.now()
                adapter.offline_since = None
                adapter.last_error = ""
                send_machine_status(adapter.machine, adapter, app_version="v2")

        # Sleep interruptible
        for _ in range(check_interval * 5):
            if stop_evt.is_set():
                break
            time.sleep(0.2)

    logger.info("🛑 Monitor v2 arrêté")
