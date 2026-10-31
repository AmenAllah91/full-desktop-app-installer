import json
import logging
import os
import threading
import time

import requests
from confluent_kafka import Consumer, Producer, TopicPartition, OFFSET_END
from confluent_kafka.admin import AdminClient, NewTopic

BRIDGE_VERSION = "remote-1"
OPEN_DOOR = "OPEN_DOOR"
CAPABILITIES = [OPEN_DOOR]
COMMAND_TOPIC_PREFIX = "device_command_"
RESULT_TOPIC = "device_result"
PRESENCE_TOPIC = "bridge_presence"
PRESENCE_INTERVAL = 10
CHECK_TIMEOUT = 2
DETAIL_MAX = 200
UNLOCK_DEFAULT = 5
UNLOCK_MAX = 10
SESSION_POLL = 0.2
TCP_PROBE = 1.5

OPENED = "OPENED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"

logger = logging.getLogger("remote_commands")


def enabled(env=None):
    value = (env if env is not None else os.environ).get("REMOTE_COMMANDS_ENABLED", "")
    return str(value).strip().lower() in ("1", "true", "yes", "oui")


def start(tenant, gym_branch_id, kafka_broker, pointage_topic, base_url):
    if not enabled():
        logger.info("Commandes à distance désactivées (REMOTE_COMMANDS_ENABLED absent)")
        return None
    if not (tenant and gym_branch_id and kafka_broker and base_url):
        logger.warning("Commandes à distance non démarrées : identité ou adresses manquantes")
        return None
    runner = RemoteCommands(tenant, gym_branch_id, kafka_broker, pointage_topic, base_url)
    runner.start()
    logger.info("Commandes à distance démarrées (%s, filiale %s)", tenant, gym_branch_id)
    return runner


def _device_context(machine_id):
    from services.DeviceMAnager import DeviceManager
    return DeviceManager.get(machine_id)


def _device_contexts():
    from services.DeviceMAnager import DeviceManager
    return list(DeviceManager.all())


def _tcp_reachable(machine, timeout):
    from services.common import tcp_reachable
    return tcp_reachable(machine.addresseip, machine.port, timeout=timeout)


def _zk_sdk_lock():
    from services.common import zk_sdk_lock
    return zk_sdk_lock


def _com_initialize():
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception as exc:
        logger.debug("CoInitialize ignoré sur ce thread : %s", exc)


def _publish_passage(ctx, door, pin, porte_type, pointage_topic, tenant, gym_branch_id):
    from datetime import datetime
    from services.MachineMonitor import make_rt_json, kafka
    from services.websocket import send_pointage
    payload = json.loads(make_rt_json(
        machine_id=ctx.machine.id,
        ip=ctx.machine.addresseip,
        mtype=ctx.machine.type,
        pin=int(pin),
        state_code=0,
        dt=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        door_id=door,
        card_no=None,
        gym_branch_id=str(gym_branch_id),
        porte_type=porte_type,
        tenant=tenant,
    ))
    kafka.produce(pointage_topic, payload)
    send_pointage(payload, str(gym_branch_id))


def _now_ms():
    return int(time.time() * 1000)


def _porte_type(machine, door):
    if door is None:
        return getattr(machine, "porte_type", None) or ""
    flag = getattr(machine, f"door{door}", 1)
    return "ENTREE" if flag == 1 else "SORTIE"


def _unlock_seconds(value):
    if isinstance(value, bool):
        return UNLOCK_DEFAULT
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return UNLOCK_DEFAULT
    return max(1, min(seconds, UNLOCK_MAX))


def _shorten(value):
    text = str(value) if value is not None else None
    return text[:DETAIL_MAX] if text else text


class RemoteCommands:

    def __init__(self, tenant, gym_branch_id, kafka_broker, pointage_topic, base_url,
                 producer=None, consumer_factory=None, http_get=None, now_ms=None,
                 device_context=None, device_contexts=None, publish_passage=None,
                 reachable=None, sdk_lock=None, sleep=None):
        self.tenant = tenant
        self.gym_branch_id = str(gym_branch_id)
        self.kafka_broker = kafka_broker
        self.pointage_topic = pointage_topic
        self.base_url = base_url.rstrip("/")
        self.command_topic = f"{COMMAND_TOPIC_PREFIX}{tenant}"
        self._producer = producer
        self._consumer_factory = consumer_factory or self._new_consumer
        self._http_get = http_get or requests.get
        self._now_ms = now_ms or _now_ms
        self._device_context = device_context or _device_context
        self._device_contexts = device_contexts or _device_contexts
        self._publish_passage = publish_passage or _publish_passage
        self._reachable = reachable or _tcp_reachable
        self._sdk_lock = sdk_lock
        self._sleep = sleep or time.sleep
        self._stop = threading.Event()

    def start(self):
        if self._producer is None:
            self._producer = Producer({"bootstrap.servers": self.kafka_broker})
        threading.Thread(target=self._consume_loop, daemon=True, name="RemoteCommands").start()
        threading.Thread(target=self._presence_loop, daemon=True, name="RemotePresence").start()

    def stop(self):
        self._stop.set()

    def handle(self, message):
        if not isinstance(message, dict) or str(message.get("gymBranchId")) != self.gym_branch_id:
            return None
        command_id = message.get("commandId")
        if not command_id:
            return None
        received = self._now_ms()
        deadline = message.get("deadlinePcMs")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or received > deadline:
            return self._answer(message, EXPIRED, "Reçue après l'échéance", received)
        if message.get("type") != OPEN_DOOR:
            return self._answer(message, REJECTED, "Type de commande inconnu", received)
        ctx = self._device_context(message.get("machineId"))
        if ctx is None:
            return self._answer(message, REJECTED, "Pointeuse inconnue de ce poste", received)
        kind = type(ctx.adapter).__name__
        if kind not in ("PlcommAdapter", "ZkemAdapter"):
            return self._answer(message, REJECTED, f"Pointeuse non prise en charge : {kind}", received)
        door = message.get("door")
        if kind == "PlcommAdapter" and (isinstance(door, bool) or door not in (1, 2, 3, 4)):
            return self._answer(message, REJECTED, "Porte invalide", received)
        unlock = _unlock_seconds(message.get("unlockSeconds"))
        if not self._confirmed(message):
            return self._answer(message, REJECTED, "Commande non confirmée par le serveur", received)
        if self._remaining(deadline) <= 0:
            return self._answer(message, EXPIRED, "Échéance dépassée avant l'ouverture", received)
        status, detail = self._open(ctx, kind, door, unlock, deadline, command_id)
        if status == OPENED:
            pin = message.get("memberPin")
            if pin:
                try:
                    self._publish_passage(ctx, door if kind == "PlcommAdapter" else 1, pin,
                                          _porte_type(ctx.machine, door if kind == "PlcommAdapter" else None),
                                          self.pointage_topic, self.tenant, self.gym_branch_id)
                except Exception:
                    logger.exception("Passage non publié après l'ouverture %s", command_id)
        return self._answer(message, status, detail, received)

    def presence(self):
        machines = []
        for ctx in self._device_contexts():
            connected = getattr(ctx.adapter, "connected", None)
            machines.append({"id": ctx.machine.id, "connected": bool(connected) if connected is not None else None})
        return {
            "tenant": self.tenant,
            "gymBranchId": int(self.gym_branch_id),
            "bridgeVersion": BRIDGE_VERSION,
            "capabilities": CAPABILITIES,
            "sentAtPcMs": self._now_ms(),
            "machines": machines,
        }

    def _remaining(self, deadline):
        return (deadline - self._now_ms()) / 1000.0

    def _open(self, ctx, kind, door, unlock, deadline, command_id):
        refusal = self._hold(ctx, kind, deadline)
        if refusal:
            return refusal
        sdk_lock = None
        try:
            if kind == "PlcommAdapter":
                ok = ctx.adapter.open_door(door_no=door, duration=unlock)
            else:
                if not self._reachable(ctx.machine, max(0.1, min(TCP_PROBE, self._remaining(deadline)))):
                    return FAILED, "Pointeuse injoignable"
                lock = self._sdk_lock or _zk_sdk_lock()
                if not lock.acquire(timeout=max(0.0, self._remaining(deadline))):
                    return EXPIRED, "SDK des pointeuses occupé jusqu'à l'échéance"
                sdk_lock = lock
                ok = ctx.adapter.open_door(duration_seconds=unlock)
        except Exception as ex:
            logger.exception("Ouverture à distance en échec (%s)", command_id)
            return FAILED, ex
        finally:
            if sdk_lock is not None:
                sdk_lock.release()
            ctx.lock.release()
        if not ok:
            return FAILED, "La pointeuse a refusé l'ouverture"
        late = self._now_ms() - deadline
        return OPENED, (f"Ouverte {int(late)} ms après l'échéance" if late > 0 else None)

    def _hold(self, ctx, kind, deadline):
        waited_for_session = False
        while True:
            remaining = self._remaining(deadline)
            if remaining <= 0 or not ctx.lock.acquire(timeout=remaining):
                if waited_for_session:
                    return FAILED, "Pointeuse C3 sans session jusqu'à l'échéance"
                return EXPIRED, "Pointeuse occupée jusqu'à l'échéance"
            if kind != "PlcommAdapter" or getattr(ctx.adapter, "handle", None):
                return None
            ctx.lock.release()
            waited_for_session = True
            self._sleep(max(0.0, min(SESSION_POLL, self._remaining(deadline))))

    def _confirmed(self, message):
        command_id = message.get("commandId")
        url = (f"{self.base_url}/gym-management/public/device/commands/"
               f"{self.tenant}/{command_id}/check")
        params = {"gymBranchId": self.gym_branch_id, "machineId": message.get("machineId")}
        if message.get("door") is not None:
            params["door"] = message.get("door")
        if message.get("memberPin"):
            params["memberPin"] = message.get("memberPin")
        try:
            response = self._http_get(url, params=params, timeout=CHECK_TIMEOUT)
            return response.status_code == 204
        except Exception as ex:
            logger.warning("Vérification de la commande %s impossible : %s", command_id, ex)
            return False

    def _answer(self, message, status, detail, received):
        result = {
            "commandId": message.get("commandId"),
            "tenant": self.tenant,
            "gymBranchId": int(self.gym_branch_id),
            "machineId": message.get("machineId"),
            "status": status,
            "detail": _shorten(detail),
            "receivedPcMs": received,
            "doneAtPcMs": self._now_ms(),
            "bridgeVersion": BRIDGE_VERSION,
        }
        logger.info("Commande %s : %s%s", result["commandId"], status,
                    f" ({result['detail']})" if result["detail"] else "")
        self._send(RESULT_TOPIC, result)
        return status

    def _send(self, topic, payload):
        try:
            self._producer.produce(topic, json.dumps(payload).encode("utf-8"))
            self._producer.poll(0)
            self._producer.flush(2)
        except Exception as ex:
            logger.warning("Envoi sur %s impossible : %s", topic, ex)

    def _new_consumer(self):
        return Consumer({
            "bootstrap.servers": self.kafka_broker,
            "group.id": f"{self.command_topic}_{self.gym_branch_id}",
            "enable.auto.commit": False,
            "auto.offset.reset": "latest",
        })

    def _ensure_topic(self):
        try:
            admin = AdminClient({"bootstrap.servers": self.kafka_broker})
            if self.command_topic in admin.list_topics(timeout=5).topics:
                return
            admin.create_topics([NewTopic(self.command_topic, num_partitions=1, replication_factor=1)])[
                self.command_topic].result(timeout=10)
        except Exception as ex:
            if "TOPIC_ALREADY_EXISTS" not in str(ex):
                logger.warning("Topic %s non créé : %s", self.command_topic, ex)

    def _consume_loop(self):
        _com_initialize()
        while not self._stop.is_set():
            consumer = None
            try:
                self._ensure_topic()
                consumer = self._consumer_factory()
                metadata = consumer.list_topics(self.command_topic, timeout=5).topics.get(self.command_topic)
                partitions = sorted(metadata.partitions) if metadata and metadata.partitions else [0]
                consumer.assign([TopicPartition(self.command_topic, p, OFFSET_END) for p in partitions])
                while not self._stop.is_set():
                    record = consumer.poll(0.1)
                    if record is None:
                        continue
                    if record.error():
                        logger.warning("Lecture des commandes : %s", record.error())
                        continue
                    try:
                        message = json.loads(record.value())
                    except Exception:
                        logger.warning("Commande illisible ignorée")
                        continue
                    self.handle(message)
            except Exception:
                logger.exception("Lecteur des commandes à distance interrompu, reprise dans 5 s")
                self._stop.wait(5)
            finally:
                if consumer is not None:
                    try:
                        consumer.close()
                    except Exception:
                        pass

    def _presence_loop(self):
        while not self._stop.is_set():
            try:
                self._send(PRESENCE_TOPIC, self.presence())
            except Exception:
                logger.exception("Signal de présence non envoyé")
            self._stop.wait(PRESENCE_INTERVAL)
