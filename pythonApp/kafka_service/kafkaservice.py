import base64
import json
import logging
from confluent_kafka import Producer, Consumer, KafkaException, KafkaError
from confluent_kafka.admin import AdminClient, NewTopic
import time

logger = logging.getLogger(__name__)

class KafkaService:
    def __init__(self, kafka_broker: str, group_id: str):
        """Initialize Kafka producer and consumer with the provided configurations."""
        # Initialize Kafka Producer
        self.producer = Producer({'bootstrap.servers': kafka_broker})

        # Initialize Kafka Consumer
        self.consumer = Consumer({
            'bootstrap.servers': kafka_broker,
            'group.id': group_id,
            'auto.offset.reset': 'earliest',
            'metadata.max.age.ms': '10000',
            'session.timeout.ms': 60000,  # Increase session timeout
            'max.poll.interval.ms': 300000
        })
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
        # def delivery_report(err, msg):
        #     if err is not None:
        #         print(f"Message delivery failed: {err}")
        #     else:
        #         print(f"Message delivered to {msg.topic()} [{msg.partition()}]")

        # Convert the message to JSON string if it's a dictionary and the topic is not 'rt_fingerprint_capture'
        json_message = json.dumps(message) if isinstance(message, dict) else str(message)

        # Send the message asynchronously
        self.producer.produce(topic, value=json_message, callback=None)

        # Periodically poll to trigger delivery callbacks
        self.producer.poll(0)

    def consume(self, topic: str, on_message):
        """Listen to messages from the specified Kafka topic and process them using a callback."""
        self.consumer.subscribe([topic])
        logger.info("Listening to Kafka topic '%s'...", topic)

        try:
            while True:
                msg = self.consumer.poll(1.0)

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
                    on_message(message_value)

                except UnicodeDecodeError:
                    logger.warning("Non-UTF-8 message received: %s", msg.value())
                    on_message(msg.value())

        finally:

            self.consumer.close()

    def close(self):
        """Close the Kafka consumer and release resources."""
        self.consumer.close()
