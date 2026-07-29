import os
from dotenv import load_dotenv

# Load environment variables from .env file (if applicable)
load_dotenv()

KAFKA_BROKER = os.getenv('KAFKA_BROKER', '51.178.55.238:9094')
KAFKA_TOPIC = os.getenv('KAFKA_TOPIC', 'rt_pointage')
KAFKA_GROUP_ID = os.getenv('KAFKA_GROUP_ID', 'group_c')

