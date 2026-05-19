import base64
import json
import logging
import sqlite3


app_version = "v1"
currentGymBranchId = None
tenant = None
gym_branch_id = None
DB_FILE = None
TEMP_DIR = None
UPLOAD_DIR = None

pointage_kafka = None
publish_photo_kafka = None
fingerprint_kafka = None

TOPIC_CONSUME_PUBLISH_PHOTO = "launch_publish_photo"
TOPIC_PRODUCE_PUBLISH_PHOTO = "finish_publish_photo"

machineService = None


def get_device_context(machine_id):
    from services.DeviceMAnager import DeviceManager
    if app_version == "v2":
        from services.v2.device_manager_v2 import DeviceManagerV2
        return DeviceManagerV2.get(machine_id)
    return DeviceManager.get(machine_id)


def get_all_device_contexts():
    from services.DeviceMAnager import DeviceManager
    if app_version == "v2":
        from services.v2.device_manager_v2 import DeviceManagerV2
        return DeviceManagerV2.all()
    return list(DeviceManager._registry.values())


def add_task_to_queue(task):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO task_queue (task_data, status) VALUES (?, 'PENDING')",
        (json.dumps(task),),
    )
    conn.commit()
    conn.close()


def process_user_photo(user_pin: str, target_gym_branch_id: str,
                       machine_id: int, ip: str = None, port: int = None):
    if target_gym_branch_id != currentGymBranchId:
        return None

    ctx = get_device_context(machine_id)
    if not ctx:
        logging.error("No context found for machine ID %s", machine_id)
        return None

    adapter = ctx.adapter
    with ctx.lock:
        photo_path = f"C:/temp/{user_pin}.jpg"
        success = adapter.download_user_photo(user_pin, photo_path)

        if success:
            with open(photo_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")

            payload = {"userPin": user_pin, "photo": encoded}
            publish_photo_kafka.produce(
                TOPIC_PRODUCE_PUBLISH_PHOTO, json.dumps(payload)
            )
            logging.info("✅ Published photo for user %s to Kafka.", user_pin)
            return payload
        else:
            logging.error("❌ Failed to download photo for user %s.", user_pin)
            return None
