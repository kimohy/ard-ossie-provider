from __future__ import annotations

import importlib
from dataclasses import replace

import pytest

from ard_ossie.semantic.adjudication import DecisionRecord, DecisionReport

SOURCE_HASH = "a" * 64


def _decision(
    decision_type: str,
    ordinal: int,
    *,
    source_hash: str = SOURCE_HASH,
    request_hash: str = "d" * 64,
) -> DecisionRecord:
    suffix = f"{ordinal:016x}"
    return DecisionRecord(
        decision_id=f"decision_{suffix}",
        request_hash=request_hash,
        source_hash=source_hash,
        evidence_hash="e" * 64,
        candidate_set_id=f"candidate_set_{suffix}",
        region_id=f"region_{suffix}",
        decision_type=decision_type,
        selected_candidate_id=f"candidate_{suffix}",
        outcome="selected",
        source="deterministic",
        confidence=1.0,
        provider="deterministic",
        model="deterministic",
    )


def _report(*decisions: DecisionRecord) -> DecisionReport:
    return DecisionReport(source_hash=SOURCE_HASH, decisions=decisions)


def _replay_module():
    return importlib.import_module("ard_ossie.semantic.replay")


def _baseline(product_key: str, markdown: bytes):
    replay = _replay_module()
    report = _report(_decision("spacing", 1))
    return replay.SemanticReplayBaseline(
        product_key=product_key,
        identity=replay.semantic_replay_identity(report),
        canonical_markdown=markdown,
        decisions=report,
    )


def test_replay_identity_is_sorted_and_excludes_product_metadata() -> None:
    replay = _replay_module()
    report = _report(_decision("spacing", 2), _decision("block", 1))

    identity = replay.semantic_replay_identity(report)

    assert identity.source_hash == SOURCE_HASH
    assert [item.decision_type for item in identity.decisions] == ["block", "spacing"]
    assert identity.model_dump(mode="json") == {
        "source_hash": SOURCE_HASH,
        "decisions": [
            {
                "decision_type": "block",
                "region_id": "region_0000000000000001",
                "candidate_set_id": "candidate_set_0000000000000001",
                "request_hash": "d" * 64,
            },
            {
                "decision_type": "spacing",
                "region_id": "region_0000000000000002",
                "candidate_set_id": "candidate_set_0000000000000002",
                "request_hash": "d" * 64,
            },
        ],
    }


def test_replay_identity_rejects_decision_source_mismatch() -> None:
    replay = _replay_module()
    report = _report(_decision("spacing", 1, source_hash="b" * 64))

    with pytest.raises(ValueError, match="SEMANTIC_REPLAY_TRUST_MISMATCH"):
        replay.semantic_replay_identity(report)


def test_catalog_converges_identical_duplicates_using_input_precedence() -> None:
    replay = _replay_module()
    first = _baseline("current", b"canonical\n")
    second = _baseline("alpha", b"canonical\n")

    catalog = replay.SemanticReplayCatalog.build((first, second))

    assert catalog.baselines == (first,)
    assert catalog.trusted_decisions(SOURCE_HASH) == first.decisions.decisions
    assert catalog.canonical_markdown_for(first.decisions) == b"canonical\n"


def test_catalog_rejects_equal_identity_with_different_bytes() -> None:
    replay = _replay_module()

    with pytest.raises(replay.SemanticReplayBaselineConflict):
        replay.SemanticReplayCatalog.build(
            (
                _baseline("alpha", b"canonical\n"),
                _baseline("beta", b"changed\n"),
            )
        )


def test_catalog_rejects_identity_that_does_not_match_decision_report() -> None:
    replay = _replay_module()
    baseline = _baseline("alpha", b"canonical\n")
    changed_report = _report(_decision("spacing", 1, request_hash="f" * 64))

    with pytest.raises(ValueError, match="SEMANTIC_REPLAY_TRUST_MISMATCH"):
        replay.SemanticReplayCatalog.build((replace(baseline, decisions=changed_report),))


def test_catalog_has_no_baseline_for_incompatible_request_identity() -> None:
    replay = _replay_module()
    catalog = replay.SemanticReplayCatalog.build((_baseline("alpha", b"canonical\n"),))
    changed = _report(_decision("spacing", 1, request_hash="f" * 64))

    assert catalog.canonical_markdown_for(changed) is None
