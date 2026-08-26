"""OCI data primitives"""

from __future__ import annotations

from dataclasses import dataclass, field

from mercury_ocip_fast.utils.defines import to_snake_case


@dataclass
class OCINil:
    pass


@dataclass
class OCITableRow:
    col: list[str]

    def __init__(self, col):
        self.col = col


@dataclass
class OCITable:
    col_heading: list[str]
    row: list[OCITableRow] = field(default_factory=list)

    def to_dict(self) -> list[dict[str, str]]:
        return [
            {
                to_snake_case(self.col_heading[i]): row.col[i]
                for i in range(len(self.col_heading))
            }
            for row in self.row
        ]
