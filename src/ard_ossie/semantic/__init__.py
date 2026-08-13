"""Immutable semantic document parsing and audit contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ard_ossie.semantic.parser import SemanticParseResult, parse_semantic_document

__all__ = ["SemanticParseResult", "parse_semantic_document"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from ard_ossie.semantic import parser

        return getattr(parser, name)
    raise AttributeError(name)
