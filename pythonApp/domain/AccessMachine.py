from dataclasses import dataclass

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
    currentHandle: int = -1
