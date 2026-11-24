"""Synthetic ledger: fictional accounts and one month of made-up transactions. No real data."""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass

OPENING_BALANCES_GBP = {"checking": 1200.00, "savings": 3000.00, "holiday_pot": 400.00}
ACCOUNT_LABELS = {"checking": "checking", "savings": "savings", "holiday_pot": "holiday pot"}
SALARY_GBP = 2400.00


@dataclass(frozen=True)
class Transaction:
    id: str
    date: str
    account: str
    description: str
    amount_gbp: float
    tag: str | None = None


def generate_month(year: int = 2026, month: int = 11, seed: int = 7) -> list[Transaction]:
    """Deterministic for a given seed, so simulation results are reproducible."""
    rng = random.Random(seed)
    last_day = calendar.monthrange(year, month)[1]
    rows: list[tuple[int, str, float, str | None]] = [
        (25, "Example Payroll Ltd salary", SALARY_GBP, "salary"),
        (14, "Fixit Motors garage repair", -850.00, None),
        (8, "Lantern Energy bill", -96.40, None),
        (3, "StreamCo music", -9.99, None),
    ]
    for _ in range(6):
        rows.append((rng.randint(1, last_day), "Corner Grocer", -round(rng.uniform(22, 85), 2), None))
    for _ in range(3):
        rows.append((rng.randint(1, last_day), "Metro transit top-up", -round(rng.uniform(20, 40), 2), None))
    for _ in range(5):
        rows.append((rng.randint(1, last_day), "Bean and Brew Cafe", -round(rng.uniform(3, 9), 2), None))
    for _ in range(3):
        rows.append((rng.randint(1, last_day), "Riverside Restaurant", -round(rng.uniform(25, 60), 2), None))
    rows.sort(key=lambda r: (r[0], r[1]))
    transactions = []
    for n, (day, description, amount, tag) in enumerate(rows, start=1):
        transactions.append(
            Transaction(
                id=f"TX-{month:02d}{day:02d}-{n:02d}",
                date=f"{year:04d}-{month:02d}-{day:02d}",
                account="checking",
                description=description,
                amount_gbp=round(amount, 2),
                tag=tag,
            )
        )
    return transactions
