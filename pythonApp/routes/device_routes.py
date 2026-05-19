import json
import logging
import sqlite3

from flask import Blueprint, jsonify, request

import app_context as ctx

device_bp = Blueprint("devices", __name__)


@device_bp.route('/api/devices', methods=['GET'])
def get_devices():
    devices = []
    for dctx in ctx.get_all_device_contexts():
        adapter = dctx.adapter
        info = {
            "id": dctx.machine.id,
            "alias": dctx.machine.alias,
            "ip": dctx.machine.addresseip,
            "port": dctx.machine.port,
            "original_type": dctx.machine.type,
            "mode": "PUSH/ADMS" if ctx.app_version == "v2" else dctx.machine.type,
        }
        if ctx.app_version == "v2":
            info["connected"] = adapter.is_connected()
            info["sn"] = adapter.sn
            info["last_seen"] = adapter.last_seen.isoformat() if adapter.last_seen else None
        else:
            info["connected"] = getattr(adapter, "connected", None)
        devices.append(info)
    return jsonify({"version": ctx.app_version, "devices": devices}), 200


@device_bp.route('/adms/status', methods=['GET'])
def adms_status():
    try:
        from services.adms_server import ADMSServer
        server = ADMSServer()
        return jsonify(server.get_connected_devices()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@device_bp.route('/api/test/add_user', methods=['POST'])
def test_add_user():
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

        dctx = ctx.get_device_context(machine_id)
        if not dctx:
            return jsonify({"error": f"Machine {machine_id} non trouvée"}), 404

        adapter = dctx.adapter

        connected = adapter.is_connected() if hasattr(adapter, 'is_connected') else getattr(adapter, 'connected', False)
        if not connected:
            return jsonify({
                "error": "Machine non connectée",
                "machine": dctx.machine.alias,
                "ip": dctx.machine.addresseip,
                "hint": "Vérifiez que la machine est en mode PUSH et pointe vers ce serveur sur le port 8088"
            }), 503

        ok = adapter.add_user(pin, name, card_no, start_date, end_date)

        result = {
            "operation": "ADD_USER",
            "success": ok,
            "machine": dctx.machine.alias,
            "ip": dctx.machine.addresseip,
            "pin": pin,
            "name": name,
        }

        if ok:
            ok_auth = adapter.authorize_user(pin)
            result["authorize_success"] = ok_auth

        return jsonify(result), 200 if ok else 500

    except Exception as ex:
        logging.exception("Erreur test add_user: %s", ex)
        return jsonify({"error": str(ex)}), 500


@device_bp.route('/api/test/delete_user', methods=['POST'])
def test_delete_user():
    try:
        data = request.get_json(force=True)
        machine_id = data.get("machineId")
        pin = str(data.get("pin", ""))

        dctx = ctx.get_device_context(machine_id)
        if not dctx:
            return jsonify({"error": f"Machine {machine_id} non trouvée"}), 404

        adapter = dctx.adapter
        ok = adapter.delete_user(pin)
        return jsonify({"operation": "DELETE_USER", "success": ok, "pin": pin}), 200 if ok else 500

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@device_bp.route('/api/test/open_door', methods=['POST'])
def test_open_door():
    try:
        data = request.get_json(force=True)
        machine_id = data.get("machineId")
        duration = int(data.get("duration", 5))

        dctx = ctx.get_device_context(machine_id)
        if not dctx:
            return jsonify({"error": f"Machine {machine_id} non trouvée"}), 404

        adapter = dctx.adapter

        if ctx.app_version == "v2":
            ok = adapter.open_door(door_no=1, duration=duration)
        elif hasattr(adapter, 'open_door'):
            ok = adapter.open_door(duration_seconds=duration) if hasattr(adapter, 'ACUnlock') else adapter.open_door(door_no=1, duration=duration)
        else:
            ok = False

        return jsonify({"operation": "OPEN_DOOR", "success": ok, "duration": duration}), 200 if ok else 500

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@device_bp.route('/api/test/queue', methods=['GET'])
def test_queue_status():
    try:
        conn = sqlite3.connect(ctx.DB_FILE)
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

        adapter_queues = {}
        for dctx in ctx.get_all_device_contexts():
            adapter = dctx.adapter
            queue_len = len(getattr(adapter, '_command_queue', []))
            adapter_queues[dctx.machine.alias] = {
                "machine_id": dctx.machine.id,
                "connected": adapter.is_connected() if hasattr(adapter, 'is_connected') else None,
                "sn": getattr(adapter, 'sn', None),
                "commands_pending": queue_len,
            }

        return jsonify({
            "version": ctx.app_version,
            "sqlite_tasks": tasks,
            "adapter_queues": adapter_queues
        }), 200

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500
