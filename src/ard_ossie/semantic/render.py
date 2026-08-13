"""Lossless Markdown rendering for reconciled semantic source spans."""

from __future__ import annotations

import string
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ard_ossie.semantic.correction import has_raw_html
from ard_ossie.semantic.models import (
    MAX_LIST_DEPTH,
    HeadingBlock,
    ListItemBlock,
    LosslessBlock,
    NativeDocument,
    ParagraphBlock,
    ReconciledDocument,
    SemanticBlock,
    SourceSpan,
    SpanId,
    TableBlock,
    _validate_grid,
)


@dataclass(frozen=True)
class CoverageResult:
    source_span_count: int
    preserved_span_count: int
    excluded_span_count: int
    unmatched_span_count: int
    duplicated_span_count: int
    source_text_coverage: float


class SemanticCoverageError(ValueError):
    def __init__(
        self,
        code: Literal[
            "SEMANTIC_SOURCE_TEXT_LOSS",
            "SEMANTIC_SOURCE_TEXT_DUPLICATED",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


class SemanticRawHtmlError(ValueError):
    def __init__(self) -> None:
        self.code = "SEMANTIC_RAW_HTML_OUTPUT"
        super().__init__(self.code)


_COMMONMARK_ESCAPABLE_PUNCTUATION = string.punctuation.replace("\\", "")


def validate_source_coverage(
    native: NativeDocument, document: ReconciledDocument
) -> CoverageResult:
    """Ensure every non-excluded source span is allocated exactly once.

    Table cells are counted from their original span IDs, before renderer-owned
    merged-cell expansion repeats values across the rectangular output matrix.
    """
    source_ids = {span.span_id for span in native.spans}
    allocation_ids = _allocated_span_ids(document.blocks)
    exclusion_ids = [item.span_id for item in document.excluded_spans]
    references = [*allocation_ids, *exclusion_ids]
    if any(span_id not in source_ids for span_id in references):
        raise SemanticCoverageError("SEMANTIC_SOURCE_TEXT_LOSS")

    allocations = Counter(allocation_ids)
    excluded_ids = set(exclusion_ids)
    if set(allocations) & excluded_ids or any(count > 1 for count in allocations.values()):
        raise SemanticCoverageError("SEMANTIC_SOURCE_TEXT_DUPLICATED")

    non_excluded_ids = source_ids - excluded_ids
    preserved_ids = non_excluded_ids & set(allocations)
    unmatched_ids = non_excluded_ids - preserved_ids
    duplicated_ids = {
        span_id
        for span_id, count in allocations.items()
        if span_id in non_excluded_ids and count > 1
    }
    coverage = (
        1.0
        if not non_excluded_ids
        else round(len(preserved_ids) / len(non_excluded_ids), 6)
    )
    result = CoverageResult(
        source_span_count=len(source_ids),
        preserved_span_count=len(preserved_ids),
        excluded_span_count=len(excluded_ids),
        unmatched_span_count=len(unmatched_ids),
        duplicated_span_count=len(duplicated_ids),
        source_text_coverage=coverage,
    )
    if result.unmatched_span_count:
        raise SemanticCoverageError("SEMANTIC_SOURCE_TEXT_LOSS")
    if result.duplicated_span_count:
        raise SemanticCoverageError("SEMANTIC_SOURCE_TEXT_DUPLICATED")
    return result


def render_semantic_markdown(
    document: ReconciledDocument, spans: Mapping[SpanId, SourceSpan]
) -> str:
    """Render reconciled source spans as deterministic, lossless Markdown."""
    chunks: list[str] = []
    list_lines: list[str] = []

    def flush_list() -> None:
        if list_lines:
            chunks.append("\n".join(list_lines))
            list_lines.clear()

    for block in sorted(document.blocks, key=lambda item: item.order):
        if isinstance(block, ListItemBlock):
            list_lines.append(_render_list_item(block, spans))
            continue
        flush_list()
        chunks.append(_render_block(block, spans))
    flush_list()

    rendered = "\n\n".join(chunk.rstrip("\n") for chunk in chunks) + "\n"
    if has_raw_html(rendered):
        raise SemanticRawHtmlError
    return rendered


def _allocated_span_ids(blocks: tuple[SemanticBlock, ...]) -> list[SpanId]:
    span_ids: list[SpanId] = []
    for block in blocks:
        if isinstance(block, TableBlock):
            span_ids.extend(span_id for cell in block.cells for span_id in cell.span_ids)
        else:
            span_ids.extend(block.span_ids)
    return span_ids


def _render_block(block: SemanticBlock, spans: Mapping[SpanId, SourceSpan]) -> str:
    if isinstance(block, HeadingBlock):
        return f"{'#' * block.level} {_escape_inline(_resolve(block.span_ids, spans))}"
    if isinstance(block, ParagraphBlock):
        return _escape_inline(_resolve(block.span_ids, spans))
    if isinstance(block, TableBlock):
        return _render_table(block, spans)
    if isinstance(block, LosslessBlock):
        return _escape_inline(_resolve(block.span_ids, spans))
    raise TypeError(f"Unsupported semantic block: {type(block)!r}")


def _render_list_item(block: ListItemBlock, spans: Mapping[SpanId, SourceSpan]) -> str:
    if block.depth > MAX_LIST_DEPTH:
        raise ValueError("LIST_DEPTH_LIMIT_EXCEEDED")
    marker = "1." if block.list_kind == "ordered" else "-"
    return f"{'    ' * block.depth}{marker} {_escape_inline(_resolve(block.span_ids, spans))}"


def _render_table(block: TableBlock, spans: Mapping[SpanId, SourceSpan]) -> str:
    _validate_render_table_limits(block)
    matrix = [["" for _ in range(block.column_count)] for _ in range(block.row_count)]
    header_rows: set[int] = set()
    for cell in block.cells:
        value = _resolve_table_value(cell.span_ids, spans)
        for row in range(cell.start_row, cell.end_row):
            for column in range(cell.start_column, cell.end_column):
                matrix[row][column] = value
        if cell.column_header:
            header_rows.add(cell.start_row)

    header_row = min(header_rows) if header_rows else None
    header = (
        matrix[header_row]
        if header_row is not None
        else ["" for _ in range(block.column_count)]
    )
    data_rows = [
        row
        for row_index, row in enumerate(matrix)
        if header_row is None or row_index != header_row
    ]
    lines = [_table_line(header), _table_line(["---" for _ in range(block.column_count)])]
    lines.extend(_table_line(row) for row in data_rows)
    return "\n".join(lines)


def _table_line(values: list[str]) -> str:
    return f"| {' | '.join(values)} |"


def _resolve(span_ids: tuple[SpanId, ...], spans: Mapping[SpanId, SourceSpan]) -> str:
    try:
        return "".join(spans[span_id].text for span_id in span_ids)
    except KeyError as exc:
        raise SemanticCoverageError("SEMANTIC_SOURCE_TEXT_LOSS") from exc


def _resolve_table_value(
    span_ids: tuple[SpanId, ...], spans: Mapping[SpanId, SourceSpan]
) -> str:
    values: list[str] = []
    try:
        for span_id in span_ids:
            span = spans[span_id]
            if span.paragraph_break_before and values:
                values.append("\n")
            values.append(span.text)
    except KeyError as exc:
        raise SemanticCoverageError("SEMANTIC_SOURCE_TEXT_LOSS") from exc
    return _escape_table_value("".join(values))


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in _COMMONMARK_ESCAPABLE_PUNCTUATION:
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _escape_inline(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\\\n".join(_escape_inline_line(line) for line in normalized.split("\n"))


def _escape_inline_line(value: str) -> str:
    leading_end = 0
    while leading_end < len(value) and value[leading_end] in {" ", "\t"}:
        leading_end += 1
    trailing_start = len(value)
    while trailing_start > leading_end and value[trailing_start - 1] in {" ", "\t"}:
        trailing_start -= 1
    leading = _encode_boundary_whitespace(value[:leading_end])
    middle = _escape_markdown(value[leading_end:trailing_start])
    trailing = _encode_boundary_whitespace(value[trailing_start:])
    return f"{leading}{middle}{trailing}"


def _encode_boundary_whitespace(value: str) -> str:
    return "".join("&#9;" if character == "\t" else "&#32;" for character in value)


def _escape_table_value(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    joined = " ".join(line.strip(" \t") for line in normalized.split("\n"))
    return _escape_markdown(joined)


def _validate_render_table_limits(block: TableBlock) -> None:
    _validate_grid(block.cells, block.row_count, block.column_count, "TABLE_BLOCK")
