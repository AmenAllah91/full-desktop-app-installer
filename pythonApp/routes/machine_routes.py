from flask import Blueprint, jsonify

from services.machinestatus import get_status, get_all_statuses

machine_bp = Blueprint("machines", __name__)


@machine_bp.route('/api/machines/status', methods=['GET'])
def machines_status():
    try:
        statuses = get_all_statuses()
        return jsonify({
            "count": len(statuses),
            "machines": [s.snapshot() for s in statuses.values()]
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@machine_bp.route('/api/machines/<int:machine_id>/status', methods=['GET'])
def machine_status_by_id(machine_id):
    status = get_status(machine_id)
    if status is None:
        return jsonify({"error": f"Machine {machine_id} not found in status registry"}), 404
    return jsonify(status.snapshot()), 200


@machine_bp.route('/api/machines/<int:machine_id>/reconnect', methods=['POST'])
def reconnect_machine(machine_id):
    status = get_status(machine_id)
    if status is None:
        return jsonify({"error": f"Machine {machine_id} not found in status registry"}), 404

    if status.connected:
        return jsonify({
            "status": "already_connected",
            "machine": status.snapshot()
        }), 200

    status.request_reconnect()
    return jsonify({
        "status": "reconnect_requested",
        "message": "Monitor thread will retry connection immediately",
        "machine": status.snapshot()
    }), 202
