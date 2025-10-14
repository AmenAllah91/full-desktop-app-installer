from dataclasses import dataclass
from typing import List
from domain import Operation
from domain import AccessMachine


@dataclass
class NewAccessRequest:
    gymBranchId: int
    operation: Operation
    gymBranchName: str
    userId: str
    userPin: str
    cardNo: str
    startDate: str
    endDate: str
    machines: List[AccessMachine]
