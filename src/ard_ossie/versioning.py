from __future__ import annotations

from enum import StrEnum

from ard_ossie.models import StrictModel, Version


class VersionOutcome(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class VersionDecision(StrictModel):
    outcome: VersionOutcome
    code: str
    current_version: Version | None
    base_version: Version | None
    proposed_version: Version | None
    expected_version: Version | None
    changed: bool


def plan_version(
    *,
    current_version: int | None,
    changed: bool,
    base_version: int | None,
    proposed_version: int | None,
) -> VersionDecision:
    """Validate or propose a single numeric entity version transition."""

    _validate_input_version("current_version", current_version)
    _validate_input_version("base_version", base_version)
    _validate_input_version("proposed_version", proposed_version)

    if current_version is None:
        expected = 1
        if base_version is not None:
            return _decision(
                VersionOutcome.BLOCK,
                "VERSION_STALE",
                current_version,
                base_version,
                proposed_version,
                expected,
                changed,
            )
        if proposed_version not in (None, expected):
            return _decision(
                VersionOutcome.BLOCK,
                "VERSION_GAP",
                current_version,
                base_version,
                proposed_version,
                expected,
                changed,
            )
        return _decision(
            VersionOutcome.ALLOW,
            "NEW_V1",
            current_version,
            base_version,
            proposed_version,
            expected,
            changed,
        )

    expected = current_version if not changed else _next_version(current_version)
    if base_version != current_version:
        return _decision(
            VersionOutcome.BLOCK,
            "VERSION_STALE",
            current_version,
            base_version,
            proposed_version,
            expected,
            changed,
        )

    if changed and expected is None:
        return _decision(
            VersionOutcome.BLOCK,
            "VERSION_LIMIT_REACHED",
            current_version,
            base_version,
            proposed_version,
            expected,
            changed,
        )

    if not changed:
        if proposed_version in (None, current_version):
            return _decision(
                VersionOutcome.ALLOW,
                "NO_CHANGE",
                current_version,
                base_version,
                proposed_version,
                expected,
                changed,
            )
        return _decision(
            VersionOutcome.BLOCK,
            "VERSION_NO_CHANGE",
            current_version,
            base_version,
            proposed_version,
            expected,
            changed,
        )

    if proposed_version is None or proposed_version == expected:
        return _decision(
            VersionOutcome.ALLOW,
            "ADVANCE",
            current_version,
            base_version,
            proposed_version,
            expected,
            changed,
        )
    code = "VERSION_COLLISION" if proposed_version <= current_version else "VERSION_GAP"
    return _decision(
        VersionOutcome.BLOCK,
        code,
        current_version,
        base_version,
        proposed_version,
        expected,
        changed,
    )


def _next_version(current: int) -> int | None:
    return None if current == 999 else current + 1


def _validate_input_version(name: str, value: int | None) -> None:
    if value is not None and not 1 <= value <= 999:
        raise ValueError(f"{name} must be between 1 and 999")


def _decision(
    outcome: VersionOutcome,
    code: str,
    current_version: int | None,
    base_version: int | None,
    proposed_version: int | None,
    expected_version: int | None,
    changed: bool,
) -> VersionDecision:
    return VersionDecision(
        outcome=outcome,
        code=code,
        current_version=current_version,
        base_version=base_version,
        proposed_version=proposed_version,
        expected_version=expected_version,
        changed=changed,
    )
