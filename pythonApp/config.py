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
#
# `YOGYM_BASE_URL` est l'ancien nom de la clé, d'avant le renommage de la marque.
# Un poste déjà installé garde son .env tant qu'il n'est pas réinstallé : ne lire
# que le nouveau nom le renverrait en silence sur l'URL par défaut. Electron fait
# la même lecture en cascade dans readBaseUrl(). À retirer quand tous les postes
# auront été réinstallés.
YOCLUB_BASE_URL = (
    os.getenv('YOCLUB_BASE_URL')
    or os.getenv('YOGYM_BASE_URL')
    or 'https://account.yo-club.app'
).rstrip('/')

