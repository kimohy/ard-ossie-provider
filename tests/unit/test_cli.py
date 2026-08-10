from __future__ import annotations

import pytest

from ard_ossie.cli import _product_tag, _provider_from_environment
from ard_ossie.pipeline import ProviderExecutionError


def test_openai_compatible_api_style_rejects_unknown_protocol(monkeypatch) -> None:
    monkeypatch.setenv("ARD_LLM_API_STYLE", "responses")

    with pytest.raises(ProviderExecutionError, match="LLM_API_STYLE_UNSUPPORTED"):
        _provider_from_environment()


def test_product_history_reference_resolves_immutable_id_tag(tmp_path) -> None:
    index = tmp_path / "indexes"
    index.mkdir()
    (index / "product-keys.json").write_text(
        '{"sales-order":"prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"}',
        encoding="utf-8",
    )

    assert _product_tag("sales-order", 12, tmp_path) == (
        "product/prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631/v12"
    )
