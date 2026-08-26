from enum import Enum

class Operation(Enum):
    ADD_USER = "ADD_USER"
    AUTHORIZE_USER = "AUTHORIZE_USER"
    DELETE_USER = "DELETE_USER"
    UNAUTHORIZE_USER = "UNAUTHORIZE_USER"
    ADD_FINGERPRINT = "ADD_FINGERPRINT"
    REMOVE_FINGERPRINT = "REMOVE_FINGERPRINT"
    # Reecriture d'un calendrier, sans utilisateur : emise quand un gerant
    # modifie les horaires d'une timezone cote cloud.
    UPDATE_TIMEZONE = "UPDATE_TIMEZONE"
