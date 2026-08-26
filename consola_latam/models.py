from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InputCase:
    input_id: str
    radicado: str
    demandante: str
    demandado: str
    excel_row: int
