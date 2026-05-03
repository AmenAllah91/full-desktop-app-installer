from flask import Blueprint, jsonify

import app_context as ctx

config_bp = Blueprint("config", __name__)


@config_bp.route('/curentconf', methods=['GET'])
def curentconf():
    try:
        payload = {
            "gymbranchId": ctx.currentGymBranchId,
            "tenant": ctx.tenant,
        }
        return jsonify(payload), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@config_bp.route('/config/gymBranchId', methods=['GET'])
def get_gym_branch_id():
    return {"gymBranchId": ctx.currentGymBranchId}


@config_bp.route('/api/version', methods=['GET'])
def get_version():
    return jsonify({"version": ctx.app_version}), 200
