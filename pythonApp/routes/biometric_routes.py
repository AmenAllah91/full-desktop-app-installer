import base64
import logging
import time

from flask import Blueprint, jsonify, request, abort
from pathlib import Path
from werkzeug.utils import secure_filename

import app_context as ctx
from domain import Operation
from services.captureFingerPrint import FingerprintCapture

biometric_bp = Blueprint("biometric", __name__)


@biometric_bp.route('/getFace/<int:user_pin>/<int:gym_branch_id>/<int:machine_id>', methods=['GET'])
def capture_fingerprint_api(user_pin, gym_branch_id, machine_id):
    try:
        payload = ctx.process_user_photo(str(user_pin), str(gym_branch_id), machine_id)
        if payload:
            return jsonify(payload), 200
        else:
            return jsonify({"error": "Photo not found or machine not valid"}), 400
    except Exception as e:
        logging.error("❌ Error in capture_fingerprint_api: %s", e)
        return jsonify({"error": str(e)}), 500


@biometric_bp.route('/face/upload', methods=['POST'])
def upload_face_multipart():
    pin = request.form.get('pin')
    gym_branchId = request.form.get('gymBranchId')
    file = request.files.get('photo')

    if not all([pin, gym_branchId, file]):
        abort(400, "pin, gymBranchId et fichier photo sont requis")

    if str(gym_branchId) != str(ctx.currentGymBranchId):
        abort(400, "gymBranchId ne correspond pas à cette instance")

    upload_dir = Path(ctx.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    dst_name = f"verify_biophoto_9_{pin}.jpg"
    dst_path = upload_dir / secure_filename(dst_name)
    file.save(dst_path)
    logging.info("📥 Photo enregistrée : %s", dst_path)

    report = {}
    MAX_UPLOAD_RETRIES = 3
    RETRY_DELAY = 1

    for dctx in ctx.get_all_device_contexts():
        if str(dctx.gym_branch_id if ctx.app_version == "v2" else dctx.gymBranchId) != str(ctx.currentGymBranchId):
            continue

        adapter = dctx.adapter
        ok = False

        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                ok = adapter.upload_user_photo(pin, str(dst_path))
                if ok:
                    break
                else:
                    logging.warning(
                        "⏱️ Tentative %s/%s échouée pour %s",
                        attempt, MAX_UPLOAD_RETRIES, dctx.machine.addresseip,
                    )
            except Exception as ex:
                logging.exception(
                    "❌ Exception lors de l'upload vers %s (essai %s) : %s",
                    dctx.machine.addresseip, attempt, ex,
                )

            time.sleep(RETRY_DELAY)

        report[dctx.machine.addresseip] = "OK" if ok else "KO"

    return jsonify({"pin": pin, "result": report}), 200


@biometric_bp.route('/fingerprint/upload', methods=['POST'])
def upload_fingerprint():
    try:
        data = request.get_json()
        if not data or "pin" not in data:
            return jsonify({"error": "Missing 'pin' in request"}), 400

        pin = data["pin"]
        gym_branch_id = data.get("gymBranchId", ctx.currentGymBranchId)
        finger_id = data.get("fingerId")

        if str(gym_branch_id) != str(ctx.currentGymBranchId):
            return jsonify({"error": "gymBranchId mismatch"}), 400

        logging.info("Starting fingerprint upload for PIN %s", pin)

        capture = FingerprintCapture()
        template_bytes, images = capture.capture_fingerprint(save_file=False)

        if not template_bytes:
            return jsonify({"error": "Failed to capture fingerprint"}), 500

        logging.info("Fingerprint captured successfully. Template size: %s bytes", len(template_bytes))

        results = {}
        success_count = 0
        total_count = 0

        for dctx in ctx.get_all_device_contexts():
            machine_id = dctx.machine.id
            branch_id = dctx.gym_branch_id if ctx.app_version == "v2" else dctx.gymBranchId
            if str(branch_id) != str(ctx.currentGymBranchId):
                continue

            total_count += 1
            adapter = dctx.adapter
            machine_ip = dctx.machine.addresseip

            try:
                logging.info("Uploading to machine %s (ID: %s)", machine_ip, machine_id)
                with dctx.lock:
                    success = adapter.add_fingerprint(
                        user_id=pin,
                        fingerprint_template=template_bytes,
                        finger_id=finger_id,
                    )

                if success:
                    results[machine_ip] = "SUCCESS"
                    success_count += 1
                    logging.info("Fingerprint uploaded successfully to %s", machine_ip)
                else:
                    results[machine_ip] = "FAILED"
                    logging.error("Failed to upload fingerprint to %s", machine_ip)
            except Exception as e:
                results[machine_ip] = f"ERROR: {str(e)}"
                logging.error("Exception uploading to %s: %s", machine_ip, e)

        python_bytes = bytes(bytearray(template_bytes))
        encoded_template = base64.b64encode(python_bytes).decode('utf-8')
        payload = {
            "fingerprint_template": encoded_template,
            "pin": pin,
            "gymBranchId": gym_branch_id,
            "operation": Operation.ADD_FINGERPRINT.value,
            "finger_id": finger_id,
        }

        if success_count == 0:
            return jsonify({
                "error": f"Failed to upload fingerprint to all {total_count} machines",
                "results": results
            }), 500
        elif success_count < total_count:
            ctx.fingerprint_kafka.produce("fingerprint_actions_" + ctx.tenant, payload)
            return jsonify({
                "warning": f"Partial success: {success_count}/{total_count} machines",
                "results": results,
                "pin": pin,
                "fingerprint_template": encoded_template,
            }), 207
        else:
            ctx.fingerprint_kafka.produce("fingerprint_actions_" + ctx.tenant, payload)
            return jsonify({
                "message": f"Fingerprint uploaded successfully to all {total_count} machines",
                "results": results,
                "pin": pin,
                "fingerprint_template": encoded_template,
            }), 200

    except Exception as e:
        logging.exception("Fatal error in fingerprint upload: %s", e)
        return jsonify({"error": f"Exception during fingerprint upload: {str(e)}"}), 500


@biometric_bp.route('/getFingerprints/<int:user_pin>/<int:gym_branch_id>/<int:machine_id>', methods=['GET'])
def get_fingerprints_api(user_pin, gym_branch_id, machine_id):
    try:
        if int(gym_branch_id) != int(ctx.currentGymBranchId):
            return jsonify({"error": "Machine non valide pour cette gymBranchId"}), 400

        dctx = ctx.get_device_context(machine_id)
        if not dctx:
            try:
                from services.machinesService import AccessMachineService
                from services.DeviceMAnager import DeviceManager

                ms = AccessMachineService()
                machines = ms.get_access_machines(str(gym_branch_id), str(ctx.tenant))
                for m in machines:
                    if ctx.app_version == "v2":
                        from services.v2.device_manager_v2 import DeviceManagerV2
                        DeviceManagerV2.register(m, ctx.tenant, gym_branch_id)
                    else:
                        DeviceManager.register(m, ctx.tenant, gym_branch_id)
                dctx = ctx.get_device_context(machine_id)
            except Exception as e:
                logging.error("❌ fallback register machines failed: %s", e)

        if not dctx:
            return jsonify({"error": f"Machine ID {machine_id} introuvable côté service"}), 404

        adapter = dctx.adapter

        with dctx.lock:
            fps = adapter.get_fingerprints(str(user_pin)) or []

        payload = {
            "userPin": str(user_pin),
            "gymBranchId": int(gym_branch_id),
            "machineId": int(machine_id),
            "machineType": getattr(dctx.machine, "type", None),
            "fingerprints": fps,
        }
        return jsonify(payload), 200

    except Exception as e:
        logging.error("❌ Error in get_fingerprints_api: %s", e)
        return jsonify({"error": str(e)}), 500
