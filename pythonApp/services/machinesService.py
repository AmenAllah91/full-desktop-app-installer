# service.py
import os
import requests

from domain import AccessMachine


class AccessMachineService:
    def __init__(self, base_url=None, timeout=5):
        self.base_url = base_url or os.getenv("API_BASE_URL", "https://app.yogym.co/gym-management/public")
        self.session = requests.Session()
        self.timeout = timeout

    def get_access_machines(self, gym_branch_id: str , tenant: str) -> list[AccessMachine]:
        url = f"{self.base_url}/am/gb/{gym_branch_id}/{tenant}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()  # liste de dicts

        # mapping vers vos dataclasses
        machines = []
        for item in data:
            machines.append(AccessMachine(
                id=item.get("id"),
                alias=item.get("alias"),
                addresseip=item.get("addresseip"),
                port=item.get("port"),
                statut=item.get("statut"),
                type=item.get("type"),
                door1=item.get("door1"),
                door2=item.get("door2"),
                door3=item.get("door3"),
                door4=item.get("door4"),
                porte_type=item.get("porte_type"),
                comKey=item.get("comKey") or 0
            ))
        return machines
