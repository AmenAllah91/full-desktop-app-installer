import base64
import json
from confluent_kafka import Producer, Consumer, KafkaException, KafkaError
from confluent_kafka.admin import AdminClient, NewTopic
import time


class KafkaService:
    def __init__(self, kafka_broker: str, group_id: str, auto_offset_reset: str = 'earliest'):
        """Initialize Kafka producer and consumer with the provided configurations."""
        # Initialize Kafka Producer
        self.producer = Producer({'bootstrap.servers': kafka_broker})

        # Initialize Kafka Consumer
        self.consumer = Consumer({
            'bootstrap.servers': kafka_broker,
            'group.id': group_id,
            'auto.offset.reset': auto_offset_reset,
            'metadata.max.age.ms': '10000',
            'session.timeout.ms': 60000,  # Increase session timeout
            'max.poll.interval.ms': 300000
        })

        # Admin client for topic management
        self.admin = AdminClient({'bootstrap.servers': kafka_broker})
        self._topics_ensured = set()

    def ensure_topic(self, topic: str, num_partitions: int = 3, replication_factor: int = 1):
        """Create the topic if it doesn't exist on the broker."""
        if topic in self._topics_ensured:
            return True
        try:
            metadata = self.admin.list_topics(timeout=5)
            if topic in metadata.topics:
                self._topics_ensured.add(topic)
                return True

            future = self.admin.create_topics([
                NewTopic(topic, num_partitions=num_partitions, replication_factor=replication_factor)
            ])
            future[topic].result(timeout=5)
            print(f"✅ Kafka topic '{topic}' created")
            self._topics_ensured.add(topic)
            return True
        except Exception as e:
            err_str = str(e)
            if "TOPIC_ALREADY_EXISTS" in err_str or "already exists" in err_str:
                self._topics_ensured.add(topic)
                return True
            print(f"⚠️ Could not ensure topic '{topic}': {e}")
            return False

    def produce(self, topic: str, message):
        """Send a message to the specified Kafka topic."""
        self.ensure_topic(topic)

        # Convert the message to JSON string if it's a dictionary
        json_message = json.dumps(message) if isinstance(message, dict) else str(message)

        # Send the message asynchronously
        self.producer.produce(topic, value=json_message, callback=None)

        # Periodically poll to trigger delivery callbacks
        self.producer.poll(0)

    def consume(self, topic: str, on_message):
        """Listen to messages from the specified Kafka topic and process them using a callback."""
        self.ensure_topic(topic)
        self.consumer.subscribe([topic])
        print(f"Listening to Kafka topic '{topic}'...")

        try:
            while True:
                msg = self.consumer.poll(1.0)

                if msg is None:
                    continue  # No message received

                if msg.error():
                    code = msg.error().code()
                    if code == KafkaError._PARTITION_EOF:
                        continue
                    if code == 3:  # UNKNOWN_TOPIC_OR_PART
                        print(f"⏳ Topic '{topic}' not available yet, waiting 5s...")
                        time.sleep(5)
                        continue
                    print(f"Error: {msg.error()}")
                    time.sleep(1)
                    continue

                # Decode the message and handle JSON or other formats
                try:
                    message_value = msg.value().decode('utf-8')
                    on_message(message_value)

                except UnicodeDecodeError:
                    print("Non-UTF-8 message received:", msg.value())
                    on_message(msg.value())  # Raw binary handling

        finally:
            self.consumer.close()

    def close(self):
        """Close the Kafka consumer and release resources."""
        self.consumer.close()
