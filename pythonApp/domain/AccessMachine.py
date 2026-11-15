from dataclasses import dataclass, field

from domain.DoorType import DoorType


@dataclass
class AccessMachine:
    id: int
    alias: str
    addresseip: str
    port: int
    statut: str
    type: str
    door1: int
    door2: int
    door3: int
    door4: int
    porte_type:DoorType
    comKey: int = field(default=0, repr=False)
    currentHandle: int = -1
