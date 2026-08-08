# ARD Ossie Provider

Git-managed compiler for converting AI Ready Data documents into structured artifacts and Apache Ossie semantic models.

## Inputs

- Data product information: HTML
- Data semantic documents: DOCX or PDF
- Data dictionary documents: XLSX

## Generated artifacts

- `data-product.md`
- `data-semantic.md`
- `data-dictionary.json`
- `ossie-model.json`

## Architecture

The project uses Docling-centered parsing, format-specific adapters, a canonical intermediate representation, stable product/table identifiers, many-to-many product–table mappings, and a deterministic Ossie 0.1.1 compiler.

LLM-assisted extraction is accessed through a configurable OpenAI-compatible API. LLM output is schema-constrained and validated locally; final IDs and Ossie documents are produced by deterministic code.

See the [architecture design](docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md) for the approved system design.

## Project status

Architecture approved. Implementation has not started.

## License

Apache License 2.0.
