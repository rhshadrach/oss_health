from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Summary:
    name: str
    days: int
    history: pd.DataFrame
    regular_commiters: set[str]
    regular_commiters_summary: pd.DataFrame
    top_irregular_commiters: pd.DataFrame
