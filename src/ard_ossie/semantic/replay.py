"""Trusted cross-product replay identities for canonical semantic output."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import Field

from ard_ossie.models import Sha256
from ard_ossie.semantic.adjudication import DecisionRecord, DecisionReport
from ard_ossie.semantic.candidates import CandidateSetId
from ard_ossie.semantic.evidence import RegionId
from ard_ossie.semantic.models import ImmutableStrictModel


class SemanticDecisionIdentity(ImmutableStrictModel):
    decision_type: str = Field(min_length=1, max_length=40)
    region_id: RegionId
    candidate_set_id: CandidateSetId
    request_hash: Sha256


class SemanticReplayIdentity(ImmutableStrictModel):
    source_hash: Sha256
    decisions: tuple[SemanticDecisionIdentity, ...]


@dataclass(frozen=True)
class SemanticReplayBaseline:
    product_key: str
    identity: SemanticReplayIdentity
    canonical_markdown: bytes
    decisions: DecisionReport


@dataclass(frozen=True)
class SemanticReplayCatalog:
    baselines: tuple[SemanticReplayBaseline, ...] = ()

    @classmethod
    def build(
        cls,
        entries: Iterable[SemanticReplayBaseline],
    ) -> SemanticReplayCatalog:
        grouped: dict[SemanticReplayIdentity, SemanticReplayBaseline] = {}
        for entry in entries:
            if entry.identity != semantic_replay_identity(entry.decisions):
                raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
            existing = grouped.get(entry.identity)
            if existing is None:
                grouped[entry.identity] = entry
            elif existing.canonical_markdown != entry.canonical_markdown:
                raise ValueError("SEMANTIC_REPLAY_BASELINE_CONFLICT")
        return cls(tuple(grouped.values()))

    def trusted_decisions(self, source_hash: Sha256) -> tuple[DecisionRecord, ...]:
        return tuple(
            decision
            for baseline in self.baselines
            if baseline.identity.source_hash == source_hash
            for decision in baseline.decisions.decisions
        )

    def canonical_markdown_for(self, report: DecisionReport) -> bytes | None:
        identity = semantic_replay_identity(report)
        return next(
            (
                baseline.canonical_markdown
                for baseline in self.baselines
                if baseline.identity == identity
            ),
            None,
        )


def semantic_replay_identity(report: DecisionReport) -> SemanticReplayIdentity:
    if any(decision.source_hash != report.source_hash for decision in report.decisions):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    decisions = tuple(
        sorted(
            (
                SemanticDecisionIdentity(
                    decision_type=decision.decision_type,
                    region_id=decision.region_id,
                    candidate_set_id=decision.candidate_set_id,
                    request_hash=decision.request_hash,
                )
                for decision in report.decisions
            ),
            key=lambda item: (
                item.decision_type,
                item.region_id,
                item.candidate_set_id,
                item.request_hash,
            ),
        )
    )
    if len(decisions) != len(set(decisions)):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    return SemanticReplayIdentity(source_hash=report.source_hash, decisions=decisions)
