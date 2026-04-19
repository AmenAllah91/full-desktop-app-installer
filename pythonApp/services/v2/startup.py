"""
services/v2/startup.py
=======================
Orchestrateur de démarrage v2 — full PUSH/ADMS.

Toutes les machines sont traitées via ADMS, quel que soit leur type d'origine.
Remplace la logique de startup v1 (C3 / STANDALONE / PUSH).
"""

import base64
import json
import logging
import threading
import time

from services.v2.adms_adapter_v2 import ADMSAdapterV2
from services.v2.adms_server_v2 import ADMSServerV2
from services.v2.device_manager_v2 import DeviceManagerV2
from services.v2.monitor_v2 import setup_attendance_callback, monitor_v2_connectivity

logger = logging.getLogger("Startup-v2")


def start_v2(machines, tenant: str, gym_branch_id: str,
             stop_event, task_queue_functions: dict):
    """
    Point d'entrée v2 — appelé depuis main.py quand --version v2.

    Args:
        machines: liste de AccessMachine récupérées depuis l'API
        tenant: nom du tenant (ex: "empire")
        gym_branch_id: id de la gym branch
        stop_event: threading.Event pour l'arrêt propre
        task_queue_functions: dict avec les fonctions de la queue SQLite
            {
                "initialize_db": callable,
                "add_task": callable,
                "get_next": callable,
                "mark_completed": callable,
            }
    """
    logger.info("=" * 60)
    logger.info("🚀 Démarrage v2 — Full PUSH/ADMS")
    logger.info("   Tenant: %s | GymBranch: %s", tenant, gym_branch_id)
    logger.info("   Machines: %d", len(machines))
    logger.info("=" * 60)

    # 1. Enregistrer toutes les machines dans le DeviceManager v2
    for m in machines:
        DeviceManagerV2.register(m, tenant, gym_branch_id)
        logger.info("   → %s (%s) — type original: %s",
                     m.alias, m.addresseip, m.type)

    # 2. Initialiser la task queue
    task_queue_functions["initialize_db"]()

    # 3. Démarrer le serveur ADMS
    adms_server = ADMSServerV2(port=8088)

    # 4. Enregistrer chaque adapter auprès du serveur
    for ctx in DeviceManagerV2.all():
        adms_server.register_adapter(ctx.adapter)

    adms_server.start()

    # 5. Configurer le callback de pointage (Kafka + WebSocket)
    setup_attendance_callback(adms_server, tenant, gym_branch_id)

    # 6. Démarrer le thread de traitement de la queue
    queue_thread = threading.Thread(
        target=_process_queue_v2,
        args=(stop_event, task_queue_functions),
        daemon=True,
        name="QueueProcessor-v2"
    )
    queue_thread.start()

    # 7. Démarrer le thread de monitoring de connectivité
    monitor_thread = threading.Thread(
        target=monitor_v2_connectivity,
        args=(adms_server, stop_event),
        daemon=True,
        name="Monitor-v2"
    )
    monitor_thread.start()

    logger.info("✅ v2 initialisé — en attente de connexion des machines sur port 8088")

    return adms_server


def _process_queue_v2(stop_event, tq: dict):
    """
    Traitement de la queue de tâches en v2.
    Toutes les commandes passent par ADMS (pas de SDK direct).
    """
    POLL_SLEEP = 0.5
    MAX_RETRIES = 3
    RETRY_SLEEP = 2

    while not stop_event.is_set():
        row = tq["get_next"]()
        if not row:
            time.sleep(POLL_SLEEP)
            continue

        task_id, raw = row
        try:
            task = json.loads(raw)
            machine_id = task["machineId"]
            op = task["operation"]
            pin = task["user_pin"]
        except Exception as exc:
            logger.error("Tâche #%s invalide: %s", task_id, exc)
            tq["mark_completed"](task_id)
            continue

        ctx = DeviceManagerV2.get(machine_id)
        if ctx is None:
            logger.warning("⚠️ Tâche #%s: machine %s non enregistrée, ignorée",
                           task_id, machine_id)
            tq["mark_completed"](task_id)
            continue

        adapter = ctx.adapter

        if not adapter.is_connected():
            logger.warning("⏳ Tâche #%s: machine %s pas encore connectée, "
                           "la tâche reste en file", task_id, ctx.machine.alias)
            time.sleep(RETRY_SLEEP)
            continue

        ok = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if op == "ADD_USER":
                    ok = adapter.add_user(
                        pin, task.get("user_name", ""),
                        task.get("card_no", ""),
                        task.get("start_date", ""),
                        task.get("end_date", ""))
                    if ok:
                        ok = adapter.authorize_user(pin)

                elif op == "DELETE_USER":
                    ok = adapter.delete_user(pin)

                elif op == "AUTHORIZE_USER":
                    ok = adapter.authorize_user(pin)

                elif op == "UNAUTHORIZE_USER":
                    ok = adapter.unauthorize_user(pin)

                elif op == "ADD_FINGERPRINT":
                    tpl_b64 = task["fingerprint_template"]
                    tpl_bytes = base64.b64decode(tpl_b64)
                    ok = adapter.add_fingerprint(
                        pin, tpl_bytes, task["finger_id"])

                elif op == "REMOVE_FINGERPRINT":
                    ok = adapter.delete_fingerprint(pin, task["finger_id"])

                else:
                    logger.error("Opération inconnue: %s", op)
                    ok = True  # skip

                if ok:
                    logger.info("✅ Tâche #%s terminée (%s / %s)",
                                task_id, ctx.machine.alias, op)
                    tq["mark_completed"](task_id)
                    break
                else:
                    raise RuntimeError("Commande ADMS n'a pas retourné Return=0")

            except Exception as exc:
                logger.warning("⚠️ Tâche #%s échec %s/%s: %s",
                               task_id, attempt, MAX_RETRIES, exc)
                time.sleep(RETRY_SLEEP)

        else:
            logger.error("❌ Tâche #%s abandonnée après %s échecs",
                         task_id, MAX_RETRIES)
            tq["mark_completed"](task_id)
