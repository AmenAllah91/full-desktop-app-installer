# services/DeviceManager.py
from threading import RLock

from services.adapters import PlcommAdapter, MachineType
from services.zkem_adapter import ZkemAdapter


class DeviceContext:
    def __init__(self, machine,tenant, gym_branch_id):
        self.machine  = machine
        self.tenant = tenant
        self.gymBranchId = gym_branch_id
        self.lock     = RLock()
        self.handle   = None          # ← utilisé seulement pour les C3
        # ----- adapter -----
        if machine.type == MachineType.C3.name:
            self.adapter = PlcommAdapter(machine)
        elif machine.type == MachineType.STANDALONE_NEW_FIRMWARE.name:
            self.adapter = ZkemAdapter(machine)
        else:
            raise ValueError(f"Type inconnu : {machine.type}")

    def bind_handle(self, h = None):
        """Spécifique aux C3 : copie le handle partagé dans l’adapter"""
        self.handle = h
        if hasattr(self.adapter, "bind_handle"):
            self.adapter.bind_handle(h)

    # pour les C3 : permet au thread RT de déposer son handle
    def set_handle(self, h):
        self.handle = h
        if hasattr(self.adapter, "bind_handle"):
            self.adapter.bind_handle(h)

class DeviceManager:
    _registry: dict[int, DeviceContext] = {}

    @classmethod
    def register(cls, machine , tenant, gym_branch_id):
        if machine.id not in cls._registry:
            cls._registry[machine.id] = DeviceContext(machine,tenant, gym_branch_id)
        return cls._registry[machine.id]

    # alias pratique
    register_machine = register


    @classmethod
    def get(cls, mid):      return cls._registry[mid]
    @classmethod
    def all(cls):           return cls._registry.values()
