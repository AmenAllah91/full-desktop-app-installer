import os
from dotenv import load_dotenv

# Load environment variables from .env file (if applicable)
load_dotenv()

KAFKA_BROKER = os.getenv('KAFKA_BROKER', '51.178.55.238:9094')
KAFKA_TOPIC = os.getenv('KAFKA_TOPIC', 'rt_pointage')
KAFKA_GROUP_ID = os.getenv('KAFKA_GROUP_ID', 'group_c')

# Racine unique de la plateforme. C'est LA seule valeur à changer pour basculer
# tout le poste vers un autre environnement (https://integration.yo-club.app par
# exemple) : les URLs des services en sont dérivées, côté Python comme côté
# Electron, qui lit le même fichier .env.
YOGYM_BASE_URL = os.getenv('YOGYM_BASE_URL', 'https://account.yo-club.app').rstrip('/')

