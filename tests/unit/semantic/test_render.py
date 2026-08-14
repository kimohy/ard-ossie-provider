from __future__ import annotations

import hashlib
import re

import pytest
from markdown_it import MarkdownIt

from ard_ossie.semantic.correction import has_raw_html
from ard_ossie.semantic.models import (
    ExcludedSpan,
    ExtractionMode,
    HeadingBlock,
    ListItemBlock,
    LosslessBlock,
    NativeDocument,
    ParagraphBlock,
    ReconciledDocument,
    SourceSpan,
    TableBlock,
    TableCellBlock,
    make_span_id,
)
from ard_ossie.semantic.render import (
    SemanticCoverageError,
    render_semantic_markdown,
    validate_source_coverage,
)

SOURCE_HASH = "a" * 64
ASCII_ESCAPABLE_PUNCTUATION = r'''!"#$%&'()*+,-./:;<=>?@[]^_`{|}~'''


def source_span(ordinal: int, text: str) -> SourceSpan:
    return SourceSpan(
        span_id=make_span_id(SOURCE_HASH, ordinal),
        ordinal=ordinal,
        text=text,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def native_document(*spans: SourceSpan) -> NativeDocument:
    return NativeDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.DOCX_XML,
        page_count=0,
        parser_versions={},
        spans=spans,
        groups=(),
        tables=(),
    )


def document(*blocks: object, excluded_spans: tuple[ExcludedSpan, ...] = ()) -> ReconciledDocument:
    return ReconciledDocument(blocks=blocks, excluded_spans=excluded_spans)  # type: ignore[arg-type]


def test_renderer_preserves_spaces_escapes_controls_and_keeps_source_text_immutable() -> None:
    """A renderer that collapses spaces or mutates source strings loses document fidelity."""
    heading = source_span(0, "제목 #1")
    paragraph = source_span(1, r"A  B \\ * _ [x] ` |")
    native = native_document(heading, paragraph)
    semantic = document(
        HeadingBlock(order=0, level=2, span_ids=(heading.span_id,)),
        ParagraphBlock(order=1, span_ids=(paragraph.span_id,)),
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "## 제목 \\#1\n\n" + r"A  B \\\\ \* \_ \[x\] \` \|" + "\n"
    )
    assert native.span_catalog()[paragraph.span_id].text == r"A  B \\ * _ [x] ` |"


def test_renderer_groups_nested_ordered_and_unordered_lists_in_source_order() -> None:
    """A blank line between list items would break the source list hierarchy."""
    first = source_span(0, "first")
    child = source_span(1, "child")
    final = source_span(2, "last")
    native = native_document(first, child, final)
    semantic = document(
        ListItemBlock(order=0, list_kind="ordered", depth=0, span_ids=(first.span_id,)),
        ListItemBlock(order=1, list_kind="unordered", depth=1, span_ids=(child.span_id,)),
        ListItemBlock(order=2, list_kind="ordered", depth=0, span_ids=(final.span_id,)),
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "1. first\n    - child\n1. last\n"
    )


@pytest.mark.parametrize(
    ("parent_kind", "child_kind", "expected_html"),
    (
        ("ordered", "unordered", "<ol>\n<li>parent\n<ul>\n<li>child</li>\n</ul>\n</li>\n</ol>\n"),
        ("unordered", "ordered", "<ul>\n<li>parent\n<ol>\n<li>child</li>\n</ol>\n</li>\n</ul>\n"),
    ),
)
def test_renderer_nests_all_list_marker_combinations_as_gfm_lists(
    parent_kind: str, child_kind: str, expected_html: str
) -> None:
    """Two-space child indentation is not valid for every ordered-list parent."""
    parent = source_span(0, "parent")
    child = source_span(1, "child")
    native = native_document(parent, child)
    semantic = document(
        ListItemBlock(order=0, list_kind=parent_kind, depth=0, span_ids=(parent.span_id,)),  # type: ignore[arg-type]
        ListItemBlock(order=1, list_kind=child_kind, depth=1, span_ids=(child.span_id,)),  # type: ignore[arg-type]
    )

    rendered = render_semantic_markdown(semantic, native.span_catalog())

    assert rendered == (
        ("1." if parent_kind == "ordered" else "-")
        + " parent\n    "
        + ("1." if child_kind == "ordered" else "-")
        + " child\n"
    )
    assert MarkdownIt("commonmark").render(rendered) == expected_html


def test_renderer_escapes_all_commonmark_ascii_punctuation_without_creating_blocks() -> None:
    """Unescaped fences, setext markers, and entities can reframe later source as Markdown."""
    hostile = source_span(0, f"~~~\n===\n&amp; {ASCII_ESCAPABLE_PUNCTUATION}")
    native = native_document(hostile)
    semantic = document(ParagraphBlock(order=0, span_ids=(hostile.span_id,)))

    rendered = render_semantic_markdown(semantic, native.span_catalog())
    tokens = MarkdownIt("commonmark").parse(rendered)

    assert all(f"\\{character}" in rendered for character in ASCII_ESCAPABLE_PUNCTUATION)
    assert [token.type for token in tokens] == ["paragraph_open", "inline", "paragraph_close"]
    assert MarkdownIt("commonmark").render(rendered).startswith(
        "<p>~~~<br />\n===<br />\n&amp;amp;"
    )


def test_renderer_encodes_inline_line_endings_without_extra_block_boundaries() -> None:
    """Raw inline line endings can create trailing blank blocks instead of source line breaks."""
    heading = source_span(0, "heading\n")
    paragraph = source_span(1, "first\r\nsecond\r")
    item = source_span(2, "item\ncontinued\n")
    native = native_document(heading, paragraph, item)
    semantic = document(
        HeadingBlock(order=0, level=1, span_ids=(heading.span_id,)),
        ParagraphBlock(order=1, span_ids=(paragraph.span_id,)),
        ListItemBlock(order=2, list_kind="unordered", depth=0, span_ids=(item.span_id,)),
    )

    rendered = render_semantic_markdown(semantic, native.span_catalog())

    assert rendered == "# heading\\\n\nfirst\\\nsecond\\\n\n- item\\\ncontinued\\\n"
    assert "<br>" not in rendered
    assert "\n\n\n" not in rendered
    assert [token.type for token in MarkdownIt("commonmark").parse(rendered)] == [
        "heading_open",
        "inline",
        "heading_close",
        "paragraph_open",
        "inline",
        "paragraph_close",
        "bullet_list_open",
        "list_item_open",
        "paragraph_open",
        "inline",
        "paragraph_close",
        "list_item_close",
        "bullet_list_close",
    ]


def test_renderer_normalizes_boundary_whitespace_without_emitting_html_entities() -> None:
    tabbed = source_span(0, "\talpha")
    spaced = source_span(1, "    beta")
    blank = source_span(2, " \t ")
    native = native_document(tabbed, spaced, blank)
    semantic = document(
        ParagraphBlock(order=0, span_ids=(tabbed.span_id,)),
        ParagraphBlock(order=1, span_ids=(spaced.span_id,)),
        ParagraphBlock(order=2, span_ids=(blank.span_id,)),
    )

    rendered = render_semantic_markdown(semantic, native.span_catalog())
    assert rendered == "alpha\n\nbeta\n\n\n"
    assert not has_raw_html(rendered)
    assert re.search(r"(?<!\\)&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);", rendered) is None
    assert "&#32;" not in rendered
    assert "&#9;" not in rendered


def test_renderer_rejects_unvalidated_list_depth_before_indent_allocation() -> None:
    span = source_span(0, "item")
    native = native_document(span)
    unsafe = ListItemBlock.model_construct(
        order=0,
        list_kind="unordered",
        depth=1_000_000_000,
        span_ids=(span.span_id,),
    )

    with pytest.raises(ValueError, match="LIST_DEPTH_LIMIT_EXCEEDED"):
        render_semantic_markdown(document(unsafe), native.span_catalog())


def test_renderer_expands_merged_cells_without_duplicating_source_coverage() -> None:
    """Merged-cell repetition belongs only to Markdown layout, not source allocations."""
    spans = tuple(
        source_span(index, text)
        for index, text in enumerate(("지역", "매출", "서울", "100"))
    )
    native = native_document(*spans)
    semantic = document(
        TableBlock(
            order=0,
            row_count=2,
            column_count=3,
            cells=(
                TableCellBlock(
                    start_row=0, end_row=1, start_column=0, end_column=2,
                    span_ids=(spans[0].span_id,), column_header=True,
                ),
                TableCellBlock(
                    start_row=0, end_row=1, start_column=2, end_column=3,
                    span_ids=(spans[1].span_id,), column_header=True,
                ),
                TableCellBlock(
                    start_row=1, end_row=2, start_column=0, end_column=2,
                    span_ids=(spans[2].span_id,),
                ),
                TableCellBlock(
                    start_row=1, end_row=2, start_column=2, end_column=3,
                    span_ids=(spans[3].span_id,),
                ),
            ),
        )
    )

    coverage = validate_source_coverage(native, semantic)

    assert coverage.source_span_count == 4
    assert coverage.preserved_span_count == 4
    assert coverage.excluded_span_count == 0
    assert coverage.unmatched_span_count == 0
    assert coverage.duplicated_span_count == 0
    assert coverage.source_text_coverage == 1.0
    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "| 지역 | 지역 | 매출 |\n"
        "| --- | --- | --- |\n"
        "| 서울 | 서울 | 100 |\n"
    )


def test_renderer_is_html_free_and_joins_multiline_table_cells() -> None:
    unresolved = source_span(0, "<pre>원문<br></pre>")
    header = source_span(1, "설명")
    body = source_span(2, "첫 줄\r\n 둘째 | 줄 \r 셋째 \\값")
    native = native_document(unresolved, header, body)
    semantic = document(
        LosslessBlock(
            order=0,
            span_ids=(unresolved.span_id,),
            reason="structure_unresolved",
        ),
        TableBlock(
            order=1,
            row_count=2,
            column_count=1,
            cells=(
                TableCellBlock(
                    start_row=0, end_row=1, start_column=0, end_column=1,
                    span_ids=(header.span_id,), column_header=True,
                ),
                TableCellBlock(
                    start_row=1, end_row=2, start_column=0, end_column=1,
                    span_ids=(body.span_id,),
                ),
            ),
        )
    )

    rendered = render_semantic_markdown(semantic, native.span_catalog())

    assert rendered == (
        r"\<pre\>원문\<br\>\<\/pre\>" + "\n\n"
        "| 설명 |\n"
        "| --- |\n"
        r"| 첫 줄 둘째 \| 줄 셋째 \\값 |" + "\n"
    )
    assert "<pre>" not in rendered
    assert "<br>" not in rendered
    assert has_raw_html(rendered) is False


def test_renderer_inserts_owned_break_between_distinct_table_cell_paragraphs() -> None:
    first = source_span(0, "first paragraph")
    second = source_span(1, "second paragraph")
    object.__setattr__(second, "paragraph_break_before", True)
    native = native_document(first, second)
    semantic = document(
        TableBlock(
            order=0,
            row_count=1,
            column_count=1,
            cells=(
                TableCellBlock(
                    start_row=0,
                    end_row=1,
                    start_column=0,
                    end_column=1,
                    span_ids=(first.span_id, second.span_id),
                ),
            ),
        )
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "|  |\n| --- |\n| first paragraph second paragraph |\n"
    )


def test_renderer_rejects_bypassed_unbounded_table_before_matrix_allocation() -> None:
    block = TableBlock.model_construct(
        order=0,
        row_count=1,
        column_count=257,
        cells=(),
    )

    with pytest.raises(ValueError, match="TABLE_COLUMNS_LIMIT_EXCEEDED"):
        render_semantic_markdown(document(block), {})


@pytest.mark.parametrize(
    ("row_count", "column_count"),
    ((0, 1), (1, 0), (-1, 1), (1, -1)),
)
def test_renderer_rejects_bypassed_nonpositive_table_dimensions(
    row_count: int,
    column_count: int,
) -> None:
    block = TableBlock.model_construct(
        order=0,
        row_count=row_count,
        column_count=column_count,
        cells=(),
    )
    bypassed = ReconciledDocument.model_construct(blocks=(block,), excluded_spans=())

    with pytest.raises(ValueError, match="TABLE_DIMENSIONS_INVALID"):
        render_semantic_markdown(bypassed, {})


def test_renderer_revalidates_partition_before_area_expansion() -> None:
    cells = tuple(
        TableCellBlock.model_construct(
            start_row=0,
            end_row=100,
            start_column=0,
            end_column=100,
            span_ids=(),
            column_header=False,
        )
        for _ in range(2)
    )
    block = TableBlock.model_construct(
        order=0,
        row_count=100,
        column_count=100,
        cells=cells,
    )
    bypassed = ReconciledDocument.model_construct(blocks=(block,), excluded_spans=())

    with pytest.raises(ValueError, match="TABLE_BLOCK_CELL_REGIONS_OVERLAP"):
        render_semantic_markdown(bypassed, {})


def test_renderer_creates_empty_header_without_dropping_source_rows() -> None:
    """A headerless table needs GFM structure while retaining every source row."""
    spans = tuple(source_span(index, text) for index, text in enumerate(("A", "B", "1", "2")))
    native = native_document(*spans)
    semantic = document(
        TableBlock(
            order=0,
            row_count=2,
            column_count=2,
            cells=tuple(
                TableCellBlock(
                    start_row=row,
                    end_row=row + 1,
                    start_column=column,
                    end_column=column + 1,
                    span_ids=(spans[row * 2 + column].span_id,),
                )
                for row in range(2)
                for column in range(2)
            ),
        )
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "|  |  |\n| --- | --- |\n| A | B |\n| 1 | 2 |\n"
    )


def test_renderer_retains_blank_table_cells() -> None:
    """Cells without source spans are structural blanks, not invented text values."""
    header = source_span(0, "값")
    native = native_document(header)
    semantic = document(
        TableBlock(
            order=0,
            row_count=2,
            column_count=2,
            cells=(
                TableCellBlock(
                    start_row=0, end_row=1, start_column=0, end_column=1,
                    span_ids=(header.span_id,), column_header=True,
                ),
                TableCellBlock(start_row=0, end_row=1, start_column=1, end_column=2),
                TableCellBlock(start_row=1, end_row=2, start_column=0, end_column=1),
                TableCellBlock(start_row=1, end_row=2, start_column=1, end_column=2),
            ),
        )
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "| 값 |  |\n| --- | --- |\n|  |  |\n"
    )


def test_renderer_keeps_additional_header_rows_as_source_rows() -> None:
    """Only the first explicit header row may become the structural GFM header."""
    spans = tuple(
        source_span(index, text)
        for index, text in enumerate(("지역", "매출", "시도", "금액", "서울", "100"))
    )
    native = native_document(*spans)
    semantic = document(
        TableBlock(
            order=0,
            row_count=3,
            column_count=2,
            cells=tuple(
                    TableCellBlock(
                        start_row=row,
                        end_row=row + 1,
                        start_column=column,
                        end_column=column + 1,
                        span_ids=(spans[row * 2 + column].span_id,),
                    column_header=row < 2,
                )
                for row in range(3)
                for column in range(2)
            ),
        )
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "| 지역 | 매출 |\n"
        "| --- | --- |\n"
        "| 시도 | 금액 |\n"
        "| 서울 | 100 |\n"
    )


def test_lossless_block_escapes_html_without_changing_exact_resolved_text() -> None:
    """Unresolved source text remains an escaped ordinary Markdown paragraph."""
    unresolved = source_span(0, "A  B\n<1 & 2>")
    native = native_document(unresolved)
    semantic = document(
        LosslessBlock(
            order=0,
            span_ids=(unresolved.span_id,),
            reason="structure_unresolved",
        )
    )

    assert render_semantic_markdown(semantic, native.span_catalog()) == (
        "A  B\\\n\\<1 \\& 2\\>\n"
    )


def test_coverage_excludes_native_spans_and_uses_one_for_empty_text_documents() -> None:
    """Excluded text and text-empty structural tables must not create a false coverage failure."""
    excluded = source_span(0, "page 1")
    native = native_document(excluded)
    excluded_document = document(
        excluded_spans=(ExcludedSpan(span_id=excluded.span_id, kind="page_number"),)
    )
    empty_table_document = document(
        TableBlock(
            order=0,
            row_count=1,
            column_count=1,
            cells=(TableCellBlock(start_row=0, end_row=1, start_column=0, end_column=1),),
        )
    )
    text_empty_native = native_document()

    excluded_coverage = validate_source_coverage(native, excluded_document)
    empty_coverage = validate_source_coverage(text_empty_native, empty_table_document)

    assert excluded_coverage.source_span_count == 1
    assert excluded_coverage.excluded_span_count == 1
    assert excluded_coverage.preserved_span_count == 0
    assert excluded_coverage.source_text_coverage == 1.0
    assert empty_coverage.source_span_count == 0
    assert empty_coverage.source_text_coverage == 1.0


def test_coverage_raises_exact_loss_code_for_unallocated_nonexcluded_span() -> None:
    """Dropping native source text must stop publication with the stable loss code."""
    kept = source_span(0, "kept")
    dropped = source_span(1, "dropped")
    native = native_document(kept, dropped)
    semantic = document(ParagraphBlock(order=0, span_ids=(kept.span_id,)))

    with pytest.raises(SemanticCoverageError) as exc_info:
        validate_source_coverage(native, semantic)

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_LOSS"


def test_coverage_raises_exact_duplicate_code_before_table_expansion() -> None:
    """Allocating one source ID to multiple semantic cells is a duplicate, not a merge."""
    value = source_span(0, "value")
    native = native_document(value)
    semantic = ReconciledDocument.model_construct(
        blocks=(
            TableBlock(
                order=0,
                row_count=1,
                column_count=2,
                cells=(
                    TableCellBlock(
                        start_row=0, end_row=1, start_column=0, end_column=1,
                        span_ids=(value.span_id,),
                    ),
                    TableCellBlock(
                        start_row=0, end_row=1, start_column=1, end_column=2,
                        span_ids=(value.span_id,),
                    ),
                ),
            ),
        ),
        excluded_spans=(),
    )

    with pytest.raises(SemanticCoverageError) as exc_info:
        validate_source_coverage(native, semantic)

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_DUPLICATED"


def test_coverage_rejects_unknown_span_ids_as_source_loss() -> None:
    """Unknown IDs cannot be counted as preservation or silently published."""
    known = source_span(0, "known")
    unknown_id = make_span_id(SOURCE_HASH, 1)
    native = native_document(known)
    semantic = ReconciledDocument.model_construct(
        blocks=(ParagraphBlock(order=0, span_ids=(known.span_id, unknown_id)),),
        excluded_spans=(),
    )

    with pytest.raises(SemanticCoverageError) as exc_info:
        validate_source_coverage(native, semantic)

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_LOSS"


def test_coverage_rejects_unknown_table_and_exclusion_ids_before_set_operations() -> None:
    """Bypassed model validation cannot smuggle unknown IDs into cells or exclusions."""
    known = source_span(0, "known")
    unknown_id = make_span_id(SOURCE_HASH, 1)
    native = native_document(known)
    semantic = ReconciledDocument.model_construct(
        blocks=(
            TableBlock(
                order=0,
                row_count=1,
                column_count=1,
                cells=(
                    TableCellBlock(
                        start_row=0, end_row=1, start_column=0, end_column=1,
                        span_ids=(unknown_id,),
                    ),
                ),
            ),
        ),
        excluded_spans=(ExcludedSpan(span_id=unknown_id, kind="page_number"),),
    )

    with pytest.raises(SemanticCoverageError) as exc_info:
        validate_source_coverage(native, semantic)

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_LOSS"


def test_coverage_rejects_allocated_and_excluded_span_overlap_as_duplicate() -> None:
    """One source span cannot be both published and excluded from publication."""
    value = source_span(0, "value")
    native = native_document(value)
    semantic = ReconciledDocument.model_construct(
        blocks=(ParagraphBlock(order=0, span_ids=(value.span_id,)),),
        excluded_spans=(ExcludedSpan(span_id=value.span_id, kind="page_header"),),
    )

    with pytest.raises(SemanticCoverageError) as exc_info:
        validate_source_coverage(native, semantic)

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_DUPLICATED"


def test_coverage_rejects_repeated_allocations_of_excluded_ids_as_duplicate() -> None:
    """Excluded IDs still require unique pre-render allocation accounting."""
    value = source_span(0, "value")
    native = native_document(value)
    semantic = ReconciledDocument.model_construct(
        blocks=(
            ParagraphBlock(order=0, span_ids=(value.span_id,)),
            ParagraphBlock(order=1, span_ids=(value.span_id,)),
        ),
        excluded_spans=(ExcludedSpan(span_id=value.span_id, kind="page_footer"),),
    )

    with pytest.raises(SemanticCoverageError) as exc_info:
        validate_source_coverage(native, semantic)

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_DUPLICATED"


def test_renderer_rejects_unknown_span_ids_instead_of_inventing_text() -> None:
    """A missing catalog entry must not become an empty or authored Markdown value."""
    unknown_id = make_span_id(SOURCE_HASH, 1)
    semantic = ReconciledDocument.model_construct(
        blocks=(ParagraphBlock(order=0, span_ids=(unknown_id,)),),
        excluded_spans=(),
    )

    with pytest.raises(SemanticCoverageError) as exc_info:
        render_semantic_markdown(semantic, {})

    assert exc_info.value.code == "SEMANTIC_SOURCE_TEXT_LOSS"
