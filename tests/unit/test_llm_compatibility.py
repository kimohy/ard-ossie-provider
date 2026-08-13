from __future__ import annotations


def test_llm_package_preserves_public_exports() -> None:
    from ard_ossie.llm import (
        AISuggestion,
        LLMProvider,
        MetricSuggestion,
        OpenAICompatibleProvider,
        ProductFactSuggestion,
        ProviderExecutionError,
        ProviderFailureKind,
        semantic_extraction_schema,
        validate_semantic_suggestions,
    )

    assert AISuggestion.__name__ == "AISuggestion"
    assert MetricSuggestion.__name__ == "MetricSuggestion"
    assert ProductFactSuggestion.__name__ == "ProductFactSuggestion"
    assert OpenAICompatibleProvider.__name__ == "OpenAICompatibleProvider"
    assert ProviderExecutionError.__name__ == "ProviderExecutionError"
    assert ProviderFailureKind.TRANSIENT == "transient"
    assert callable(semantic_extraction_schema)
    assert callable(validate_semantic_suggestions)
    assert LLMProvider is not None
