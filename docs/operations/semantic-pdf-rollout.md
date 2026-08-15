# Semantic PDF 운영, 검증, rollback

이 문서는 `candidate` PDF 파이프라인의 운영 runbook입니다. 상태와 보고서 구조는 [시멘틱 PDF 파이프라인](../semantic-pdf-pipeline.md), 허용되는 자동 판단의 경계는 [ARD 변환 정책과 거버넌스](../policy-and-governance.md)를 따릅니다.

## Runtime mode

GitHub processor는 `ARD_SEMANTIC_PDF_PIPELINE`이 없으면 `candidate`를 사용합니다. 허용 값은 `legacy`, `shadow`, `candidate`뿐이며 DOCX에는 적용하지 않습니다.

- `candidate`: canonical candidate 결과를 게시합니다.
- `shadow`: 두 경로를 실행하고 diagnostics를 남기되 legacy 결과를 게시합니다.
- `legacy`: candidate 경로를 우회하는 즉시 rollback입니다.

## 정상 상태 판별

다음 세 층을 따로 확인합니다.

1. `decision-report.json`: 후보를 어떻게 선택했는지
2. `application-report.json`: 선택된 후보가 canonical에 실제 적용됐는지
3. `validation-report.json`과 `semantic-fidelity.json`: 문서 전체가 게시 가능한지

| 관찰 | 판정 | 조치 |
|---|---|---|
| `verified`, `publishable=true` | 정상 게시 | warning의 감사 이유만 확인 |
| `review_pending`, `publishable=true` | 안전한 fallback 게시 | `semantic-review.json`을 후속 개선 항목에 연결 |
| `review_required`, `publishable=false` | 안전한 후보 없음 | 후보·scorer·LLM contract 개선 후 재처리 |
| `failed`, `publishable=false` | 전역 invariant 실패 | source loss, duplicate, grid, Markdown finding부터 수정 |

`WARN`만 보고 실패로 판단하지 않습니다. OCR, 검증된 spacing repair, review debt, 제외된 선택적 metric은 게시 가능한 경고일 수 있습니다. `hard_errors`, `publishable`, invariant finding을 함께 봅니다.

## 저신뢰와 공백 복구 triage

기본 confidence 하한은 `0.80`입니다. primary, recovery, 필요한 경우 tie-break까지만 허용합니다.

- `LLM_LOW_CONFIDENCE_RECOVERED`: allowlist 후보 합의가 성공했습니다.
- `LLM_SPACING_REPAIR_APPLIED`: whitespace-only 생성 후보가 별도 verification과 결정적 검증을 통과했습니다.
- `LLM_SPACING_REPAIR_DEFERRED`: 생성 또는 검증은 끝나지 않았지만 안전한 기존 spacing 후보를 적용했습니다.
- `LLM_CONFIDENCE_RECOVERY_EXHAUSTED` 또는 `LLM_CONSENSUS_NOT_REACHED`: terminal attempt와 fallback 적용 여부를 함께 확인합니다.
- `SPACING_REPAIR_SAFE_FALLBACK_UNAVAILABLE`: 모든 spacing 후보에 결정적 결함이 있어 게시할 수 없습니다.

`semantic-review.json`이 있으면 fallback candidate ID, 후보 점수, phase별 request hash와 confidence, provider retry/repair, invariant rejection code를 보존합니다. 이 파일이 없는 verified 실행을 억지로 사람 검토 대기 상태로 만들지 않습니다.

## 선택적 metric 경고

`LLM_METRIC_SQL_UNSAFE`는 해당 선택적 provider suggestion을 제외했다는 뜻입니다. 다음을 확인합니다.

- `quality-report.json`의 `hard_errors`가 비어 있음
- 경고 message에 SQL 전문이나 metric 원문이 노출되지 않음
- `llm-suggestions.json`에는 검증을 통과한 제안만 있음
- 기존 Registry metric이 제외된 suggestion 이름 때문에 삭제되지 않음

이 조건을 만족하면 문서 변환과 릴리스를 계속할 수 있습니다.

## 증거 보존

실패 run과 rollback run의 artifact를 모두 보존합니다. 기본 semantic diagnostics에는 다음이 있어야 합니다.

- `manifest.json`
- `evidence-summary.json`
- `candidate-report.json`
- `decision-report.json`
- `application-report.json`
- `validation-report.json`
- `failure-report.json`
- 필요할 때만 `semantic-review.json`

workflow URL, exact head, source/configuration/canonical hash, extraction mode, page·atom·region·table 수, status, invariant code, model/cache source, attempt 횟수와 confidence를 기록합니다. API key, service-account JSON, 원문 prompt/response, page image, unmasked source text는 Issue나 PR에 붙이지 않습니다.

## Issue #3 검증

Issue #3 제품은 Git LFS source를 실제 객체로 받은 checkout에서 검증합니다.

```bash
git lfs pull --include='products/500138301/sources/**'
uv run --frozen python scripts/verify_issue_3_semantic.py \
  --product-root products/500138301
```

정상 기준:

- verifier exit `0`
- `validation-report.json`: `verified`, `publishable=true`
- source coverage `1.0`, unmatched/duplicated/degraded `0`
- heading 12개와 level `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`
- GFM table 10개
- visible escape와 raw HTML `0`
- 잘못 분리된 `개 인정보`, `유 효 성` 같은 알려진 공백 패턴 `0`

LFS pointer의 SHA-256과 내려받은 객체가 일치하지 않으면 검증을 중단합니다. pointer 파일 자체를 PDF로 간주하지 않습니다.

## 즉시 rollback

새 문서에서 source loss, table corruption, raw HTML 또는 재현되지 않는 canonical hash가 발견되면 repository Variable을 바꾸고 실패한 data workflow를 재실행합니다.

```bash
gh variable set ARD_SEMANTIC_PDF_PIPELINE --body legacy
gh run rerun RUN_ID
```

rollback은 PDF parser만 바꾸며 source attachment와 이미 병합된 commit을 수정하지 않습니다. 원인을 해결한 뒤 candidate로 복원합니다.

```bash
gh variable set ARD_SEMANTIC_PDF_PIPELINE --body candidate
```

`gh run rerun`은 실패한 run의 exact commit과 workflow를 다시 사용합니다. 네트워크·권한·provider 같은 외부 원인을 해소한 재시도에는 적합하지만, 그 commit의 구현 결함을 고치지는 않습니다.

## 릴리스 복구

`ARD numeric release`는 `main`의 `products/**` 또는 `registry/**` 변경에만 반응합니다. code-only 수정은 자동으로 과거 data 릴리스를 재시작하지 않습니다.

### 1. 부분 상태 확인

```bash
gh run view RUN_ID --log-failed
gh api repos/OWNER/REPO/actions/runs/RUN_ID/artifacts
git ls-remote --tags origin 'product/*' 'table/*'
gh release view 'product/PRODUCT_ID/vN'
```

result envelope의 exit, finding, `outputs.commit`, `outputs.artifact_sha256`, mutations를 읽습니다. product/table tag의 실제 peeled target과 Release asset을 대조합니다. tag나 Release가 일부 생겼으면 삭제하거나 이동하지 않습니다.

### 2. 원격 immutable mutation이 없을 때

tag, Release, dispatch status가 **모두 없다는 것이 확인된 경우에만** 수정된 최신 `main`에서 수동 release를 새로 시작할 수 있습니다. 보호된 maintainer 환경의 clean checkout에서 source LFS 객체와 tag를 받은 뒤 실행합니다.

```bash
CURRENT="$(git rev-parse HEAD)"
uv run --frozen ard workflow release-product \
  --product-key PRODUCT_KEY \
  --current "$CURRENT" \
  --table-ids '["TABLE_ID"]' \
  --output dist \
  --repository-name OWNER/REPO
```

CLI는 annotated tag를 만들기 전에 repository-local bot identity를 설정합니다. 같은 tag가 같은 commit이면 재사용하고, 다른 commit이면 이동하지 않고 `TAG_TARGET_CONFLICT`로 실패합니다.

### 3. tag 또는 Release가 일부 존재할 때

이미 생성된 immutable mutation이 하나라도 있으면 최신 `main`으로 바꾸지 않습니다. result의 `outputs.commit`과 원격 tag target이 같은지 확인하고, 외부 원인을 해소한 뒤 원래 run을 재실행합니다.

```bash
ORIGINAL_COMMIT="$(jq -r '.outputs.commit' .ard/run/workflow.release-product-result.json)"
RUN_HEAD="$(gh run view RUN_ID --json headSha --jq .headSha)"
test "$ORIGINAL_COMMIT" = "$RUN_HEAD"
git ls-remote --tags origin 'product/*' 'table/*'
gh run rerun RUN_ID
```

출력된 existing tag 각각의 peeled target이 `ORIGINAL_COMMIT`과 같은지 확인합니다. 하나라도 다르면 재실행하지 않고 `TAG_TARGET_CONFLICT` incident로 전환합니다.

원래 run이 없는 수동 복구라면 exact original commit을 detached checkout하고, 최초 run의 `TABLE_IDS` 입력을 그대로 재사용합니다. 값이나 version을 추정하지 않습니다.

```bash
git fetch origin main --tags
git switch --detach "$ORIGINAL_COMMIT"
git lfs pull
CURRENT="$(git rev-parse HEAD)"
test "$CURRENT" = "$ORIGINAL_COMMIT"

uv run --frozen ard workflow release-product \
  --product-key PRODUCT_KEY \
  --current "$CURRENT" \
  --table-ids "$ORIGINAL_TABLE_IDS_JSON" \
  --output dist \
  --repository-name OWNER/REPO
```

원래 commit의 release 구현 자체가 결함이면 이 경로로 수렴시킬 수 없습니다. tag를 이동·삭제하거나 수정 commit에서 같은 version을 재게시하지 말고 incident를 열어 원래 tag/asset 상태를 보존한 채 별도 복구 정책을 승인받습니다.

### 4. 성공 result만 dispatch

```bash
uv run --frozen ard workflow release-dispatch \
  --result-path .ard/run/workflow.release-product-result.json \
  --current "$CURRENT" \
  --repository-name OWNER/REPO \
  --target-url RELEASE_OR_RUN_URL
```

제품 tag, 모든 table tag의 peeled target, Release asset hash, `ard/dispatched:<product-id>:vN` status를 확인합니다. 같은 명령을 다시 실행했을 때 release mutation이 `noop`이고 dispatch status도 `noop`이면 수렴한 것입니다.

## 로컬 repository gate

clean candidate worktree에서 비교 대상과 head를 immutable SHA로 전달합니다.

```bash
BASE_SHA="$(git rev-parse origin/main)"
HEAD_SHA="$(git rev-parse HEAD)"
uv run --frozen ard workflow repository-check \
  --repository "$PWD" \
  --base-ref "$BASE_SHA" \
  --head-ref "$HEAD_SHA" \
  --head-sha "$HEAD_SHA" \
  --verification-group static
```

branch 이름을 `--base-ref`나 `--head-ref`로 전달하지 않습니다. `--head-ref`, `--head-sha`, checkout HEAD는 같은 40자리 commit이어야 합니다.

## 안정화 기준

candidate 기본값은 다음 조건을 지속적으로 만족할 때 유지합니다.

- 권위 있는 source character coverage 100%
- missing·duplicate atom 0
- valid table grid와 raw HTML 0
- 반복 실행의 canonical hash 안정성
- review rate, model call, cache hit, stage latency, peak memory 추적

legacy 제거는 최소 14일, 서로 다른 PDF 20개에서 위 기준을 확인한 뒤 별도 정책 변경으로 결정합니다.
