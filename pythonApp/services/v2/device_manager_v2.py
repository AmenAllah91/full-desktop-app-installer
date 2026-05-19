"""
services/v2/device_manager_v2.py
=================================
Registre de machines v2 — toutes en PUSH/ADMS.

Pas de distinction de type : chaque machine a un ADMSAdapterV2.
"""

import logging
from threading import RLock
from typing import Optional, Dict, List

from domain.AccessMachine import AccessMachine
from services.v2.adms_adapter_v2 import ADMSAdapterV2

logger = logging.getLogger("DeviceManager-v2")


class DeviceContextV2:
    """Contexte d'une machine enregistrée en v2."""

    def __init__(self, machine: AccessMachine, tenant: str, gym_branch_id: str):
        self.machine = machine
        self.tenant = tenant
        self.gym_branch_id = gym_branch_id
        self.lock = RLock()
        self.adapter = ADMSAdapterV2(machine)


class DeviceManagerV2:
    """Registre global des machines — version v2 full PUSH."""

    _registry: Dict[int, DeviceContextV2] = {}

    @classmethod
    def register(cls, machine: AccessMachine, tenant: str,
                 gym_branch_id: str) -> DeviceContextV2:
        if machine.id not in cls._registry:
            ctx = DeviceContextV2(machine, tenant, gym_branch_id)
            cls._registry[machine.id] = ctx
            logger.info("📋 Machine enregistrée: id=%s, alias=%s, type_original=%s",
                        machine.id, machine.alias, machine.type)
        return cls._registry[machine.id]

    @classmethod
    def get(cls, machine_id: int) -> Optional[DeviceContextV2]:
        return cls._registry.get(machine_id)

    @classmethod
    def all(cls) -> List[DeviceContextV2]:
        return list(cls._registry.values())

    @classmethod
    def get_by_sn(cls, sn: str) -> Optional[DeviceContextV2]:
        """Trouve un contexte par numéro de série."""
        for ctx in cls._registry.values():
            if ctx.adapter.sn == sn:
                return ctx
        return None

    @classmethod
    def clear(cls):
        cls._registry.clear()
