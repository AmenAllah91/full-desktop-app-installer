from enum import Enum, auto

class MachineType(Enum):
    C3 = auto()                      # Contrôleur ACP-260 / "C3" (Pull SDK)
    STANDALONE_OLD_FIRMWARE = auto()  # Appareil autonome – ancien firmware
    STANDALONE_NEW_FIRMWARE = auto()  # Appareil autonome – nouveau firmware (zkemkeeper)
    PUSH = auto()                    # Appareil PUSH/ADMS (SpeedFace-V3L, ProFace...)