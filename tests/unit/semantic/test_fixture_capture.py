from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceExtractionMode,
    EvidenceRegion,
    ExtractedEvidence,
)
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.structure import StructureDocument
from scripts.verify_issue_3_semantic import capture_evidence, load_evidence_replay


def test_captured_evidence_is_replayable_and_contains_no_image_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = b"public fixture"
    source_hash = hashlib.sha256(snapshot).hexdigest()
    source_path = tmp_path / "fixture.pdf"
    source_path.write_bytes(snapshot)
    source = SourceFile(
        path=source_path,
        relative_path="fixture.pdf",
        role=SourceRole.SEMANTIC_DOCUMENT,
        sha256=source_hash,
        size_bytes=len(snapshot),
        snapshot=snapshot,
    )
    atom = EvidenceAtom(
        atom_id="atom_0000000000000001",
        ordinal=0,
        page=1,
        bbox=SourceBox(left=0.1, bottom=0.1, right=0.2, top=0.2),
        text="가",
        kind="character",
        authority="embedded",
        source_object=0,
        source_index=0,
    )
    evidence = ExtractedEvidence(
        source_hash=source_hash,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=(atom,),
        regions=(
            EvidenceRegion(
                region_id="region_0000000000000001",
                page=1,
                bbox=SourceBox(left=0.1, bottom=0.1, right=0.2, top=0.2),
                atom_ids=(atom.atom_id,),
                authority="embedded",
            ),
        ),
    )
    monkeypatch.setattr(
        "scripts.verify_issue_3_semantic.extract_pdf_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    destination = tmp_path / "evidence.json"

    capture_evidence(
        source,
        destination,
        hints=StructureDocument(blocks=()),
    )
    payload = json.loads(destination.read_text())
    replay = load_evidence_replay(destination)

    assert replay.source_hash == source_hash
    assert "page_images" not in payload
    assert all("image_bytes" not in region for region in payload["regions"])
    assert payload["capture_sha256"]


def test_replay_rejects_tampered_capture_hash(tmp_path: Path) -> None:
    path = tmp_path / "tampered.json"
    path.write_text(
        json.dumps(
            {
                "capture_schema": "semantic-evidence-replay-v1",
                "capture_sha256": "0" * 64,
                "source_hash": "1" * 64,
                "extraction_mode": "pdf_embedded",
                "page_count": 1,
                "parser_versions": {},
                "atoms": [],
                "hypotheses": [],
                "regions": [],
                "structure_hints": [],
            }
        )
    )

    with pytest.raises(ValueError, match="EVIDENCE_REPLAY_HASH_MISMATCH"):
        load_evidence_replay(path)
