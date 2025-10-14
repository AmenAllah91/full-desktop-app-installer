from enum import Enum

class DoorType(Enum):
    ENTREE = "ENTREE"
    SORTIE = "SORTIE"

    def __str__(self):
        return self.name
