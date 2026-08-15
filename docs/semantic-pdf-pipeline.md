# 시멘틱 PDF 파이프라인

이 문서는 현재 `candidate` 파이프라인이 PDF 원문을 감사 가능한 Markdown으로 변환하는 방법을 설명합니다. 정책 경계는 [ARD 변환 정책과 거버넌스](policy-and-governance.md), rollback과 장애 처리는 [Semantic PDF 운영 가이드](operations/semantic-pdf-rollout.md)를 참고하세요.

## 목표

파이프라인은 문자를 새로 쓰는 문서 생성기가 아니라, 권위 있는 source 문자를 구조화하는 컴파일러입니다. 정확한 문자를 보존하면서 heading, paragraph, list, table, caption과 읽기 순서를 복구하고, 모든 선택과 적용 결과를 hash로 결합된 보고서에 남깁니다.

## Runtime mode

`ARD_SEMANTIC_PDF_PIPELINE`은 PDF에만 적용되며 다음 값만 허용합니다.

| 값 | 동작 |
|---|---|
| `candidate` | canonical candidate 결과를 게시합니다. GitHub processor의 기본값입니다. |
| `shadow` | candidate를 실행하고 diagnostics를 만들지만 legacy Markdown을 게시합니다. |
| `legacy` | candidate 경로를 우회해 즉시 rollback합니다. |

DOCX 경로는 이 변수의 영향을 받지 않습니다.

## 처리 흐름

### 1. 원문 권위 선택

PDFium이 문서 전체에서 사용할 수 있는 내장 텍스트를 제공하면 `pdf_embedded`를 선택합니다. 그렇지 않으면 전체 문서 OCR을 사용합니다. 페이지별로 두 경로를 섞지 않습니다. source file SHA-256과 추출 결과의 source hash가 다르면 즉시 실패합니다.

### 2. Evidence 생성

권위 있는 문자를 immutable atom과 region으로 나눕니다. 각 atom은 page, bounding box, text와 hash에 연결됩니다. Docling은 논리 구조와 layout hint를 제공하지만 atom 문자를 대체하지 않습니다.

### 3. Candidate 생성

결정 유형별로 최대 5개의 bounded candidate를 만듭니다.

- OCR recognition hypothesis
- block kind와 heading/list metadata
- reading order와 cross-page continuation
- table grid와 cell 좌표
- region 또는 table-cell spacing

완전한 table grid처럼 invariant로 증명된 후보는 모델 호출 없이 결정적으로 선택할 수 있습니다.

### 4. Candidate adjudication

후보 점수와 증거가 충분하면 결정적 선택을 사용합니다. 판단이 필요한 경우 LLM에는 현재 candidate ID allowlist와 제한된 evidence만 전달합니다. trusted cache를 재사용할 때는 request hash, evidence hash, candidate 집합, provider/model, attempt audit와 terminal outcome을 다시 검증합니다.

### 5. 저신뢰 복구

model confidence가 `0.80` 미만이면 recovery vote를 한 번 요청합니다. 유효한 vote가 충돌할 때만 tie-break를 한 번 요청합니다. 같은 후보에 대한 회복 또는 2-of-3 합의만 `LLM_LOW_CONFIDENCE_RECOVERED`로 선택할 수 있습니다.

반복 vote로 비용을 무제한 늘리지 않습니다. 각 attempt는 `primary`, `recovery`, `tiebreak` phase와 독립 request hash를 가집니다.

### 6. Whitespace-only 생성과 검증

기존 spacing 후보로 충분하지 않을 때 생성 요청을 한 번 보낼 수 있습니다. 응답은 `rendered_text`, confidence, 제한된 repair reason만 반환합니다.

결정적 코드는 다음을 먼저 검사합니다.

- 공백을 제외한 exact character sequence 일치
- hard line boundary 보존
- immutable boundary 보존
- email, URL, 날짜·시간, identifier, 영숫자 ID, 숫자+단위 token 보존
- punctuation과 control whitespace 결함 부재
- table cell 바깥으로의 변경 금지

통과한 생성 후보도 별도의 verification 요청이 같은 candidate ID를 충분한 confidence로 선택해야 합니다. 정상 verification은 빈 `validation_codes`를 반환합니다. 단독 `VALID` sentinel은 빈 배열과 같이 처리하지만 실제 결함 code는 항상 후보를 거부합니다.

### 7. 안전한 fallback

모델이 실패하거나 합의하지 못해도 invariant-safe 후보가 있으면 파이프라인은 가장 안전한 후보를 적용해 `deferred_review`로 계속합니다. spacing은 deterministic defect가 없는 후보만 fallback 대상이며 source spacing 후보를 우선합니다.

안전한 후보가 하나도 없으면 `review_required`가 되고 게시하지 않습니다. 이 구분 때문에 사람의 검토가 필요한 판단을 기록하면서도 안전한 문서 변환 전체를 불필요하게 중단하지 않습니다.

### 8. Canonical 조립과 heading

선택된 후보를 canonical block으로 조립합니다. heading level은 각 fragment를 독립적으로 올리는 방식이 아니라 문서 hierarchy에서 유도하며 `1`부터 `6` 사이로 제한합니다. 같은 상위 구조의 연속 section은 같은 level을 유지하므로 페이지나 fragment 수가 늘어도 heading depth가 계속 증가하지 않습니다.

Markdown renderer는 canonical block만 입력으로 받습니다. source에 필요한 Markdown meta character는 문법이 아닌 문자로 보이도록 escape하되 CommonMark rendering 결과에 불필요한 backslash가 노출되어서는 안 됩니다. raw HTML은 허용하지 않습니다.

### 9. 전역 invariant 검증

개별 decision이 성공해도 다음 전역 검증이 최종 게시 여부를 결정합니다.

- evidence와 canonical의 source hash binding
- 모든 공백 아닌 source atom의 정확히 한 번 allocation 또는 증명된 제외
- 공백을 제외한 source character sequence 보존
- source whitespace disposition의 정확한 accounting
- layout DAG를 따르는 region order
- 빈틈과 overlap이 없는 table grid
- source-adjacent cross-page continuation
- 비어 있지 않고 raw HTML이 없는 Markdown
- stable canonical hash

`review_required` decision이 있으면 invariant finding이 없어도 canonical 승격을 차단합니다. `deferred_review`만 있으면 `review_pending`으로 generated output과 PR 처리를 계속하고 검토 부채를 남깁니다. 이 상태는 numeric release 대상이 아니며 검토 부채를 해결한 재처리가 `verified`가 되어야 릴리스할 수 있습니다.

### 10. 원자적 게시

generated 파일, quality 보고서, Registry snapshot을 candidate directory에 먼저 만듭니다. 검증이 끝나면 세 디렉터리를 함께 승격합니다. 중간 실패 시 이전 Registry와 게시 가능한 generated 상태를 복구합니다.

### 11. Release와 dispatch

병합된 제품/Registry 변경은 `ARD numeric release`가 감지합니다. 검증된 source snapshot으로 재현 가능한 ZIP을 만들고 제품·테이블 annotated tag를 exact commit에 생성한 뒤 GitHub Release asset을 게시합니다. release result가 성공한 경우에만 `ard_product_released` dispatch를 보내고 commit status를 기록합니다.

동일 입력을 다시 실행하면 같은 tag와 asset을 검증해 `noop`으로 수렴합니다.

## 상태 해석

| 범위 | 상태 | 의미 |
|---|---|---|
| decision | `selected` | 후보가 결정적·모델·합의·생성 경로로 선택됨 |
| decision | `deferred_review` | 안전한 fallback을 적용했으며 사람의 후속 검토가 필요함 |
| decision | `review_required` | 안전하게 적용할 후보가 없어 게시할 수 없음 |
| validation | `verified` | invariant와 decision이 모두 해결됨 |
| validation | `review_pending` | fallback canonical의 generated output·PR 계속 가능, immutable release 불가 |
| validation | `review_required` | unresolved decision 때문에 게시 불가 |
| validation | `failed` | 전역 invariant 위반으로 게시 불가 |
| fidelity | `PASS` | 경고 없이 충실도 조건 통과 |
| fidelity | `WARN` | canonical은 유효하지만 OCR, 복구 또는 review audit가 있음; release 여부는 validation status로 별도 판단 |
| fidelity | `FAIL` | source span 손실·중복 등 필수 충실도 위반 |

## Application outcome

`application-report.json`은 선택된 모든 decision이 최종 문서에 어떻게 반영됐는지 기록합니다.

| outcome | 의미 |
|---|---|
| `applied_existing_candidate` | 기존 후보가 canonical에 적용됨 |
| `applied_generated_repair` | 생성 후 독립 검증된 spacing 후보가 적용됨 |
| `applied_fallback_pending_review` | 안전한 fallback이 적용되고 review debt가 남음 |
| `not_published` | 선택 기록은 있지만 문서가 `review_required`라 게시되지 않음 |
| `rejected_by_invariant` | 선택 뒤 전역 invariant가 실패해 게시되지 않음 |

결정 confidence와 application outcome을 함께 봐야 합니다. decision이 `selected`여도 전역 invariant가 실패하면 적용 결과는 `rejected_by_invariant`입니다.

## 산출물과 보고서

### `generated/`

| 파일 | 용도 |
|---|---|
| `data-product.md` | 제품 설명과 데이터셋 요약 |
| `data-semantic.md` | canonical 시멘틱 Markdown |
| `data-dictionary.json` | 정규화한 테이블·컬럼 dictionary |
| `ossie-model.json` | Apache Ossie 0.1.1 모델 |
| `source-manifest.json` | 입력 파일과 hash binding |

### `quality/`

| 파일 | 용도 |
|---|---|
| `quality-report.json` | 전체 PASS/WARN/FAIL, hard error, warning, artifact hash |
| `duplicate-report.json` | ID, key, locator와 canonical 중복 검사 |
| `version-report.json` | 제품·테이블 버전 판정 |
| `impact-report.json` | 변경 영향과 changeset 필요성 |
| `llm-suggestions.json` | 수용된 선택적 설명·동의어·metric 감사 |
| `semantic-fidelity.json` | source coverage, block/table 수, OCR·degraded audit |
| `evidence-summary.json` | source/configuration hash와 evidence 집계 |
| `candidate-report.json` | candidate ID, score, 제한된 masked preview와 image hash |
| `decision-report.json` | terminal decision과 redacted attempt audit |
| `application-report.json` | 모든 selected/fallback decision의 실제 적용 결과 |
| `validation-report.json` | canonical hash, publishable, invariant finding과 model call 수 |
| `failure-report.json` | 실패 stage와 code의 최소 envelope |
| `manifest.json` | diagnostics 파일별 SHA-256 |
| `semantic-review.json` | `deferred_review`가 있을 때만 생성되는 후속 검토 기록 |
| `semantic-structure-repair.json` | 구조 복구를 요청하거나 trusted 결과를 재사용했을 때만 생성 |

기본 diagnostics는 generated spacing 전문과 source text를 저장하지 않습니다. decision report의 생성 후보는 hash 기반 identity로 제한하고 raw prompt/response/image는 공개 보고서에서 제외합니다.

## Issue #3 검증 기준점

제품 key `500138301`의 최종 v1 산출물은 실제 LFS PDF/XLSX를 hydrate한 뒤 독립 verifier를 통과했습니다.

| 항목 | 결과 |
|---|---|
| 페이지 | 5 |
| extraction mode | `pdf_embedded` |
| source text coverage | `1.0` |
| unmatched / duplicated span | `0 / 0` |
| degraded block | `0` |
| heading | 12, level `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]` |
| GFM table | 10 |
| decision | 65개 모두 `selected` |
| decision source | deterministic 46, model 18, generated 1 |
| application | existing candidate 64, generated repair 1 |
| unresolved decision | 0 |
| visible escape / raw HTML | `0 / 0` |

`semantic-fidelity.json`의 `WARN`은 적용·감사된 `LLM_SPACING_REPAIR_APPLIED` 때문이며 source 손실이 아닙니다. 별도로 `quality-report.json`에는 안전하지 않은 선택적 metric 제안 두 개를 제외한 `LLM_METRIC_SQL_UNSAFE` 경고가 있습니다. validation은 `verified`, `publishable=true`입니다.

실제 source를 받은 worktree에서 다음처럼 재검증합니다.

```bash
git lfs pull --include='products/500138301/sources/**'
uv run --frozen python scripts/verify_issue_3_semantic.py \
  --product-root products/500138301
```

verifier exit code `0`과 JSON의 `status`를 함께 확인합니다. LFS pointer 파일만 있는 checkout에서 실행한 결과는 실제 source 검증으로 인정하지 않습니다.
