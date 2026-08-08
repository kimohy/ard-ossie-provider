from __future__ import annotations

import pytest

from ard_ossie.versioning import VersionOutcome, plan_version


@pytest.mark.parametrize(
    ("current", "changed", "base", "proposed", "outcome", "code", "expected"),
    [
        (None, True, None, 1, VersionOutcome.ALLOW, "NEW_V1", 1),
        (11, True, 11, 12, VersionOutcome.ALLOW, "ADVANCE", 12),
        (11, True, 11, None, VersionOutcome.ALLOW, "ADVANCE", 12),
        (11, False, 11, 11, VersionOutcome.ALLOW, "NO_CHANGE", 11),
        (11, False, 11, None, VersionOutcome.ALLOW, "NO_CHANGE", 11),
        (11, True, 10, 11, VersionOutcome.BLOCK, "VERSION_STALE", 12),
        (11, True, 11, 13, VersionOutcome.BLOCK, "VERSION_GAP", 12),
        (11, True, 11, 11, VersionOutcome.BLOCK, "VERSION_COLLISION", 12),
        (11, False, 11, 12, VersionOutcome.BLOCK, "VERSION_NO_CHANGE", 11),
        (999, True, 999, None, VersionOutcome.BLOCK, "VERSION_LIMIT_REACHED", None),
    ],
)
def test_numeric_version_transition(
    current: int | None,
    changed: bool,
    base: int | None,
    proposed: int | None,
    outcome: VersionOutcome,
    code: str,
    expected: int | None,
) -> None:
    decision = plan_version(
        current_version=current,
        changed=changed,
        base_version=base,
        proposed_version=proposed,
    )

    assert decision.outcome is outcome
    assert decision.code == code
    assert decision.expected_version == expected


def test_new_entity_rejects_any_version_other_than_v1() -> None:
    decision = plan_version(
        current_version=None,
        changed=True,
        base_version=None,
        proposed_version=2,
    )

    assert decision.outcome is VersionOutcome.BLOCK
    assert decision.code == "VERSION_GAP"
    assert decision.expected_version == 1


def test_existing_entity_requires_current_base_even_when_content_is_unchanged() -> None:
    decision = plan_version(
        current_version=8,
        changed=False,
        base_version=7,
        proposed_version=8,
    )

    assert decision.outcome is VersionOutcome.BLOCK
    assert decision.code == "VERSION_STALE"
