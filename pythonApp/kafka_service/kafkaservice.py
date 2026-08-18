import base64
import json
import logging
from confluent_kafka import Producer, Consumer, KafkaException, KafkaError
from confluent_kafka.admin import AdminClient, NewTopic
import time

logger = logging.getLogger(__name__)

class KafkaService:
    def __init__(self, kafka_broker: str, group_id: str, auto_offset_reset: str = 'earliest'):
        """Initialize Kafka producer and consumer with the provided configurations.

        auto_offset_reset ne s'applique QUE lorsqu'aucun offset n'est commité pour
        ce group.id (premier démarrage). Ensuite le consommateur reprend toujours
        à son dernier offset commité : un poste éteint quelques heures rattrape
        bien les messages manqués à son redémarrage.
        """
        # Initialize Kafka Producer
        self.producer = Producer({'bootstrap.servers': kafka_broker})

        # Le Consumer n'est PLUS créé ici.
        #
        # Il l'était, et consume() le refermait dans son finally. L'appelant
        # rattrapait l'exception, attendait 1 s, puis rappelait consume() sur le
        # MÊME objet fermé : confluent_kafka lève alors « RuntimeError: Consumer
        # closed » sur subscribe() comme sur poll(), indéfiniment. Une seule
        # erreur suffisait donc à tuer le canal give access jusqu'au redémarrage
        # de l'application — le process restait vivant, /health répondait ok, et
        # plus aucun accès ne descendait vers les pointeuses.
        #
        # On conserve la configuration et on fabrique un consumer neuf à chaque
        # entrée dans consume().
        self._consumer_config = {
            'bootstrap.servers': kafka_broker,
            'group.id': group_id,
            'auto.offset.reset': auto_offset_reset,
            'metadata.max.age.ms': '10000',
            'session.timeout.ms': 60000,  # Increase session timeout
            'max.poll.interval.ms': 300000
        }
        self.consumer = None
        self._admin = AdminClient({'bootstrap.servers': kafka_broker})
        self._broker = kafka_broker

    def ensure_topic(self, topic: str, num_partitions: int = 1, replication_factor: int = 1):
        """Create the topic if it does not already exist on the broker."""
        try:
            metadata = self._admin.list_topics(timeout=5)
            if topic in metadata.topics:
                return
            futures = self._admin.create_topics([
                NewTopic(topic, num_partitions=num_partitions, replication_factor=replication_factor)
            ])
            futures[topic].result(timeout=10)
            logger.info("Topic '%s' created successfully", topic)
        except Exception as e:
            if "TOPIC_ALREADY_EXISTS" in str(e):
                return
            logger.warning("Could not create topic '%s': %s", topic, e)

    def produce(self, topic: str, message):
        """Send a message to the specified Kafka topic."""
        json_message = json.dumps(message) if isinstance(message, dict) else str(message)

        def delivery_report(err, msg):
            if err is not None:
                logger.error("Kafka delivery failed to %s: %s", topic, err)

        try:
            self.producer.produce(topic, value=json_message, callback=delivery_report)
            self.producer.poll(0)
        except Exception as e:
            logger.error("Kafka produce exception for topic %s: %s", topic, e)

    def consume(self, topic: str, on_message):
        """Listen to messages from the specified Kafka topic and process them using a callback."""
        # Consumer neuf à chaque appel : l'appelant relance consume() en boucle
        # après une erreur, et réutiliser un objet fermé est fatal (voir __init__).
        consumer = Consumer(self._consumer_config)
        self.consumer = consumer
        consumer.subscribe([topic])
        logger.info("Listening to Kafka topic '%s'...", topic)

        try:
            while True:
                msg = consumer.poll(1.0)

                if msg is None:
                    continue  # No message received

                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    else:
                        logger.error("Kafka error: %s", msg.error())
                        time.sleep(1)
                        continue

                try:
                    message_value = msg.value().decode('utf-8')
                except UnicodeDecodeError:
                    logger.warning("Non-UTF-8 message received: %s", msg.value())
                    message_value = msg.value()

                # Le traitement d'UN message ne doit jamais interrompre l'écoute :
                # une exception remontait jusqu'ici, fermait le consumer et coupait
                # le canal. Tous les handlers ne sont pas également protégés
                # (process_fingerprint_actions n'a aucun try/except), donc le filet
                # est posé ici, une fois pour toutes.
                try:
                    on_message(message_value)
                except Exception as exc:
                    apercu = str(message_value)[:300]
                    logger.error("Traitement du message KO sur '%s' : %s — message ignoré, "
                                 "écoute maintenue. Payload: %s", topic, exc, apercu,
                                 exc_info=True)

        finally:
            self.consumer = None
            try:
                consumer.close()
            except Exception:
                pass

    def close(self):
        """Close the Kafka consumer and release resources."""
        consumer, self.consumer = self.consumer, None
        if consumer is None:
            return
        try:
            consumer.close()
        except Exception:
            pass
