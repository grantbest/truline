from typing import Literal

FinanceCategory = Literal[
    "groceries",
    "dining",
    "utilities",
    "kids",
    "auto",
    "housing",
    "entertainment",
    "health",
    "interest_fees",
    "misc",
]

SubstrateNamespace = Literal["finance", "personal", "homelab"]
MonthSelector = str  # "current" | "last" | "YYYY-MM"
