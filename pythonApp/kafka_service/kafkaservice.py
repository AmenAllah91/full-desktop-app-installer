import base64
import json
from confluent_kafka import Producer, Consumer, KafkaException, KafkaError
import time
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
        print(f"Listening to Kafka topic '{topic}'...")

        try:
            while True:
                msg = self.consumer.poll(1.0)

                if msg is None:
                    continue  # No message received

                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    else:
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
