from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook
from pydantic import Field

from ard_ossie.docling_parser import Evidence
from ard_ossie.ingestion import SourceRole
from ard_ossie.models import Sha256, StrictModel


class DictionaryColumn(StrictModel):
    ordinal: int = Field(gt=0)
    name: str
    logical_name: str | None = None
    data_type: str
    nullable: bool
    primary_key: bool
    foreign_key: str | None = None
    description: str | None = None
    formula: str | None = None
    comment: str | None = None
    evidence: Evidence


class DictionaryTable(StrictModel):
    locator: str
    columns: list[DictionaryColumn]


class ParsedDictionary(StrictModel):
    source_hash: Sha256
    tables: list[DictionaryTable]


_REQUIRED_HEADERS = frozenset(
    {"platform", "catalog", "schema", "table", "column", "data_type", "nullable", "pk"}
)


def parse_dictionary(path: str | Path, *, source_hash: str) -> ParsedDictionary:
    workbook = load_workbook(Path(path), data_only=False, read_only=False)
    grouped: dict[str, list[DictionaryColumn]] = defaultdict(list)
    parsed_sheet = False
    try:
        for sheet in workbook.worksheets:
            headers = {
                _normalize_header(cell.value): index
                for index, cell in enumerate(sheet[1], start=1)
                if cell.value is not None
            }
            if not headers:
                continue
            missing = sorted(_REQUIRED_HEADERS - set(headers))
            if missing:
                continue
            parsed_sheet = True
            for row_number in range(2, sheet.max_row + 1):
                column_name = _cell_value(sheet, row_number, headers["column"])
                if column_name is None or not str(column_name).strip():
                    continue
                platform = _required_value(sheet, row_number, headers, "platform")
                catalog = _required_value(sheet, row_number, headers, "catalog")
                schema = _required_value(sheet, row_number, headers, "schema")
                table = _required_value(sheet, row_number, headers, "table")
                locator = "|".join(
                    str(value).strip().lower() for value in (platform, catalog, schema, table)
                )
                fk_table = _as_optional_text(
                    _optional_value(sheet, row_number, headers, "fk_table")
                )
                fk_column = _as_optional_text(
                    _optional_value(sheet, row_number, headers, "fk_column")
                )
                if bool(fk_table) != bool(fk_column):
                    raise ValueError(
                        f"INCOMPLETE_FOREIGN_KEY: {sheet.title}!{row_number}"
                    )
                foreign_key = (
                    f"{fk_table}.{fk_column}"
                    if fk_table and fk_column
                    else None
                )
                row_cells = sheet[row_number]
                start = row_cells[0].coordinate
                end = row_cells[len(headers) - 1].coordinate
                source_cell = sheet.cell(row=row_number, column=headers["column"])
                explicit_comment = _as_optional_text(
                    _optional_value(sheet, row_number, headers, "comment")
                )
                grouped[locator].append(
                    DictionaryColumn(
                        ordinal=len(grouped[locator]) + 1,
                        name=str(column_name).strip(),
                        logical_name=_as_optional_text(
                            _optional_value(sheet, row_number, headers, "logical_name")
                        ),
                        data_type=str(
                            _required_value(sheet, row_number, headers, "data_type")
                        ).strip(),
                        nullable=_as_bool(_required_value(sheet, row_number, headers, "nullable")),
                        primary_key=_as_bool(_required_value(sheet, row_number, headers, "pk")),
                        foreign_key=foreign_key,
                        description=_as_optional_text(
                            _optional_value(sheet, row_number, headers, "description")
                        ),
                        formula=_as_optional_text(
                            _optional_value(sheet, row_number, headers, "formula")
                        ),
                        comment=(
                            explicit_comment
                            if explicit_comment is not None
                            else (source_cell.comment.text if source_cell.comment else None)
                        ),
                        evidence=Evidence(
                            source_hash=source_hash,
                            role=SourceRole.DICTIONARY_EXCEL,
                            locator={"sheet": sheet.title, "range": f"{start}:{end}"},
                            excerpt=str(column_name).strip(),
                        ),
                    )
                )
    finally:
        workbook.close()

    if not parsed_sheet:
        raise ValueError("MISSING_DICTIONARY_HEADERS")
    return ParsedDictionary(
        source_hash=source_hash,
        tables=[
            DictionaryTable(locator=locator, columns=grouped[locator])
            for locator in sorted(grouped)
        ],
    )


def _normalize_header(value: object) -> str:
    return str(value).strip().lower().replace(" ", "_")


def _cell_value(sheet, row: int, column: int):
    return sheet.cell(row=row, column=column).value


def _required_value(sheet, row: int, headers: dict[str, int], name: str):
    value = _cell_value(sheet, row, headers[name])
    if value is None or not str(value).strip():
        raise ValueError(f"MISSING_DICTIONARY_VALUE: {sheet.title}!{row}:{name}")
    return value


def _optional_value(sheet, row: int, headers: dict[str, int], name: str):
    column = headers.get(name)
    return None if column is None else _cell_value(sheet, row, column)


def _as_optional_text(value: object) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "y", "yes"}:
        return True
    if normalized in {"0", "false", "n", "no"}:
        return False
    raise ValueError(f"INVALID_BOOLEAN_VALUE: {value}")
