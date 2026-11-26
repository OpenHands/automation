"""Named shapes for json outcome keys.

The ``table.v1`` shape borrows from two published formats. The ``columns`` of a
key are a subset of the Frictionless Table Schema ``fields`` array
(https://specs.frictionlessdata.io/table-schema/): ``name``, ``title``, ``type``,
``constraints.required`` and ``constraints.enum``. A cell that carries a raw
value ``v`` and a display text ``f`` follows the Google Charts ``DataTable``
JSON (https://developers.google.com/chart/interactive/docs/reference#dataparam).
``href`` is our own.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openhands.automation.outcome_values import (
    MAX_OUTCOME_STRING_LENGTH,
    OUTCOME_KEY_PATTERN,
    OutcomeShape,
    validate_outcome_datetime,
    validate_outcome_number,
)


MAX_TABLE_COLUMNS = 20
MAX_TABLE_ROWS = 100
MAX_TABLE_ENUM_VALUES = 50
MAX_TABLE_ENUM_VALUE_LENGTH = 100
MAX_TABLE_HREF_LENGTH = 2048

TableColumnType = Literal["string", "number", "integer", "boolean", "datetime"]
_CELL_KEYS = {"v", "f", "href"}
# A backslash is out because browsers read "/\host" as "//host".
_UNSAFE_HREF = re.compile(r"[\s\x00-\x1f\x7f\\]")


class TableColumnConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = Field(
        default=False, description="A null cell is refused when this is true."
    )
    enum: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_TABLE_ENUM_VALUES,
        description="The values a string cell may hold.",
    )


class TableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=40, pattern=OUTCOME_KEY_PATTERN)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    type: TableColumnType
    constraints: TableColumnConstraints = Field(default_factory=TableColumnConstraints)

    @model_validator(mode="after")
    def check_enum(self) -> "TableColumn":
        values = self.constraints.enum
        if values is None:
            return self
        if self.type != "string":
            raise ValueError(f"Column {self.name!r}: enum applies only to a string")
        if len(set(values)) != len(values):
            raise ValueError(f"Column {self.name!r}: enum values must be unique")
        if any(len(value) > MAX_TABLE_ENUM_VALUE_LENGTH for value in values):
            raise ValueError(
                f"Column {self.name!r}: an enum value is longer than "
                f"{MAX_TABLE_ENUM_VALUE_LENGTH} characters"
            )
        return self


def check_table_columns(columns: list[TableColumn]) -> None:
    if not 0 < len(columns) <= MAX_TABLE_COLUMNS:
        raise ValueError(f"A table has between 1 and {MAX_TABLE_COLUMNS} columns")
    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise ValueError("Table column names must be unique")


def _check_href(href: Any, where: str) -> None:
    valid = (
        isinstance(href, str)
        and len(href) <= MAX_TABLE_HREF_LENGTH
        and not _UNSAFE_HREF.search(href)
        and (
            (href.startswith("https://") and len(href) > len("https://"))
            or (href.startswith("/") and not href.startswith("//"))
        )
    )
    if not valid:
        raise ValueError(f"{where}.href must be an https:// URL or a path starting /")


def _check_cell_value(value: Any, column: dict[str, Any], where: str) -> None:
    constraints = column.get("constraints") or {}
    if value is None:
        if constraints.get("required"):
            raise ValueError(
                f"{where} is null but column {column['name']!r} is required"
            )
        return
    column_type = column["type"]
    if column_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{where} is not a string")
        if len(value) > MAX_OUTCOME_STRING_LENGTH:
            raise ValueError(f"{where} exceeds {MAX_OUTCOME_STRING_LENGTH} characters")
        allowed = constraints.get("enum")
        if allowed is not None and value not in allowed:
            raise ValueError(f"{where} must be one of {allowed}, not {value!r}")
    elif column_type == "number":
        validate_outcome_number(value, where)
    elif column_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} is not an integer")
        validate_outcome_number(value, where)
    elif column_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{where} is not a boolean")
    else:
        validate_outcome_datetime(value, where)


def _check_cell(cell: Any, column: dict[str, Any], where: str) -> None:
    if not isinstance(cell, dict):
        _check_cell_value(cell, column, where)
        return
    if "v" not in cell or not cell.keys() <= _CELL_KEYS:
        raise ValueError(f"{where} is an object, so it needs v and only v, f, href")
    _check_cell_value(cell["v"], column, f"{where}.v")
    if "f" in cell:
        text = cell["f"]
        if not isinstance(text, str) or len(text) > MAX_OUTCOME_STRING_LENGTH:
            raise ValueError(
                f"{where}.f must be a string of at most "
                f"{MAX_OUTCOME_STRING_LENGTH} characters"
            )
    if "href" in cell:
        _check_href(cell["href"], where)


def validate_table_value(
    value: Any, columns: list[dict[str, Any]], key: str
) -> dict[str, Any]:
    """Check a ``table.v1`` value against the columns its key declares."""
    if not isinstance(value, dict) or not value.keys() <= {"rows", "total_rows"}:
        raise ValueError(f"{key}: a table is an object with rows and total_rows only")
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"{key}.rows must be a list")
    if len(rows) > MAX_TABLE_ROWS:
        raise ValueError(f"{key}.rows has more than {MAX_TABLE_ROWS} rows")
    total_rows = value.get("total_rows")
    if "total_rows" in value and (
        isinstance(total_rows, bool)
        or not isinstance(total_rows, int)
        or total_rows < len(rows)
    ):
        raise ValueError(f"{key}.total_rows must be an integer of at least {len(rows)}")
    for row_index, row in enumerate(rows):
        where = f"{key}.rows[{row_index}]"
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f"{where} must be a list of {len(columns)} cells")
        for column_index, (cell, column) in enumerate(zip(row, columns, strict=True)):
            _check_cell(cell, column, f"{where}[{column_index}]")
    return value


def validate_shape_value(
    shape: OutcomeShape, columns: list[dict[str, Any]] | None, value: Any, key: str
) -> Any:
    if shape == "table.v1":
        return validate_table_value(value, columns or [], key)
    raise ValueError(f"Unsupported outcome shape: {shape}")
