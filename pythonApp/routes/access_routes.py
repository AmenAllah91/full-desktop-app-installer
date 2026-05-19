import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, abort

import app_context as ctx
from services.MachineMonitor import make_rt_json, kafka
from services.websocket import send_pointage

access_bp = Blueprint("access", __name__)


REQUIRED_TOP = {
    "gymBranchId", "operation",
    "userPin", "cardNo",
    "startDate", "endDate",
    "machines"
}
REQUIRED_MACHINE = {"id", "addresseip", "port", "type"}
REQUIRED_OPEN = {"gymBranchId", "machineId"}


@access_bp.route('/tasks/access', methods=['POST'])
def enqueue_access_tasks():
    try:
        data = request.get_json(force=True)
    except Exception:
        abort(400, "Payload JSON invalide")

    if not REQUIRED_TOP.issubset(data):
        missing = REQUIRED_TOP - data.keys()
        abort(400, f"Champs manquants : {', '.join(missing)}")

    if str(data["gymBranchId"]) != ctx.currentGymBranchId:
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
        ctx.add_task_to_queue(task)
        queued += 1

    return jsonify({"status": "queued", "tasksQueued": queued}), 201


@access_bp.route('/door/open', methods=['POST'])
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

        dctx = ctx.get_device_context(machine_id)
        if not dctx:
            abort(404, f"Machine {machine_id} inconnue dans DeviceManager")

        adapter = dctx.adapter

        with dctx.lock:
            from services.adapters import PlcommAdapter
            from services.zkem_adapter import ZkemAdapter
            from services.adms_adapter import ADMSAdapter

            if ctx.app_version == "v2":
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
                machine_id=dctx.machine.id,
                ip=dctx.machine.addresseip,
                mtype=dctx.machine.type,
                pin=int(pin),
                state_code=state_code,
                dt=dt_str,
                door_id=door_no,
                card_no=None,
                gym_branch_id=gym_branch_id,
                porte_type=porte_type,
            )

            payload = json.loads(payload_json)
            kafka.produce("rt_" + ctx.tenant, payload)
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
