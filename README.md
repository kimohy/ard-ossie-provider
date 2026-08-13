# ARD Ossie Provider

AI Ready Data(ARD) 문서를 GitHub에서 공개적으로 관리하고 Apache Ossie 0.1.1 모델로 변환하는 오픈소스 컴파일러입니다.

입력은 데이터 제품 HTML, 시멘틱 DOCX/PDF, 데이터 딕셔너리 XLSX의 세 종류입니다. 제품 HTML은 Docling으로, 데이터 딕셔너리는 셀 보존 Excel 어댑터로 파싱합니다. 시멘틱 DOCX/PDF는 이중 소스 권위 경로를 사용합니다. OOXML/PDFium이 추출한 원문 텍스트가 게시 문자열의 유일한 권위이며, 완전한 PDF 내장 텍스트가 없으면 문서 전체 OCR 결과가 그 실행의 권위가 됩니다. 내장 텍스트 페이지와 OCR 페이지를 섞지 않습니다. Docling 텍스트는 논리 구조와 읽기 순서를 맞추는 힌트로만 사용합니다. 그 뒤 다음 결과를 결정적으로 생성합니다.

- `data-product.md`
- `data-semantic.md`
- `data-dictionary.json`
- `ossie-model.json`
- `source-manifest.json`
- 중복·버전·영향도·완전성·LLM 제안 감사 보고서
- 필수 `semantic-fidelity.json`과 구조 복구를 요청하거나 재사용했을 때만 생성하는 `semantic-structure-repair.json`

## 핵심 원칙

- 제품, 테이블, 컬럼은 접두어가 붙은 UUIDv7 불변 ID를 사용합니다.
- 제품과 테이블 버전은 서로 독립적인 단순 숫자 `v1`부터 `v999`까지입니다.
- 현재 상태만 파일로 보관하고 과거 상태는 Git commit, tag, GitHub Release로 탐색합니다.
- LLM은 근거가 있는 시멘틱 설명·동의어·ANSI SQL metric만 제안할 수 있습니다. 물리 이름, 타입, PK/FK, 불변 ID는 결정적 코드가 관리하며 FK 관계는 Excel에서 결정적으로 생성합니다.
- 시멘틱 문서 구조 복구 LLM은 불변 source span ID를 block 종류·순서·표 좌표에 매핑할 뿐이며 게시 텍스트를 작성하거나 수정할 수 없습니다. 구조 복구가 실패하면 원문 span을 문단 또는 lossless block으로 모두 보존하고 `SEMANTIC_STRUCTURE_DEGRADED`와 `WARN`을 기록합니다.
- Issue, 첨부파일, 생성물과 보고서는 모두 public 저장소에 공개됩니다.
- 승인 전 Issue와 fork PR에는 LLM Secret이나 쓰기 권한을 전달하지 않습니다.

## 저장 구조

```text
products/<product-key>/
  product.yaml
  intake-manifest.json
  sources/
    product-info/product.html
    semantic/semantic.docx|pdf
    dictionary/dictionary.xlsx
  generated/
    data-product.md
    data-semantic.md
    data-dictionary.json
    ossie-model.json
    source-manifest.json
  quality/
    quality-report.json
    duplicate-report.json
    version-report.json
    impact-report.json
    llm-suggestions.json
    semantic-fidelity.json
    semantic-structure-repair.json  # 구조 복구 요청 또는 재사용 시에만 존재

registry/
  products/<product-id>.json
  tables/<table-id>.json
  mappings/<product-id>.json
  changesets/<changeset-id>.json
  indexes/
```

제품 폴더에 `v1`, `v2` 사본을 중복 저장하지 않습니다. 태그는 `product/<product-id>/vN`, `table/<table-id>/vN` 형식입니다.

## 처리 경로

### GitHub Issue

1. `AI Ready Data submission` Issue Form에 한 제품과 세 문서를 첨부합니다.
2. write 이상 권한의 관리자가 `ard:approved` 라벨을 적용합니다.
3. 첨부 호스트·리다이렉트·크기·MIME·확장자·파일 구조를 검증합니다.
4. `ard/issue-<number>-<product-key>` 브랜치와 Draft PR을 생성합니다.
5. 같은 PR에 Docling/LLM 변환 결과, 레지스트리 변경, 품질 보고서를 커밋합니다.
6. `ard/quality-gate`와 `ard/changeset` 상태를 통과해야 main에 병합할 수 있습니다.

### 직접 브랜치 커밋

`products/*/sources/**`를 신뢰된 non-main 브랜치에 커밋하면 동일한 처리기가 실행됩니다. 한 변경에는 정확히 한 제품만 허용됩니다. Fork PR은 Secret·writeback 없이 로컬 소스/스키마 검사만 실행합니다.

직접 커밋의 `product.yaml`에도 `product_id`가 필수입니다. 새 ID는 Issue 수집 경로가 생성하며, 이미 등록된 ID를 갱신하려면 `operation: update`, 현재 `base_version`, 정확히 `+1`인 새 버전을 함께 제출합니다.

DOCX/PDF/XLSX는 Git LFS 객체로 관리합니다. 브랜치 처리 checkout과 Issue 수집 push도 LFS 객체를 명시적으로 내려받고 올립니다.

## 로컬 실행

Python 3.12와 `uv`가 필요합니다.

```bash
uv sync --frozen
uv run ard process products/sales-order --registry registry
uv run ard impact table <table-id> --registry registry
uv run ard history sales-order
uv run ard diff sales-order@v1..v2
```

세분화된 명령과 GitHub lifecycle 명령은 다음 help에서 확인합니다.

```bash
uv run ard --help
uv run ard workflow --help
uv run ard github --help
uv run ard parse --help
uv run ard model --help
uv run ard validate --help
```

GitHub event fixture가 있으면 승인 단계와 intake를 로컬에서 같은 result envelope 계약으로
실행할 수 있습니다.

```bash
uv run ard workflow issue-authorize --event event.json --repository-name owner/repo --actor maintainer --label ard:approved
uv run ard workflow issue-intake --event event.json --repository-name owner/repo --actor maintainer
```

각 lifecycle은 `.ard/run/<command>-result.json`을 원자적으로 기록합니다. 공통 exit code는
`0` 성공/no-op, `10` 검증, `20` 구성, `30` 일시 장애, `40` 충돌, `50` 보안 경계,
`70` 일부 원격 반영입니다. `30`은 원인을 해소한 뒤 재시도하고, `70`은 mutation journal과
현재 head를 보존한 채 동일 입력으로 수렴시킵니다. 강제 tag 이동이나 branch 덮어쓰기는 하지
않습니다.

OpenAI-compatible API를 사용할 때만 다음 환경 변수를 설정합니다.

```bash
read -s ARD_LLM_API_KEY
export ARD_LLM_API_KEY
export ARD_LLM_BASE_URL='https://api.openai.com/v1'
export ARD_LLM_MODEL='your-model'
export ARD_LLM_API_STYLE='chat_completions'
```

API 키는 파일에 저장하거나 로그로 출력하지 마세요.

### 시멘틱 구조 충실도 acceptance

워크플로가 생성한 실제 제품 디렉터리와 그에 대응하는 Registry 디렉터리를 각각
`SEMANTIC_PRODUCT_ROOT`, `SEMANTIC_REGISTRY_ROOT`에 지정한 뒤, 먼저 LLM credential 없이
결정적 파싱을 검증합니다. 생성물이나 Registry 파일을 직접 수정하지 마세요.

```bash
test -n "${SEMANTIC_PRODUCT_ROOT:-}" && test -d "$SEMANTIC_PRODUCT_ROOT"
test -n "${SEMANTIC_REGISTRY_ROOT:-}" && test -d "$SEMANTIC_REGISTRY_ROOT"
export SEMANTIC_PRODUCT_ROOT SEMANTIC_REGISTRY_ROOT
env -u ARD_LLM_API_KEY -u ARD_LLM_BASE_URL -u ARD_LLM_MODEL -u ARD_LLM_API_STYLE \
  UV_CACHE_DIR=/tmp/ard-semantic-structure-uv-cache \
  uv run --frozen ard process "$SEMANTIC_PRODUCT_ROOT" --registry "$SEMANTIC_REGISTRY_ROOT"
UV_CACHE_DIR=/tmp/ard-semantic-structure-uv-cache uv run --frozen python - <<'PY'
import json
import os
from pathlib import Path

product = Path(os.environ["SEMANTIC_PRODUCT_ROOT"])
semantic = (product / "generated" / "data-semantic.md").read_text(encoding="utf-8")
fidelity = json.loads(
    (product / "quality" / "semantic-fidelity.json").read_text(encoding="utf-8")
)
assert "개인정보" in semantic
assert "유효성" in semantic
assert "개 인정보" not in semantic
assert "유 효 성" not in semantic
assert "|" in semantic
assert fidelity["source_text_coverage"] == 1.0
assert fidelity["unmatched_span_count"] == 0
assert fidelity["duplicated_span_count"] == 0
PY
```

결정적 reconciliation 뒤에도 구조가 해결되지 않은 실제 문서는 기존의 보호된
`ARD_LLM_*` 환경에서 다시 처리하고 `semantic-structure-repair.json`까지 검증합니다. 이때도
원문 payload와 API key는 출력하지 않습니다. 결정적 reconciliation 또는 검증된 LLM 구조
복구가 모든 불변식을 만족하면 시멘틱 충실도는 `PASS`입니다. 전체 문서 OCR을 사용하거나
실패한 구조 복구를 lossless block으로 내리면 `WARN`이며, source span 손실이나 예상하지 않은
중복은 `FAIL`입니다.

## 중복과 버전 규칙

제품 생성 시 기존 ID, product key, alias, 동일 canonical hash는 차단됩니다. 의미적으로 유사한 후보는 자동 병합하지 않고 경고만 생성합니다. 업데이트 시 제품 ID와 product key는 바꿀 수 없고 retired ID는 다시 활성화할 수 없습니다.

테이블은 정규화한 `(source system, catalog, schema, table)` locator가 같으면 전역 ID를 재사용합니다. 같은 locator에 다른 ID를 지정하면 차단하고, locator가 다른데 스키마만 같으면 clone 가능성 경고만 생성합니다. 컬럼 ID는 같은 테이블 안에서 이름/alias로 재사용하며 제거된 컬럼은 retired 처리합니다.

새 엔터티는 반드시 `v1`입니다. 내용이 바뀌면 현재 버전에서 정확히 `+1`, 내용이 같으면 같은 버전을 유지해야 합니다. stale base, 건너뛴 버전, 충돌, `v999` 이후 변경은 차단합니다.

두 제품 이상이 참조하는 테이블을 변경하려면 changeset이 필요합니다. 중앙 `ard/changeset-<id>` 브랜치가 제품별 준비 상태를 직렬화하며 모든 필수 제품 PR이 준비되기 전까지 `ard/changeset`은 pending입니다.

GitHub Actions YAML은 checkout, 권한, Environment, matrix, artifact 전달 같은 플랫폼 선언만
담습니다. 분류·검증·Git/Release/GitHub mutation은 모두 `uv run --frozen ard ...` lifecycle을
통해 실행합니다. `production-linkage` 재시도는 dispatch를 다시 보낼 수 있으므로 downstream은
`(product_id, version, tag, commit)` 튜플을 중복 제거 키로 사용해야 합니다.

Metric과 relationship도 각각 `met_*`, `rel_*` 불변 ID를 Registry의 제품 레코드에 보존합니다. Metric은 읽기 전용 ANSI SQL만 허용하고 알려지지 않은 `table.column` 참조와 변경 SQL을 차단합니다. relationship은 Excel의 `fk_table`/`fk_column`을 현재 제품 테이블에 해석하지 못하면 hard error입니다. 제품 폐기(retire)는 tombstone 전환 파이프라인이 구현되기 전까지 Issue와 `product.yaml`에서 허용하지 않습니다.

## 문서

- [GitHub Actions 설정](docs/github-actions-setup.md)
- [다음 작업 로드맵](docs/next-steps.md)
- [상세 아키텍처](docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md)
- [구현 계획](docs/superpowers/plans/2026-08-08-ard-github-pipeline.md)

## 라이선스

Apache License 2.0
