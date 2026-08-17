# Same-Source Semantic Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse a verified base-revision semantic decision history across products with the same semantic source and reject any byte-level canonical Markdown divergence.

**Architecture:** Add an immutable semantic replay identity/catalog in the semantic layer, and a revision-pinned loader in the application layer. `ProcessingService` scans the candidate semantic source, builds the catalog exclusively from the exact remote base SHA, and passes it through the parser; the candidate pipeline reuses matching decisions and turns a compatible byte mismatch into an ordinary hard semantic finding before generated or Registry promotion.

**Tech Stack:** Python 3.11+, Pydantic v2 strict models, pytest, Ruff, existing `GitPort`, semantic candidate pipeline, and atomic promotion pipeline.

## Global Constraints

- Replay identity is the semantic source SHA-256 plus the sorted complete set of `(decision_type, region_id, candidate_set_id, request_hash)` tuples.
- Product ID, product key, display metadata, and product configuration are excluded from replay identity.
- Only artifacts read with `GitPort.read_bytes_at(base_sha, path)` may establish replay authority.
- Compatible replay output must match the trusted canonical Markdown byte-for-byte, including spaces, wrapping, and final newline.
- A compatible second product must make zero semantic adjudication provider calls.
- Missing whole quality history is ineligible; partial, malformed, hash-mismatched, or source-inconsistent matching history is a bounded trust failure.
- Diagnostics must not expose source text, prompts, provider responses, credentials, or signed URLs.
- No generated product artifact or Registry file may be edited by hand.

---

## File Structure

- Create `src/ard_ossie/semantic/replay.py`: replay identity, immutable baseline/catalog, deterministic deduplication, conflict detection, and byte lookup.
- Create `src/ard_ossie/application/semantic_replay.py`: exact-base product enumeration, strict artifact parsing, hash/source/status verification, and bounded workflow-error translation.
- Modify `src/ard_ossie/application/processing.py`: scan the current semantic source, load the catalog at the remote base SHA, and pass it to `process_product`.
- Modify `src/ard_ossie/pipeline.py`: accept the catalog, thread it into `DoclingParser`, and surface replay mismatch as the exact hard quality code.
- Modify `src/ard_ossie/docling_parser.py`: carry the catalog into semantic parsing.
- Modify `src/ard_ossie/semantic/parser.py`: carry the catalog into the PDF candidate pipeline.
- Modify `src/ard_ossie/semantic/pipeline_v2.py`: flatten trusted decisions before adjudication and enforce byte equality after canonical rendering.
- Create `tests/unit/test_semantic_replay.py`: domain identity/catalog tests.
- Create `tests/unit/test_semantic_replay_loader.py`: exact-revision loader and trust-boundary tests.
- Modify `tests/unit/test_processing_service.py`: service wiring and bounded error mapping tests.
- Modify `tests/integration/test_semantic_pdf_regressions.py`: Issue #3 same-source, zero-call, and bad-spacing regression.
- Modify `tests/integration/test_semantic_pdf_v2.py`: incompatible identity and forced mismatch behavior.
- Modify `tests/integration/test_atomic_promotion.py`: replay-specific failure diagnostics and no-promotion guarantee.
- Modify `scripts/verify_issue_3_semantic.py`: optional replay catalog/provider injection for the captured Issue #3 evidence test.

### Task 1: Define replay identity and catalog semantics

**Files:**
- Create: `src/ard_ossie/semantic/replay.py`
- Create: `tests/unit/test_semantic_replay.py`

**Interfaces:**
- Consumes: `DecisionReport` and `DecisionRecord` from `ard_ossie.semantic.adjudication`.
- Produces: `SemanticDecisionIdentity`, `SemanticReplayIdentity`, `SemanticReplayBaseline`, `SemanticReplayCatalog`, `semantic_replay_identity(report)`, `trusted_decisions(source_hash)`, and `canonical_markdown_for(report)`.

- [x] **Step 1: Write failing identity tests**

```python
SOURCE_HASH = "a" * 64


def decision(
    decision_type: str,
    ordinal: str,
    *,
    source_hash: str = SOURCE_HASH,
    request_hash: str = "d" * 64,
) -> DecisionRecord:
    suffix = f"{int(ordinal):016x}"
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


def decision_report(*, decisions: tuple[DecisionRecord, ...]) -> DecisionReport:
    return DecisionReport(source_hash=SOURCE_HASH, decisions=decisions)


def baseline(product_key: str, *, markdown: bytes) -> SemanticReplayBaseline:
    report = decision_report(decisions=(decision("spacing", "1"),))
    return SemanticReplayBaseline(
        product_key=product_key,
        identity=semantic_replay_identity(report),
        canonical_markdown=markdown,
        decisions=report,
    )


def test_replay_identity_is_sorted_and_ignores_product_metadata() -> None:
    report = decision_report(decisions=(decision("spacing", "2"), decision("block", "1")))

    identity = semantic_replay_identity(report)

    assert identity.source_hash == SOURCE_HASH
    assert [item.decision_type for item in identity.decisions] == ["block", "spacing"]
    assert "product" not in identity.model_dump_json()


def test_replay_identity_rejects_decision_source_mismatch() -> None:
    report = decision_report(decisions=(decision("spacing", "1", source_hash="b" * 64),))

    with pytest.raises(ValueError, match="SEMANTIC_REPLAY_TRUST_MISMATCH"):
        semantic_replay_identity(report)
```

- [x] **Step 2: Run the identity tests and confirm the missing module failure**

Run: `uv run pytest tests/unit/test_semantic_replay.py -q`

Expected: collection fails because `ard_ossie.semantic.replay` does not exist.

- [x] **Step 3: Implement immutable replay identities**

```python
class SemanticDecisionIdentity(ImmutableStrictModel):
    decision_type: str = Field(min_length=1, max_length=40)
    region_id: RegionId
    candidate_set_id: CandidateSetId
    request_hash: Sha256


class SemanticReplayIdentity(ImmutableStrictModel):
    source_hash: Sha256
    decisions: tuple[SemanticDecisionIdentity, ...]


def semantic_replay_identity(report: DecisionReport) -> SemanticReplayIdentity:
    if any(item.source_hash != report.source_hash for item in report.decisions):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    identities = tuple(
        sorted(
            (
                SemanticDecisionIdentity(
                    decision_type=item.decision_type,
                    region_id=item.region_id,
                    candidate_set_id=item.candidate_set_id,
                    request_hash=item.request_hash,
                )
                for item in report.decisions
            ),
            key=lambda item: (
                item.decision_type,
                item.region_id,
                item.candidate_set_id,
                item.request_hash,
            ),
        )
    )
    if len(identities) != len(set(identities)):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    return SemanticReplayIdentity(source_hash=report.source_hash, decisions=identities)
```

- [x] **Step 4: Run identity tests and confirm they pass**

Run: `uv run pytest tests/unit/test_semantic_replay.py -q`

Expected: all identity tests pass.

- [x] **Step 5: Write failing catalog convergence and conflict tests**

```python
def test_catalog_converges_identical_duplicate_baselines() -> None:
    first = baseline("alpha", markdown=b"canonical\n")
    second = baseline("beta", markdown=b"canonical\n")

    catalog = SemanticReplayCatalog.build((first, second))

    assert catalog.baselines == (first,)
    assert catalog.trusted_decisions(SOURCE_HASH) == first.decisions.decisions
    assert catalog.canonical_markdown_for(first.decisions) == b"canonical\n"


def test_catalog_rejects_equal_identity_with_different_bytes() -> None:
    with pytest.raises(ValueError, match="SEMANTIC_REPLAY_BASELINE_CONFLICT"):
        SemanticReplayCatalog.build(
            (baseline("alpha", markdown=b"canonical\n"), baseline("beta", markdown=b"changed\n"))
        )


def test_catalog_rejects_baseline_identity_that_does_not_match_its_report() -> None:
    entry = baseline("alpha", markdown=b"canonical\n")
    changed_report = decision_report(
        decisions=(decision("spacing", "1", request_hash="f" * 64),)
    )

    with pytest.raises(ValueError, match="SEMANTIC_REPLAY_TRUST_MISMATCH"):
        SemanticReplayCatalog.build((replace(entry, decisions=changed_report),))


def test_catalog_returns_none_for_incompatible_request_identity() -> None:
    catalog = SemanticReplayCatalog.build((baseline("alpha", markdown=b"canonical\n"),))
    changed = decision_report(decisions=(decision("spacing", "1", request_hash="f" * 64),))

    assert catalog.canonical_markdown_for(changed) is None
```

- [x] **Step 6: Implement deterministic baseline grouping and lookup**

```python
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
    def build(cls, entries: Iterable[SemanticReplayBaseline]) -> SemanticReplayCatalog:
        grouped: dict[SemanticReplayIdentity, list[SemanticReplayBaseline]] = {}
        for entry in entries:
            if entry.identity != semantic_replay_identity(entry.decisions):
                raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
            grouped.setdefault(entry.identity, []).append(entry)
        selected: list[SemanticReplayBaseline] = []
        for identity, matches in sorted(grouped.items(), key=lambda item: item[0].model_dump_json()):
            if len({item.canonical_markdown for item in matches}) != 1:
                raise ValueError("SEMANTIC_REPLAY_BASELINE_CONFLICT")
            selected.append(matches[0])
        return cls(tuple(selected))

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
            (item.canonical_markdown for item in self.baselines if item.identity == identity),
            None,
        )
```

- [x] **Step 7: Run the domain tests and commit**

Run: `uv run pytest tests/unit/test_semantic_replay.py -q`

Expected: all tests pass.

```bash
git add src/ard_ossie/semantic/replay.py tests/unit/test_semantic_replay.py
git commit -m "feat: define semantic replay catalog"
```

### Task 2: Load and verify trusted baselines from the exact base revision

**Files:**
- Create: `src/ard_ossie/application/semantic_replay.py`
- Create: `tests/unit/test_semantic_replay_loader.py`

**Interfaces:**
- Consumes: `GitPort.read_bytes_at`, `QualityReport`, `DecisionReport`, `SemanticValidationReport`, the base SHA, current product key, and current semantic source hash.
- Produces: `load_semantic_replay_catalog(git, *, base_sha, product_key, semantic_source_hash) -> SemanticReplayCatalog`.
- Raises: `WorkflowSecurityError("SEMANTIC_REPLAY_TRUST_MISMATCH", bounded_message)` or `WorkflowValidationError("SEMANTIC_REPLAY_BASELINE_CONFLICT", bounded_message)`.

- [x] **Step 1: Write exact-revision success tests**

```python
BASE_SHA = "1" * 40
SOURCE_HASH = "a" * 64
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
CANONICAL_BYTES = "Data Semantics 정의서이며\n".encode()


def verified_decision_report(*, source_hash: str = SOURCE_HASH) -> DecisionReport:
    decision = DecisionRecord(
        decision_id="decision_0000000000000001",
        request_hash="d" * 64,
        source_hash=source_hash,
        evidence_hash="e" * 64,
        candidate_set_id="candidate_set_0000000000000001",
        region_id="region_0000000000000001",
        decision_type="spacing",
        selected_candidate_id="candidate_0000000000000001",
        outcome="selected",
        source="deterministic",
        confidence=1.0,
        provider="deterministic",
        model="deterministic",
    )
    return DecisionReport(source_hash=source_hash, decisions=(decision,))


DECISION_REPORT = verified_decision_report()


def verified_tree(
    *,
    keys: tuple[str, ...] = ("current",),
    source_hash: str = SOURCE_HASH,
    mutation: str | None = None,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "registry/indexes/product-keys.json": json.dumps(
            {key: PRODUCT_ID for key in keys}, sort_keys=True
        ).encode()
    }
    for key in keys:
        manifest = json.dumps(
            {
                "files": [
                    {
                        "role": "semantic_document",
                        "relative_path": "sources/semantic/semantic.pdf",
                        "sha256": source_hash,
                        "size_bytes": 7,
                    }
                ]
            },
            sort_keys=True,
        ).encode()
        markdown = CANONICAL_BYTES
        decisions = (verified_decision_report(source_hash=source_hash).model_dump_json() + "\n").encode()
        validation_model = SemanticValidationReport(
            status="verified",
            publishable=True,
            source_hash=source_hash,
            canonical_hash="c" * 64,
            findings=[],
            character_coverage=1.0,
            missing_atom_count=0,
            duplicate_atom_count=0,
            degraded_block_count=0,
            model_call_count=0,
        )
        validation = (validation_model.model_dump_json() + "\n").encode()
        quality = QualityReport(
            status="PASS",
            product_id=PRODUCT_ID,
            product_version=1,
            completeness=1.0,
            hard_errors=[],
            warnings=[],
            artifact_hashes={
                "source-manifest.json": hashlib.sha256(manifest).hexdigest(),
                "data-semantic.md": hashlib.sha256(markdown).hexdigest(),
            },
            quality_artifact_hashes={
                "decision-report.json": hashlib.sha256(decisions).hexdigest(),
                "validation-report.json": hashlib.sha256(validation).hexdigest(),
            },
        )
        root = f"products/{key}"
        files.update(
            {
                f"{root}/generated/source-manifest.json": manifest,
                f"{root}/generated/data-semantic.md": markdown,
                f"{root}/quality/quality-report.json": (
                    quality.model_dump_json() + "\n"
                ).encode(),
                f"{root}/quality/decision-report.json": decisions,
                f"{root}/quality/validation-report.json": validation,
            }
        )
    return mutate_verified_tree(files, mutation=mutation)


class FakeGit:
    def __init__(self, base_sha: str, files: dict[str, bytes]) -> None:
        self.base_sha = base_sha
        self.files = files
        self.reads: list[tuple[str, str]] = []

    def read_bytes_at(self, revision: str, path: str | Path) -> bytes:
        relative = Path(path).as_posix()
        self.reads.append((revision, relative))
        if revision != self.base_sha or relative not in self.files:
            raise WorkflowConflict("REVISION_FILE_NOT_FOUND", relative)
        return self.files[relative]

    @property
    def product_read_order(self) -> list[str]:
        return [
            Path(path).parts[1]
            for _revision, path in self.reads
            if path.endswith("generated/source-manifest.json")
        ]


def test_loader_enumerates_current_product_then_lexical_keys_at_exact_base_sha() -> None:
    git = FakeGit(BASE_SHA, verified_tree(keys=("zeta", "current", "alpha")))

    catalog = load_semantic_replay_catalog(
        git,
        base_sha=BASE_SHA,
        product_key="current",
        semantic_source_hash=SOURCE_HASH,
    )

    assert catalog.canonical_markdown_for(DECISION_REPORT) == CANONICAL_BYTES
    assert git.reads[0] == (BASE_SHA, "registry/indexes/product-keys.json")
    assert git.product_read_order == ["current", "alpha", "zeta"]


def test_loader_skips_complete_history_for_a_different_semantic_source() -> None:
    git = FakeGit(BASE_SHA, verified_tree(source_hash="c" * 64))

    catalog = load_semantic_replay_catalog(
        git,
        base_sha=BASE_SHA,
        product_key="new-product",
        semantic_source_hash=SOURCE_HASH,
    )

    assert catalog.baselines == ()
```

Use these test-only byte transformers for the mutation matrix and conflict fixture:

```python
def replace_hashed_payload(
    files: dict[str, bytes],
    *,
    product_key: str,
    report_name: str,
    payload: bytes,
    generated: bool = False,
) -> None:
    root = f"products/{product_key}"
    directory = "generated" if generated else "quality"
    files[f"{root}/{directory}/{report_name}"] = payload
    quality_path = f"{root}/quality/quality-report.json"
    quality = json.loads(files[quality_path])
    hash_field = "artifact_hashes" if generated else "quality_artifact_hashes"
    quality[hash_field][report_name] = hashlib.sha256(payload).hexdigest()
    files[quality_path] = (json.dumps(quality, sort_keys=True) + "\n").encode()


def mutate_verified_tree(
    files: dict[str, bytes],
    *,
    mutation: str | None,
) -> dict[str, bytes]:
    if mutation is None:
        return files
    key = next(iter(json.loads(files["registry/indexes/product-keys.json"])))
    root = f"products/{key}"
    if mutation == "missing_decision_report":
        del files[f"{root}/quality/decision-report.json"]
    elif mutation == "tampered_markdown":
        files[f"{root}/generated/data-semantic.md"] += b" "
    elif mutation == "tampered_validation":
        files[f"{root}/quality/validation-report.json"] += b" "
    elif mutation == "invalid_manifest_utf8":
        files[f"{root}/generated/source-manifest.json"] = b"{\xff}"
    elif mutation == "decision_source_mismatch":
        report = verified_decision_report()
        report = report.model_copy(
            update={
                "decisions": (
                    report.decisions[0].model_copy(update={"source_hash": "b" * 64}),
                )
            }
        )
        replace_hashed_payload(
            files,
            product_key=key,
            report_name="decision-report.json",
            payload=(report.model_dump_json() + "\n").encode(),
        )
    elif mutation in {"validation_source_mismatch", "unverified_validation"}:
        validation_path = f"{root}/quality/validation-report.json"
        validation = SemanticValidationReport.model_validate_json(files[validation_path])
        update = (
            {"source_hash": "b" * 64}
            if mutation == "validation_source_mismatch"
            else {
                "status": SemanticPipelineStatus.REVIEW_REQUIRED,
                "publishable": False,
            }
        )
        replace_hashed_payload(
            files,
            product_key=key,
            report_name="validation-report.json",
            payload=(validation.model_copy(update=update).model_dump_json() + "\n").encode(),
        )
    else:
        raise AssertionError(mutation)
    return files


def conflicting_verified_tree() -> dict[str, bytes]:
    files = verified_tree(keys=("alpha", "beta"))
    replace_hashed_payload(
        files,
        product_key="beta",
        report_name="data-semantic.md",
        payload="Data Semantics 정의 서이며\n".encode(),
        generated=True,
    )
    return files
```

- [x] **Step 2: Run loader tests and confirm the missing module failure**

Run: `uv run pytest tests/unit/test_semantic_replay_loader.py -q`

Expected: collection fails because the application loader does not exist.

- [x] **Step 3: Add strict stored-manifest models and revision readers**

```python
class StoredSourceFile(ImmutableStrictModel):
    role: SourceRole
    relative_path: str = Field(min_length=1)
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class StoredSourceManifest(ImmutableStrictModel):
    files: tuple[StoredSourceFile, ...]

    def semantic_hash(self) -> Sha256:
        matches = [item.sha256 for item in self.files if item.role is SourceRole.SEMANTIC_DOCUMENT]
        if len(matches) != 1:
            raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
        return matches[0]


class ProductKeyIndex(RootModel[dict[ProductKey, ProductId]]):
    pass


def _read_optional(git: GitPort, base_sha: str, path: Path) -> bytes | None:
    try:
        return git.read_bytes_at(base_sha, path)
    except WorkflowConflict as error:
        if error.code == "REVISION_FILE_NOT_FOUND":
            return None
        raise


def _verify_hash(hashes: Mapping[str, Sha256], name: str, payload: bytes) -> None:
    if hashes.get(name) != hashlib.sha256(payload).hexdigest():
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
```

- [x] **Step 4: Implement strict artifact and hash validation**

```python
def _load_matching_baseline(
    git: GitPort,
    *,
    base_sha: str,
    product_key: str,
    semantic_source_hash: Sha256,
) -> SemanticReplayBaseline | None:
    root = Path("products") / product_key
    manifest_bytes = _read_optional(git, base_sha, root / "generated/source-manifest.json")
    if manifest_bytes is None:
        return None
    manifest = StoredSourceManifest.model_validate_json(manifest_bytes)
    if manifest.semantic_hash() != semantic_source_hash:
        return None
    paths = {
        "markdown": root / "generated/data-semantic.md",
        "quality": root / "quality/quality-report.json",
        "decisions": root / "quality/decision-report.json",
        "validation": root / "quality/validation-report.json",
    }
    payloads = {name: _read_optional(git, base_sha, path) for name, path in paths.items()}
    if all(payloads[name] is None for name in ("quality", "decisions", "validation")):
        return None
    if any(value is None for value in payloads.values()):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    quality = QualityReport.model_validate_json(payloads["quality"])
    decisions = DecisionReport.model_validate_json(payloads["decisions"])
    validation = SemanticValidationReport.model_validate_json(payloads["validation"])
    _verify_hash(quality.artifact_hashes, "source-manifest.json", manifest_bytes)
    _verify_hash(quality.artifact_hashes, "data-semantic.md", payloads["markdown"])
    _verify_hash(quality.quality_artifact_hashes, "decision-report.json", payloads["decisions"])
    _verify_hash(quality.quality_artifact_hashes, "validation-report.json", payloads["validation"])
    if quality.hard_errors or validation.status is not SemanticPipelineStatus.VERIFIED:
        return None
    if not validation.publishable or decisions.source_hash != semantic_source_hash:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    if validation.source_hash != semantic_source_hash:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    return SemanticReplayBaseline(
        product_key=product_key,
        identity=semantic_replay_identity(decisions),
        canonical_markdown=payloads["markdown"],
        decisions=decisions,
    )
```

- [x] **Step 5: Implement index ordering and bounded error translation**

```python
def load_semantic_replay_catalog(
    git: GitPort,
    *,
    base_sha: str,
    product_key: str,
    semantic_source_hash: Sha256,
) -> SemanticReplayCatalog:
    try:
        if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
            raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
        index = ProductKeyIndex.model_validate_json(
            git.read_bytes_at(base_sha, Path("registry/indexes/product-keys.json"))
        )
        keys = tuple(
            key for key in (product_key, *sorted(index.root)) if key in index.root
        )
        entries = tuple(
            entry
            for key in dict.fromkeys(keys)
            if (
                entry := _load_matching_baseline(
                    git,
                    base_sha=base_sha,
                    product_key=key,
                    semantic_source_hash=semantic_source_hash,
                )
            ) is not None
        )
        return SemanticReplayCatalog.build(entries)
    except (ValueError, WorkflowConflict) as error:
        code = str(error).partition(":")[0]
        if code == "SEMANTIC_REPLAY_BASELINE_CONFLICT":
            raise WorkflowValidationError(code, "trusted semantic baselines conflict") from None
        raise WorkflowSecurityError(
            "SEMANTIC_REPLAY_TRUST_MISMATCH",
            "trusted semantic replay artifacts failed verification",
        ) from None
```

- [x] **Step 6: Add tampering, partial-tree, invalid UTF-8, and conflict tests**

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_decision_report",
        "tampered_markdown",
        "tampered_validation",
        "invalid_manifest_utf8",
        "decision_source_mismatch",
        "validation_source_mismatch",
    ],
)
def test_loader_rejects_untrusted_matching_history(mutation: str) -> None:
    git = FakeGit(BASE_SHA, verified_tree(mutation=mutation))

    with pytest.raises(WorkflowSecurityError, match="SEMANTIC_REPLAY_TRUST_MISMATCH"):
        load_semantic_replay_catalog(
            git,
            base_sha=BASE_SHA,
            product_key="current",
            semantic_source_hash=SOURCE_HASH,
        )


def test_loader_maps_duplicate_byte_conflict_without_source_disclosure() -> None:
    git = FakeGit(BASE_SHA, conflicting_verified_tree())

    with pytest.raises(WorkflowValidationError) as captured:
        load_semantic_replay_catalog(
            git,
            base_sha=BASE_SHA,
            product_key="new-product",
            semantic_source_hash=SOURCE_HASH,
        )

    assert captured.value.code == "SEMANTIC_REPLAY_BASELINE_CONFLICT"
    assert "정의" not in str(captured.value)


def test_loader_ignores_complete_but_unverified_history() -> None:
    git = FakeGit(BASE_SHA, verified_tree(mutation="unverified_validation"))

    catalog = load_semantic_replay_catalog(
        git,
        base_sha=BASE_SHA,
        product_key="current",
        semantic_source_hash=SOURCE_HASH,
    )

    assert catalog.baselines == ()
```

- [x] **Step 7: Run loader tests and commit**

Run: `uv run pytest tests/unit/test_semantic_replay_loader.py -q`

Expected: all tests pass.

```bash
git add src/ard_ossie/application/semantic_replay.py tests/unit/test_semantic_replay_loader.py
git commit -m "feat: load trusted semantic replay baselines"
```

### Task 3: Wire exact-base replay into `ProcessingService`

**Files:**
- Modify: `src/ard_ossie/application/processing.py:93-170`
- Modify: `tests/unit/test_processing_service.py`

**Interfaces:**
- Consumes: `scan_sources(product / "sources")`, `SourceRole.SEMANTIC_DOCUMENT`, and `load_semantic_replay_catalog`.
- Produces: processor keyword `trusted_semantic_replay_catalog: SemanticReplayCatalog` in addition to the existing product-local repair/fidelity/decision keywords.

- [x] **Step 1: Write failing service wiring tests**

```python
class FakeGit:
    def __init__(
        self,
        *,
        base_sha: str = OLD_SHA,
        revision_files: dict[str, str | bytes] | None = None,
    ) -> None:
        self.sha = OLD_SHA
        self.remote_sha = OLD_SHA
        self.base_sha = base_sha
        self.revision_files = {
            "registry/indexes/product-keys.json": json.dumps(
                {"sales-order": PRODUCT_ID}
            ),
            **(revision_files or {}),
        }
        self.revision_reads: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, bool]] = []


def repository(tmp_path: Path) -> None:
    product = tmp_path / "products" / "sales-order"
    product.mkdir(parents=True)
    (product / "product.yaml").write_text(
        f"product_id: {PRODUCT_ID}\nversion: 1\nchangeset_id:\n",
        encoding="utf-8",
    )
    for directory, name, payload in (
        ("product-info", "product.html", b"<h1>Product</h1>"),
        ("semantic", "semantic.pdf", b"same-source"),
        ("dictionary", "dictionary.xlsx", b"dictionary"),
    ):
        target = product / "sources" / directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (tmp_path / "registry").mkdir()


def test_processing_builds_replay_catalog_from_remote_base_and_candidate_source(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit.with_revision_files(base_sha=NEW_SHA, files=verified_replay_tree())
    service, captured = capturing_processing_service(tmp_path, git=git)

    service.run(request(tmp_path))

    catalog = cast(SemanticReplayCatalog, captured["trusted_semantic_replay_catalog"])
    assert catalog.baselines[0].identity.source_hash == hashlib.sha256(b"same-source").hexdigest()
    assert all(revision == NEW_SHA for revision, _path in git.revision_reads)


def test_processing_maps_replay_trust_failure_to_security_exit_contract(tmp_path: Path) -> None:
    service = service_with_tampered_replay_tree(tmp_path)

    with pytest.raises(WorkflowSecurityError) as captured:
        service.run(request(tmp_path))

    assert captured.value.code == "SEMANTIC_REPLAY_TRUST_MISMATCH"
    assert captured.value.exit_code is ExitCode.SECURITY
```

Update the two existing exact `revision_reads` assertions to retain their current product-local reads and append these catalog reads in this order:

```python
(
    base_sha,
    "registry/indexes/product-keys.json",
),
(
    base_sha,
    "products/sales-order/generated/source-manifest.json",
),
```

This keeps all pre-existing service tests on the exact-revision path while making an absent replay manifest an eligible empty catalog, not a checkout fallback.

- [x] **Step 2: Run the focused service tests and confirm they fail**

Run: `uv run pytest tests/unit/test_processing_service.py -q -k 'replay_catalog or replay_trust'`

Expected: failures because the processor does not receive the catalog.

- [x] **Step 3: Scan the semantic source and pass the catalog to the processor**

```python
base_sha = self.git.remote_branch_sha(pull_request.base_branch)
if base_sha is None:
    raise WorkflowTransientError("PROCESSING_BASE_SHA_MISSING", "remote base is unavailable")
semantic_source_hash = scan_sources(product / "sources").by_role(
    SourceRole.SEMANTIC_DOCUMENT
).sha256
trusted_semantic_replay_catalog = load_semantic_replay_catalog(
    self.git,
    base_sha=base_sha,
    product_key=request.product_key,
    semantic_source_hash=semantic_source_hash,
)
processed = self.processor(
    product,
    registry_root=registry,
    provider=provider,
    pr_number=request.pr_number,
    warnings_as_errors=request.warnings_as_errors,
    trusted_semantic_repair=trusted_semantic_repair,
    trusted_semantic_fidelity=trusted_semantic_fidelity,
    trusted_semantic_decisions=trusted_semantic_decisions,
    trusted_semantic_replay_catalog=trusted_semantic_replay_catalog,
    semantic_pipeline_mode=request.semantic_pipeline_mode,
)
```

- [x] **Step 4: Run all processing-service tests and commit**

Run: `uv run pytest tests/unit/test_processing_service.py -q`

Expected: all tests pass, including existing product-local trusted artifact tests.

```bash
git add src/ard_ossie/application/processing.py tests/unit/test_processing_service.py
git commit -m "feat: supply cross-product replay catalog"
```

### Task 4: Reuse catalog decisions and enforce byte equality in the candidate pipeline

**Files:**
- Modify: `src/ard_ossie/pipeline.py:192-225,500-535,1171-1205`
- Modify: `src/ard_ossie/docling_parser.py:24-90`
- Modify: `src/ard_ossie/semantic/parser.py:48-170`
- Modify: `src/ard_ossie/semantic/pipeline_v2.py:78-250`
- Modify: `tests/integration/test_semantic_pdf_v2.py`

**Interfaces:**
- Consumes: `SemanticReplayCatalog.trusted_decisions(source.sha256)` and `canonical_markdown_for(decision_report)`.
- Produces: `ValidationFinding(code="SEMANTIC_SOURCE_REPLAY_MISMATCH", ...)` with `status=failed` and `publishable=false` on a compatible byte mismatch.

- [x] **Step 1: Write failing pipeline replay tests**

```python
def test_compatible_catalog_reuses_decisions_without_provider_calls(tmp_path: Path) -> None:
    first = run_fixture(tmp_path, provider=CountingProvider())
    catalog = catalog_for("base-product", first)
    provider = CountingProvider()

    replayed = run_fixture(
        tmp_path,
        provider=provider,
        trusted_semantic_replay_catalog=catalog,
    )

    assert provider.calls == 0
    assert replayed.canonical_markdown.encode() == first.canonical_markdown.encode()


def test_compatible_catalog_rejects_byte_mismatch(tmp_path: Path) -> None:
    first = run_fixture(tmp_path, provider=CountingProvider())
    catalog = catalog_for("base-product", first, markdown=b"different\n")

    replayed = run_fixture(
        tmp_path,
        provider=CountingProvider(),
        trusted_semantic_replay_catalog=catalog,
    )

    assert replayed.validation.status == "failed"
    assert replayed.validation.publishable is False
    assert [item.code for item in replayed.validation.findings][-1] == (
        "SEMANTIC_SOURCE_REPLAY_MISMATCH"
    )


def test_changed_request_identity_uses_fresh_adjudication(tmp_path: Path) -> None:
    first = run_fixture(tmp_path, provider=CountingProvider())
    catalog = catalog_for("base-product", first)
    provider = ChangedIdentityProvider()

    replayed = run_fixture(
        tmp_path,
        provider=provider,
        trusted_semantic_replay_catalog=catalog,
    )

    assert provider.calls > 0
    assert replayed.validation.status == "verified"
```

Place these concrete helpers above the tests:

```python
class CountingProvider(DeferredSpacingProvider):
    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        del schema
        self.calls += 1
        request = json.loads(messages[-1]["content"])
        if request.get("task") in {
            "generate_whitespace_repair",
            "verify_whitespace_repair",
        }:
            return super().generate_structured(schema={}, messages=messages)
        selected = max(
            request["candidates"],
            key=lambda item: (item["score"], item["candidate_id"]),
        )
        structured = {
            "candidate_id": selected["candidate_id"],
            "confidence": 0.99,
        }
        capabilities = self.capabilities()
        return LLMResult(
            text=json.dumps(structured),
            structured=structured,
            metadata=LLMMetadata(
                profile="semantic-fixture",
                provider=str(capabilities["provider"]),
                model=str(capabilities["model"]),
                elapsed_ms=0,
            ),
        )


class ChangedIdentityProvider(CountingProvider):
    def capabilities(self) -> dict[str, object]:
        return {"provider": "openai_compatible", "model": "changed-semantic-policy"}


def catalog_for(
    product_key: str,
    result: SemanticPipelineResult,
    *,
    markdown: bytes | None = None,
) -> SemanticReplayCatalog:
    return SemanticReplayCatalog.build(
        (
            SemanticReplayBaseline(
                product_key=product_key,
                identity=semantic_replay_identity(result.decisions),
                canonical_markdown=(
                    result.canonical_markdown.encode("utf-8")
                    if markdown is None
                    else markdown
                ),
                decisions=result.decisions,
            ),
        )
    )


def run_fixture(
    tmp_path: Path,
    *,
    provider: CountingProvider | None,
    trusted_semantic_replay_catalog: SemanticReplayCatalog | None = None,
) -> SemanticPipelineResult:
    return parse_semantic_pdf_v2(
        _source(tmp_path),
        hints=StructureDocument(blocks=()),
        mode="candidate",
        provider=provider,
        trusted_semantic_replay_catalog=trusted_semantic_replay_catalog,
        extracted_evidence=_extracted(),
        spacing_scorer=AmbiguousSpacingScorer(),
    )
```

Use `ChangedIdentityProvider` in `test_changed_request_identity_uses_fresh_adjudication`; its changed model capability changes the request identity while retaining valid structured responses.

- [x] **Step 2: Run the focused semantic tests and confirm they fail**

Run: `uv run pytest tests/integration/test_semantic_pdf_v2.py -q -k 'catalog or request_identity'`

Expected: failures because the replay catalog is not accepted or enforced.

- [x] **Step 3: Thread the typed catalog through public parser boundaries**

```python
def process_product(
    product_path: str | Path,
    *,
    registry_root: str | Path,
    provider: LLMProvider | None = None,
    parser: DoclingParser | None = None,
    pr_number: int | None = None,
    warnings_as_errors: bool = False,
    trusted_semantic_repair: dict[str, object] | None = None,
    trusted_semantic_fidelity: dict[str, object] | None = None,
    trusted_semantic_decisions: dict[str, object] | None = None,
    trusted_semantic_replay_catalog: SemanticReplayCatalog | None = None,
    require_semantic_visual_correction: bool = True,
    propagate_provider_errors: bool = False,
    semantic_pipeline_mode: SemanticPipelineMode | str = SemanticPipelineMode.SHADOW,
    semantic_diagnostics_dir: str | Path | None = None,
) -> ProcessResult:
    active_parser = _processing_parser(
        provider=provider,
        parser=parser,
        trusted_semantic_repair=trusted_semantic_repair,
        trusted_semantic_fidelity=trusted_semantic_fidelity,
        trusted_semantic_decisions=trusted_semantic_decisions,
        trusted_semantic_replay_catalog=trusted_semantic_replay_catalog,
        propagate_provider_errors=propagate_provider_errors,
        semantic_pipeline_mode=semantic_pipeline_mode,
    )


class DoclingParser:
    def __init__(
        self,
        *,
        converter: Any | None = None,
        full_page_ocr_converter: Any | None = None,
        structure_repair_planner: SemanticStructureRepairPlanner | None = None,
        trusted_repair_record: SemanticStructureRepairRecord | None = None,
        ocr_correction_planner: OcrCorrectionPlanner | None = None,
        trusted_fidelity_report: SemanticFidelityReport | None = None,
        pdfium: Any | None = None,
        semantic_pipeline_mode: str = "shadow",
        candidate_provider: Any | None = None,
        trusted_candidate_decisions: tuple[Any, ...] = (),
        trusted_semantic_replay_catalog: SemanticReplayCatalog | None = None,
    ) -> None:
        self._trusted_semantic_replay_catalog = trusted_semantic_replay_catalog


trusted_semantic_replay_catalog: SemanticReplayCatalog | None = None,
```

Add that exact keyword to the existing `parse_semantic_document` and `parse_semantic_pdf_v2` signatures. At both `parse_semantic_pdf_v2` calls in `parse_semantic_document`, forward it exactly as:

```python
trusted_semantic_replay_catalog=trusted_semantic_replay_catalog,
```

At the `parse_semantic_document` call in `DoclingParser.parse`, forward the stored value exactly as:

```python
trusted_semantic_replay_catalog=self._trusted_semantic_replay_catalog,
```

- [x] **Step 4: Flatten trusted decisions before adjudication**

```python
catalog_decisions = (
    trusted_semantic_replay_catalog.trusted_decisions(source.sha256)
    if trusted_semantic_replay_catalog is not None
    else ()
)
adjudicator = CandidateAdjudicator(
    provider,
    trusted=(*trusted_decisions, *catalog_decisions),
)
```

Preserve product-local decisions first so an update of the same product retains its existing deterministic precedence; cross-product decisions are fallback authority and still pass `_trusted_decision_matches`.

- [x] **Step 5: Enforce the final byte invariant**

```python
decision_report = DecisionReport(source_hash=evidence.source_hash, decisions=decisions)
canonical_markdown = render_canonical_markdown(canonical)
expected = (
    trusted_semantic_replay_catalog.canonical_markdown_for(decision_report)
    if trusted_semantic_replay_catalog is not None
    else None
)
if expected is not None and canonical_markdown.encode("utf-8") != expected:
    validation = validation.model_copy(
        update={
            "status": SemanticPipelineStatus.FAILED,
            "publishable": False,
            "findings": [
                *validation.findings,
                ValidationFinding(
                    code="SEMANTIC_SOURCE_REPLAY_MISMATCH",
                    message="Compatible semantic replay differs from the trusted canonical bytes.",
                ),
            ],
        }
    )
```

Use `decision_report` in the returned `SemanticPipelineResult` so lookup and diagnostics observe the identical object.

- [x] **Step 6: Preserve the exact mismatch code in the quality report**

```python
replay_mismatch = next(
    (
        finding
        for finding in validation.findings
        if finding.code == "SEMANTIC_SOURCE_REPLAY_MISMATCH"
    ),
    None,
)
if replay_mismatch is not None:
    return [
        QualityFinding(
            code=replay_mismatch.code,
            message="Compatible semantic replay failed canonical byte equality",
            path="quality.validation-report.json",
        )
    ]
```

Keep the existing generic candidate-validation finding for all other failed invariants.

- [x] **Step 7: Run semantic pipeline and existing parser tests, then commit**

Run: `uv run pytest tests/integration/test_semantic_pdf_v2.py tests/unit/test_pipeline.py -q`

Expected: all tests pass.

```bash
git add src/ard_ossie/pipeline.py src/ard_ossie/docling_parser.py src/ard_ossie/semantic/parser.py src/ard_ossie/semantic/pipeline_v2.py tests/integration/test_semantic_pdf_v2.py
git commit -m "fix: enforce same-source semantic replay"
```

### Task 5: Lock the Issue #3 Korean spacing regression and atomic failure behavior

**Files:**
- Modify: `tests/integration/test_semantic_pdf_regressions.py`
- Modify: `tests/integration/test_atomic_promotion.py`

**Interfaces:**
- Consumes: session fixture `issue_3_replay`, `tests/fixtures/semantic/issue-3-evidence.json`, and `SemanticReplayCatalog`.
- Produces: regression proof for `정의서이며`, zero cross-product adjudication calls, replay mismatch diagnostics, and unchanged generated/Registry state.

- [x] **Step 1: Add the exact Issue #3 same-source regression**

```python
def test_issue_3_same_source_catalog_blocks_korean_word_boundary_drift(issue_3_replay) -> None:
    first, _first_provider, _repeated, _repeated_provider = issue_3_replay
    catalog = SemanticReplayCatalog.build(
        (
            SemanticReplayBaseline(
                product_key="500138301",
                identity=semantic_replay_identity(first.decisions),
                canonical_markdown=first.canonical_markdown.encode("utf-8"),
                decisions=first.decisions,
            ),
        )
    )
    replayed, provider = run_evidence_replay(
        Path("tests/fixtures/semantic/issue-3-evidence.json"),
        trusted_semantic_replay_catalog=catalog,
        provider=BadDefinitionSpacingProvider(),
    )

    assert provider.calls == 0
    assert replayed.canonical_markdown == first.canonical_markdown
    assert "정의서이며" in replayed.canonical_markdown
    assert "정의 서이며" not in replayed.canonical_markdown
```

Define the provider and extend the replay helper with these exact optional injection points:

```python
class BadDefinitionSpacingProvider(ReplayCandidateProvider):
    def generate_structured(self, **kwargs: object) -> LLMResult:
        self.calls += 1
        request = json.loads(cast(list[dict[str, str]], kwargs["messages"])[-1]["content"])
        bad = next(
            item for item in request["candidates"] if "정의 서이며" in item.get("rendering", "")
        )
        return self._result({"candidate_id": bad["candidate_id"], "confidence": 0.99})


def run_evidence_replay(
    path: Path,
    *,
    trusted_decisions: tuple[DecisionRecord, ...] = (),
    trusted_semantic_replay_catalog: SemanticReplayCatalog | None = None,
    provider: ReplayCandidateProvider | None = None,
) -> tuple[SemanticPipelineResult, ReplayCandidateProvider]:
    evidence = load_evidence_replay(path)
    active_provider = provider or ReplayCandidateProvider()
    source = SourceFile.model_construct(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=Path("issue-3.pdf"),
        relative_path="sources/semantic/issue-3.pdf",
        sha256=evidence.source_hash,
        size_bytes=0,
        snapshot=b"",
    )
    result = parse_semantic_pdf_v2(
        source,
        hints=load_structure_replay(path),
        mode="candidate",
        provider=active_provider,
        trusted_decisions=trusted_decisions,
        trusted_semantic_replay_catalog=trusted_semantic_replay_catalog,
        extracted_evidence=evidence,
    )
    return result, active_provider
```

Preserve the helper's current default behavior and return tuple.

- [x] **Step 2: Run the regression and confirm it passes through trusted reuse**

Run: `uv run pytest tests/integration/test_semantic_pdf_regressions.py -q -k 'same_source_catalog'`

Expected: pass with zero provider calls and exact Markdown equality.

- [x] **Step 3: Add replay-specific atomic promotion failure test**

```python
def test_semantic_replay_mismatch_writes_diagnostics_without_promotion(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    before = {
        "generated": tree_hash(product / "generated"),
        "registry": tree_hash(registry),
    }
    verified, _provider = run_evidence_replay(
        Path("tests/fixtures/semantic/issue-3-evidence.json")
    )
    mismatching_catalog = catalog_for(
        "500138301",
        verified,
        markdown=b"different-but-hash-verified-base\n",
    )
    failed, _provider = run_evidence_replay(
        Path("tests/fixtures/semantic/issue-3-evidence.json"),
        trusted_semantic_replay_catalog=mismatching_catalog,
    )
    parser = ReplayMismatchParser(failed)

    with pytest.raises(PipelineValidationError, match="SEMANTIC_SOURCE_REPLAY_MISMATCH"):
        process_product(product, registry_root=registry, parser=parser)

    assert tree_hash(product / "generated") == before["generated"]
    assert tree_hash(registry) == before["registry"]
    quality = json.loads((product / "quality/quality-report.json").read_text())
    validation = json.loads((product / "quality/validation-report.json").read_text())
    application = json.loads((product / "quality/application-report.json").read_text())
    assert [item["code"] for item in quality["hard_errors"]] == [
        "SEMANTIC_SOURCE_REPLAY_MISMATCH"
    ]
    assert validation["publishable"] is False
    assert "SEMANTIC_SOURCE_REPLAY_MISMATCH" in application["applications"][0][
        "invariant_codes"
    ]
```

Place this parser in the same test module:

```python
class ReplayMismatchParser(FidelityParser):
    def __init__(self, result: SemanticPipelineResult) -> None:
        super().__init__(
            canonical_fidelity_report(result.evidence, result.canonical, result.validation)
        )
        self.result = result

    def parse(self, source: SourceFile) -> ParsedDocument:
        if source.role is SourceRole.PRODUCT_HTML:
            return super().parse(source)
        return ParsedDocument(
            role=source.role,
            source_hash=source.sha256,
            markdown=self.result.markdown,
            semantic_fidelity=self.fidelity,
            semantic_validation=self.result.validation,
            semantic_pipeline_result=self.result,
        )
```

- [x] **Step 4: Run the integration tests and commit**

Run: `uv run pytest tests/integration/test_semantic_pdf_regressions.py tests/integration/test_atomic_promotion.py -q`

Expected: all tests pass.

```bash
git add scripts/verify_issue_3_semantic.py tests/integration/test_semantic_pdf_regressions.py tests/integration/test_atomic_promotion.py
git commit -m "test: prevent same-source spacing drift"
```

### Task 6: Run repository gates and prepare the code-only PR

**Files:**
- Modify only if verification exposes a defect in files already listed above.

**Interfaces:**
- Consumes: all commits from Tasks 1-5.
- Produces: a clean, reviewed branch ready for a code-only PR into `main`.

- [x] **Step 1: Run focused replay tests together**

Run:

```bash
uv run pytest \
  tests/unit/test_semantic_replay.py \
  tests/unit/test_semantic_replay_loader.py \
  tests/unit/test_processing_service.py \
  tests/integration/test_semantic_pdf_v2.py \
  tests/integration/test_semantic_pdf_regressions.py \
  tests/integration/test_atomic_promotion.py -q
```

Expected: all focused tests pass.

- [x] **Step 2: Run lint and formatting checks on changed Python files**

Run:

```bash
uv run ruff check \
  src/ard_ossie/semantic/replay.py \
  src/ard_ossie/application/semantic_replay.py \
  src/ard_ossie/application/processing.py \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/docling_parser.py \
  src/ard_ossie/semantic/parser.py \
  src/ard_ossie/semantic/pipeline_v2.py \
  tests/unit/test_semantic_replay.py \
  tests/unit/test_semantic_replay_loader.py \
  tests/unit/test_processing_service.py \
  tests/integration/test_semantic_pdf_v2.py \
  tests/integration/test_semantic_pdf_regressions.py \
  tests/integration/test_atomic_promotion.py
uv run ruff format --check \
  src/ard_ossie/semantic/replay.py \
  src/ard_ossie/application/semantic_replay.py \
  src/ard_ossie/application/processing.py \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/docling_parser.py \
  src/ard_ossie/semantic/parser.py \
  src/ard_ossie/semantic/pipeline_v2.py \
  tests/unit/test_semantic_replay.py \
  tests/unit/test_semantic_replay_loader.py \
  tests/unit/test_processing_service.py \
  tests/integration/test_semantic_pdf_v2.py \
  tests/integration/test_semantic_pdf_regressions.py \
  tests/integration/test_atomic_promotion.py
```

Expected: both commands exit 0.

- [x] **Step 3: Run model/schema verification and the complete test suite**

Run:

```bash
uv run python -m ard_ossie.application.model_schema_verification --repository .
uv run pytest -q
```

Expected: model/schema verification succeeds and at least the 1,183-test baseline plus new tests passes.

- [x] **Step 4: Check diff hygiene and inspect the branch delta**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors, no uncommitted files, and only the design, plan, implementation, and tests listed above.

- [x] **Step 5: Perform pre-landing review and publish the code-only PR**

Use the repository review workflow on `origin/main...HEAD`. Resolve every blocker, rerun the affected focused tests, then push `fix/same-source-semantic-replay` and open a PR targeting `main`. Do not modify or merge product PR #49 in this task.

### Task 7: Merge the fix, reprocess Issue #46, and verify PR #49 exact-head state

**Files:**
- No direct file edits; this is the rollout verification task.

**Interfaces:**
- Consumes: merged code PR SHA, Issue #46, product branch `ard/issue-46-500138302`, and PR #49.
- Produces: a regenerated product PR whose exact head contains current `main`, contains `정의서이며`, and has successful protected statuses.

- [x] **Step 1: Wait for the code PR protected checks and merge it to `main`**

Expected checks: repository change gate, model/schema verification, full pytest, wheel build, and any configured security checks all report success on the code PR exact head.

- [x] **Step 2: Trigger trusted base sync for Issue #46**

Remove `ard:approved` from Issue #46 and reapply it once. This uses the existing trusted `base_sync` flow to merge current `main`, clear only allowed derived output, and re-run processing.

- [x] **Step 3: Verify regenerated PR #49 content and ancestry**

Run against the refreshed local branch or equivalent exact GitHub refs:

```bash
git merge-base --is-ancestor origin/main origin/ard/issue-46-500138302
git show origin/ard/issue-46-500138302:products/500138302/generated/data-semantic.md | rg -n '정의서이며|정의 서이며'
```

Expected: the ancestry command exits 0; output contains `정의서이며` and does not contain `정의 서이며`.

- [x] **Step 4: Verify PR #49 exact-head statuses**

Read the new PR #49 head SHA and require `ard/quality-gate=success` and `ard/changeset=success` on that exact SHA. Confirm the PR is no longer behind `main` and no replay mismatch or trust error appears in the redacted result envelope.

- [x] **Step 5: Record rollout evidence**

Capture the merged code PR URL/SHA, Issue #46 workflow run URL, refreshed PR #49 head SHA, exact-head status results, and the line proving `정의서이며`. Only then mark PR #49 ready for its normal review/merge process.

완료 증거는 [Issue #46 same-source replay 및 v1 release 검증 기록](../../acceptance/issue-46-same-source-replay-release.md)에 있습니다. Fix PR #51은 `51989c1e67b2f024e3cab6cfc1d7c61cff1e2018`로 병합됐고, Issue #46 재처리 run `31991352055`가 refreshed PR #49 head `721a143a2ffc0183bab418dd9448543b82e912b8`에 두 required status를 success로 게시했습니다. PR #49는 `282228635a36e8709ef8cb01fc0bfba4259ed01b`로 병합됐으며 product v1 release와 downstream linkage까지 완료됐습니다.
