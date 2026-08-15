# ARD 변환 정책과 거버넌스

이 문서는 ARD Ossie Provider가 공개 문서를 변환하고 게시할 때 지켜야 하는 현재 정책을 정의합니다. 구현 세부 흐름은 [시멘틱 PDF 파이프라인](semantic-pdf-pipeline.md), GitHub 권한과 Secret 설정은 [GitHub Actions 운영 설정](github-actions-setup.md), 장애 대응은 [Semantic PDF 운영 가이드](operations/semantic-pdf-rollout.md)를 따릅니다.

## 문서의 지위

동작과 문서가 충돌할 때는 다음 순서로 판단합니다.

1. JSON Schema와 실행 가능한 검증 invariant
2. 현재 `main`의 CLI, 파이프라인, GitHub Actions 구현
3. 이 정책 문서와 현재 운영 문서
4. README의 요약
5. `docs/superpowers/specs/`와 `docs/superpowers/plans/`의 과거 설계·구현 기록

과거 설계서는 결정의 배경을 설명하지만 현재 계약을 덮어쓰지 않습니다. 정책 변경은 코드, 스키마, 검증 테스트와 문서를 같은 PR에서 일치시켜야 합니다.

## Source authority

시멘틱 문서의 게시 문자열은 권위 있는 원문에서만 나옵니다.

- DOCX는 OOXML의 원문 텍스트를 사용합니다.
- PDF는 모든 페이지에 쓸 수 있는 내장 텍스트가 있으면 PDFium 추출 결과를 사용합니다.
- 내장 텍스트가 불완전하면 문서 전체 OCR 결과를 그 실행의 source authority로 사용합니다.
- 내장 텍스트 페이지와 OCR 페이지를 한 문서에서 섞지 않습니다.
- Docling 결과는 block 종류, 표 구조, 읽기 순서 같은 구조 후보를 만드는 힌트입니다. 게시 문자를 새로 쓰는 권위가 아닙니다.

반복 머리글, 반복 바닥글, 페이지 번호처럼 제외 가능한 요소도 layout proof와 source atom 기록을 남깁니다. 공백이 아닌 source atom은 게시 block 또는 검증된 제외 기록에 정확히 한 번 속해야 합니다.

## 결정적 코드와 LLM의 권한

| 영역 | 결정적 코드 | LLM |
|---|---|---|
| 제품·테이블·컬럼 ID와 버전 | 생성, 재사용, 충돌·retire 검증 | 변경 불가 |
| 물리 이름, 타입, PK/FK | Excel과 Registry로 결정 | 변경 불가 |
| relationship | FK 좌표를 현재 제품 테이블에 해석 | 생성 권한 없음 |
| 시멘틱 구조 | 후보 생성, allowlist, invariant 검증, canonical 조립 | allowlist 후보 선택 가능 |
| 공백 | 문자·hard break·protected token 경계 검증 | 제한된 whitespace repair 제안 가능 |
| metric | 안전한 ANSI SQL과 참조 검증 | 근거가 있는 선택적 제안 가능 |
| 게시·릴리스 | 전역 검증, 원자적 승격, tag와 Release | 권한 없음 |

LLM 응답은 제안입니다. 선택된 후보나 생성된 공백 복구도 결정적 검증을 통과하기 전에는 canonical 결과가 아닙니다. 모델 confidence가 높아도 문자 보존, source binding, atom ownership, 순서, 표 grid, Markdown 안전성 검증을 우회할 수 없습니다.

## 공백 복구 정책

한글과 다국어 문서에서 추출기가 단어 내부에 공백을 삽입하거나 필요한 공백을 잃을 수 있습니다. 다음 순서로만 복구합니다.

1. 결정적 scorer가 최대 5개의 동일 문자열 후보를 만듭니다.
2. 후보 중 하나가 충분히 확실하면 결정적으로 선택합니다.
3. 판단이 필요한 경우 LLM은 allowlist 안에서 후보를 선택합니다.
4. 기존 후보가 모두 부적절하면 LLM은 whitespace-only 후보를 한 번 생성할 수 있습니다.
5. 생성 후보는 별도의 verification 요청과 결정적 invariant 검증을 모두 통과해야 합니다.

생성 후보는 공백을 제외한 정확한 문자 순서, hard line boundary, 변경 불가 boundary를 보존해야 합니다. 이메일, URL, 날짜·시간, qualified identifier, 영숫자 ID, 숫자와 단위 같은 protected token을 분리해서는 안 됩니다. 같은 token이 반복되거나 더 긴 token 안에 부분 문자열로 포함되어도 occurrence 단위로 검증합니다.

verification 응답의 `validation_codes=[]`가 정상 계약입니다. 호환성을 위해 단독 성공 sentinel인 `VALID`는 결함으로 취급하지 않지만, `VALID`와 실제 결함 코드가 함께 오면 실제 결함을 유지해 후보를 거부합니다.

## 저신뢰와 검토 부채

기본 model confidence 하한은 `0.80`입니다. 낮은 primary vote에는 recovery vote를 최대 한 번 허용하고, 유효한 vote가 충돌할 때만 독립 tie-break vote를 최대 한 번 허용합니다.

- 같은 allowlist 후보에 대한 유효한 합의가 생기면 `selected`와 `LLM_LOW_CONFIDENCE_RECOVERED`를 기록합니다.
- 공백 생성과 독립 검증이 성공하면 `selected`, `recovery_status=generated`, `LLM_SPACING_REPAIR_APPLIED`를 기록합니다.
- 모델 판단이 끝나도 결정적 invariant를 만족하는 기존 후보가 있으면 그 후보를 `deferred_review`로 적용하고 변환을 계속합니다.
- 모든 후보에 결정적 결함이 있으면 안전한 fallback이 없으므로 `review_required`로 남기고 게시를 차단합니다.

`deferred_review`는 검토를 없애는 것이 아닙니다. `semantic-review.json`에 fallback, 후보 점수, attempt별 request hash, confidence, 상태, validation code, retry·repair 횟수를 남겨 사람이 이후 개선할 수 있게 합니다. 같은 입력의 trusted audit를 재사용할 때도 request, evidence, 후보 집합과 terminal attempt의 합의를 다시 검증합니다.

## 게시 판정

개별 decision과 canonical/generated 승격 상태는 서로 다른 층입니다. 아래 `게시`는 검증된 파일을 제품 branch/PR에 승격한다는 의미이며, immutable 숫자 릴리스 자격은 별도로 `verified`를 요구합니다.

| 상태 | 의미 | 게시 |
|---|---|---|
| `selected` | 결정적·모델·복구 후보 하나가 선택됨 | 전역 검증 결과에 따름 |
| `deferred_review` | 안전한 fallback을 적용했고 검토 부채가 남음 | 전역 invariant가 통과하면 generated output·PR 가능, release 불가 |
| `review_required` | 적용할 안전한 후보가 없음 | 불가 |
| `verified` | 모든 decision과 전역 invariant가 해결됨 | canonical 게시 가능, 다른 release gate도 통과하면 immutable release 가능 |
| `review_pending` | fallback을 적용했고 전역 invariant가 통과함 | generated output·PR 계속 가능, `WARN`과 review 기록 유지; immutable release는 불가 |
| `failed` | source loss, 중복, grid, Markdown 등 invariant 위반 | 불가 |

품질 최종 상태는 다음처럼 해석합니다.

- `PASS`: 필수 invariant와 품질 조건이 모두 통과했고 경고가 없습니다.
- `WARN`: canonical 생성이 가능한 결과지만 OCR 사용, 감사된 LLM 복구, review debt 또는 제외된 선택적 제안이 있습니다. `review_pending`이면 PR에서 후속 검토를 계속할 수 있지만 immutable release는 아직 허용하지 않습니다.
- `FAIL`: 필수 invariant 또는 필수 데이터 계약이 깨졌습니다. 기존 게시 가능한 상태는 원자적 승격 전에 보존합니다.

경고를 일괄 오류로 승격하는 운영 모드가 아니라면 `WARN` 자체는 파이프라인 중단 사유가 아닙니다.

## 선택적 제안과 필수 데이터

LLM metric과 시멘틱 설명은 선택적입니다. `LLM_METRIC_SQL_UNSAFE`가 발생하면 해당 provider suggestion만 제외하고 일반화된 경고를 남깁니다. 안전한 기존 metric, 다른 제안, 문서와 Registry 처리는 계속됩니다. 제외된 이름을 기존 Registry metric 삭제 신호로 사용하지 않습니다.

다음 항목은 선택적 제안이 아니므로 오류를 무시할 수 없습니다.

- 제품·테이블·컬럼 identity와 버전 충돌
- 물리 schema와 PK/FK 무결성
- 존재하지 않는 table/column을 참조하는 relationship
- source character 손실·중복 또는 허가되지 않은 제외
- 불완전하거나 겹치는 table grid
- raw HTML, 비어 있는 Markdown, source/configuration hash 불일치
- immutable tag가 다른 commit을 가리키는 충돌

## 감사와 개인정보

기본 공개 diagnostics는 검증과 재현에 필요한 식별 정보만 저장합니다.

저장하는 정보:

- source/configuration/canonical/request hash
- candidate ID, 점수, decision source와 outcome
- attempt phase, confidence, 상태, validation code, provider retry·repair 횟수
- application outcome, 전역 validation 상태와 invariant code
- 제한된 masked preview와 image hash

저장하지 않는 정보:

- API key와 service-account JSON
- 원문 prompt와 provider response 전문
- unrestricted source text, page image bytes, 생성 후보 전문
- Secret을 포함할 수 있는 예외·명령 출력

보호된 진단을 명시적으로 켜지 않는 한 raw source와 image는 기록하지 않습니다. Issue, PR comment, artifact, commit에 Secret이나 비공개 원문을 복사하지 않습니다.

## GitHub 신뢰 경계

- 승인 전 Issue와 fork PR에는 LLM Secret과 write 권한을 전달하지 않습니다.
- `ard:approved`는 `write`, `maintain`, `admin` 권한이 있는 관리자가 적용해야 합니다.
- 보호된 processor는 기본 브랜치의 trusted CLI를 실행하고 candidate checkout은 데이터와 Git state로만 읽습니다.
- 코드·workflow·문서 PR과 ARD data PR을 섞지 않습니다.
- Issue는 처리 성공만으로 닫지 않습니다. 제품 PR 본문의 `Closes #N`이 병합될 때 GitHub가 닫는 것이 정상 완료 경로입니다.
- 처리 실패나 Draft PR 생성만으로 Issue를 닫아서는 안 됩니다.

## 버전·릴리스·재시도

`validation-report.json`의 `publishable`은 canonical/generated 결과를 원자적으로 승격하고 PR 처리를 계속할 수 있다는 의미입니다. 숫자 릴리스는 더 엄격하며 semantic validation status가 정확히 `verified`이고 `publishable=true`여야 합니다. 따라서 `review_pending` PR은 검토 부채를 해결하고 다시 처리해 `verified`가 되기 전에는 병합·릴리스하지 않습니다.

제품과 테이블은 독립적인 숫자 버전을 가지며 tag는 `product/<product-id>/vN`, `table/<table-id>/vN` 형식입니다. annotated tag를 만들기 전에 repository-local GitHub Actions bot identity를 설정합니다.

- 기존 tag가 같은 commit을 가리키면 재사용합니다.
- 기존 tag가 다른 commit을 가리키면 `TAG_TARGET_CONFLICT`로 차단하고 이동하지 않습니다.
- GitHub Release asset은 검증된 bundle hash와 일치해야 합니다.
- downstream dispatch의 중복 제거 키는 `(product_id, version, tag, commit)`입니다.
- 성공 status가 이미 있으면 같은 dispatch는 `noop`입니다.
- exit `30`은 일시 장애이므로 같은 exact input으로 재시도합니다.
- exit `70`은 일부 원격 mutation이 발생했을 수 있으므로 result envelope와 mutation journal을 보존하고 같은 입력으로 수렴시킵니다.

강제 tag 이동, managed branch 강제 push, 검증되지 않은 asset 교체는 복구 수단으로 사용하지 않습니다.
