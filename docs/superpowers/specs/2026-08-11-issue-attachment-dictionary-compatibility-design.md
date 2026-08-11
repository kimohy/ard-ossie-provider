# Issue Attachment and Data Dictionary Compatibility Design

## Goal

Make approved GitHub Issue submissions work with GitHub's current non-image attachment URLs and the multi-sheet Korean Data Dictionary template used by Issue #3, without weakening the trusted processing boundary.

## Confirmed failures

- GitHub generated `https://github.com/user-attachments/files/30932953/Marketing.Insight.Data.Dictionary.xlsx`, but intake accepts only `/user-attachments/assets/<UUID>` and raises `UNTRUSTED_ATTACHMENT_PATH`.
- The workbook uses one table per sheet, metadata rows above row 13, and Korean column headers. The flat row-1 English parser finds no supported sheet and raises `MISSING_DICTIONARY_HEADERS`.
- Issue #3 uses operation `create` while `Existing product ID` contains `Marketing Insight`. Create must not accept any existing product ID.
- Two sheets contain formatting residue after the first blank data row. Reading through `max_row` would introduce duplicate `loaded_at` columns.

## Attachment URL contract

Initial Issue URLs continue to require HTTPS, host exactly `github.com`, no credentials, query, fragment, or non-443 port. Two exact path families are accepted:

- `/user-attachments/assets/<canonical UUID>`
- `/user-attachments/files/<canonical positive decimal ID>/<single safe filename segment>`

For the `files` form, leading-zero IDs, empty or extra path segments, malformed percent escapes, decoded `/` or `\\`, dot paths, control characters, and filenames longer than 255 characters are rejected. Redirects may return to either canonical GitHub path or use the already-approved GitHub asset storage hosts. External repository paths and broad GitHub URLs remain rejected.

## Issue operation contract

`IssueIntake` rejects a non-empty `product_id` before field-level ID parsing when operation is `create`, using `PRODUCT_ID_FORBIDDEN_FOR_CREATE`. `update` continues to require a valid immutable product ID. The Issue template says unambiguously that create must leave the field empty.

## Excel dialect architecture

`parse_dictionary` dispatches each sheet through an ordered set of deterministic dialect parsers. A parser either does not recognize the sheet or returns normalized table data; recognized malformed input fails with a typed error instead of falling through to another dialect.

The existing flat dialect remains unchanged in meaning: row 1 contains English normalized headers and rows may describe multiple tables.

The Korean template dialect is recognized only when all of these are present:

- metadata labels `저장 플랫폼 및 세부 위치` and `테이블 명` before the column section;
- a header row containing `컬럼명`, `Type`, `Key 여부`, and `Null 허용`;
- contiguous data rows immediately below that header.

It maps:

- `catalog.schema` to `unspecified|catalog|schema|table`;
- `platform.catalog.schema` to `platform|catalog|schema|table`;
- `컬럼 설명` to the physical column description;
- `PK` or `PK, FK` to `primary_key=true`;
- `Y/N` in `Null 허용` to the existing strict boolean converter;
- table metadata `테이블 설명` to the initial table description.

The first fully blank row across the template's data columns ends the data section. This prevents formatted or accidentally populated residue below a blank separator from becoming schema. `FK` without an explicit target table and target column produces no `foreign_key`; the system does not infer relationships from names.

## Compatibility and extension rules

- Every locator always has four explicit parts; absent platform remains `unspecified` rather than an inferred vendor.
- Existing flat workbooks retain their output and evidence ranges.
- Each new workbook family must be added as a separate recognizable dialect with failing tests before implementation.
- Duplicate columns within a normalized table fail with `DUPLICATE_DICTIONARY_COLUMN`.
- Unsupported workbooks continue to fail closed with `MISSING_DICTIONARY_HEADERS`.

## Operational proof

The change is complete only after:

1. focused RED/GREEN tests and the full local verification suite pass;
2. the exact Issue #3 workbook parses as four tables and 40 columns;
3. a protected PR is created and its required GitHub Actions statuses succeed;
4. the code PR is merged, Issue #3's existing product ID is cleared, and a fresh approval-label event is emitted;
5. Issue intake creates the managed ARD Draft PR and the processing workflow reaches a terminal result. If the protected `ard-llm` Environment requires manual approval, that approval is the only permitted user intervention.
