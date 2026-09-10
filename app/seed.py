"""Synthetic seed data for the Meridian back-office target app.

Everything here is fabricated. The SSN- and card-shaped fields exist so the
redaction and screenshot-masking paths are exercised against realistic *shapes*
with zero real PII.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Check:
    number: str
    amount: float
    status: str  # open | cleared | stopped


@dataclass
class Account:
    number: str          # full; never rendered in full
    kind: str            # Checking | Savings
    balance: float
    checks: list[Check] = field(default_factory=list)

    @property
    def masked(self) -> str:
        return "****" + self.number[-4:]


@dataclass
class Member:
    member_id: str
    last: str
    first: str
    ssn: str
    card: str
    restricted: bool = False
    accounts: list[Account] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.last}, {self.first[0]}."

    @property
    def full_name(self) -> str:
        return f"{self.last}, {self.first}"

    def account(self, masked_or_kind: str) -> Account | None:
        for a in self.accounts:
            if masked_or_kind in (a.masked, a.kind, a.number):
                return a
        return None


MEMBERS: dict[str, Member] = {
    "100482": Member(
        member_id="100482", last="BARNES", first="ROSALIND",
        ssn="412-88-7390", card="4539872210034821",
        accounts=[
            Account("620041884821", "Checking", 4182.55, [
                Check("1043", 320.00, "open"),
                Check("1009", 88.40, "cleared"),
                Check("1051", 1200.00, "open"),
            ]),
            Account("620041889930", "Savings", 15903.12, []),
        ],
    ),
    "100517": Member(
        member_id="100517", last="OKONKWO", first="DANIEL",
        ssn="509-14-2266", card="4539110847217715",
        accounts=[
            Account("620041887715", "Checking", 921.03, [
                Check("2210", 145.75, "open"),
                Check("2204", 60.00, "cleared"),
            ]),
        ],
    ),
    "100633": Member(
        member_id="100633", last="VOSS", first="MARGARET",
        ssn="330-71-5518", card="4539004417883388",
        restricted=True,
        accounts=[
            Account("620041883388", "Checking", 2740.19, [
                Check("3001", 410.00, "open"),
            ]),
        ],
    ),
}

OPERATORS = {"roper": "meridian-demo-pw"}
