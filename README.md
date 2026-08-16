# ARD Ossie Provider

ARD Ossie Provider는 비공개 저장소에 반입이 승인된 AI Ready Data 문서를 검증하고 Apache Ossie 0.1.1 모델로 변환하는 컴파일러입니다. 데이터 제품 HTML, 시멘틱 DOCX/PDF, 데이터 딕셔너리 XLSX를 GitHub Issue 또는 신뢰된 branch로 받아, 사람이 읽는 Markdown과 기계가 읽는 모델·Registry·품질 보고서를 함께 생성합니다.

## 입력과 결과

제품 하나는 다음 source를 가집니다.

- 제품 설명 HTML 1개
- 시멘틱 DOCX 또는 PDF 1개
- 데이터 딕셔너리 XLSX 1개

성공한 변환은 다음 핵심 산출물을 만듭니다.

| 경로 | 결과 |
|---|---|
| `generated/data-product.md` | 제품 설명과 데이터셋 요약 |
| `generated/data-semantic.md` | source 문자에 결합된 canonical 시멘틱 Markdown |
| `generated/data-dictionary.json` | 정규화한 테이블·컬럼 dictionary |
| `generated/ossie-model.json` | Apache Ossie 0.1.1 모델 |
| `generated/source-manifest.json` | 입력 파일과 SHA-256 binding |
| `quality/*.json` | fidelity, 후보, 결정, 적용, 검증, 중복, 버전, 영향, LLM 제안 감사 |
| `registry/**` | 불변 ID, 현재 버전, product-table mapping과 changeset |

## 사용자가 기대할 수 있는 보장

1. **원문 보존:** PDF는 완전한 내장 텍스트 또는 문서 전체 OCR 중 하나를 source authority로 사용합니다. Docling과 LLM은 원문 문자를 대체하지 않습니다.
2. **결정적 소유권:** 물리 이름, 타입, PK/FK, ID, 버전, relationship, 게시 여부는 검증 가능한 코드가 결정합니다.
3. **제한된 LLM 보조:** LLM은 allowlist 후보 선택과 검증된 whitespace-only 복구만 수행합니다. 낮은 confidence는 bounded recovery와 독립 검증을 거칩니다.
4. **감사 가능한 계속 처리:** 안전한 fallback이 있으면 사람의 후속 검토 기록을 남기고 변환을 계속합니다. 안전한 후보가 없거나 전역 invariant가 깨지면 게시하지 않습니다.
5. **불변 게시:** Registry와 생성물은 원자적으로 승격하며 과거 버전은 exact commit, immutable tag, GitHub Release로 추적합니다.

정확한 정책은 [ARD 변환 정책과 거버넌스](docs/policy-and-governance.md)에 정의되어 있습니다.

## 처리 흐름

```text
HTML + DOCX/PDF + XLSX
          │
          ▼
source·권한·LFS 검증
          │
          ▼
결정적 파싱과 evidence 생성
          │
          ▼
bounded candidate + LLM 보조 판단
          │
          ▼
canonical 조립과 전역 invariant 검증
          │
          ▼
generated + quality + Registry 원자적 승격
          │
          ▼
PR gate → merge → immutable Release → downstream dispatch
```

시멘틱 PDF의 candidate, 공백 복구, heading, table, diagnostics 계약은 [시멘틱 PDF 파이프라인](docs/semantic-pdf-pipeline.md)을 참고하세요.

## GitHub 사용

### 승인된 Issue

1. `AI Ready Data submission` Issue Form에 제품 하나와 source 세 개를 첨부합니다.
2. 저장소 반입 권한과 조직 정책 준수 여부를 검토한 관리자가 `ard:approved`를 적용합니다.
3. trusted workflow가 첨부와 권한을 검증하고 `ard/issue-<number>-<product-key>` Draft PR을 만듭니다.
4. 같은 PR에 generated, quality, Registry 변경을 기록합니다.
5. hard error가 0이고 `ard/quality-gate`와 `ard/changeset`이 정확한 PR head에서 성공해야 병합할 수 있습니다. candidate PDF라면 추가로 `validation-report.json`이 `status=verified`, `publishable=true`여야 하며, `WARN`은 validation이 계속 `verified`인 경우에만 병합할 수 있습니다.
6. 제품 PR의 `Closes #N`이 병합되면 Issue가 닫히고 숫자 릴리스가 실행됩니다.

처리 실패나 Draft PR 생성만으로 Issue를 닫지 않습니다. `ard:failed`를 해결해 같은 제품 PR이 검증을 통과하도록 재처리합니다.

### 직접 branch 변경

신뢰된 same-repository non-main branch의 `products/<product-key>/sources/**` 변경도 같은 processor를 사용합니다. 한 변경에는 정확히 한 제품만 허용합니다. fork PR에는 Secret이나 writeback 권한을 전달하지 않고 정적 source/schema 검사만 수행합니다.

PDF/DOCX/XLSX는 Git LFS 객체로 관리합니다. code·workflow·문서 변경과 `products/`·`registry/` data 변경을 한 PR에 섞으면 gate가 차단합니다.

GitHub Environment, Secret, Variable, branch protection 설정은 [GitHub Actions 운영 설정](docs/github-actions-setup.md)을 따릅니다. Enterprise Cloud 또는 GHES의 신규 저장소로 옮길 때는 [GitHub Enterprise 이전 및 신규 저장소 구축](docs/github-enterprise-migration.md)의 호환성 게이트를 먼저 적용합니다. 이 매뉴얼의 GHES 기준은 3.18.12이며, 운영 전 3.18.13 보안 hotpatch가 필요합니다.

## 로컬 빠른 시작

Python 3.12, `uv`, Git LFS가 필요합니다.

```bash
uv sync --frozen
git lfs install
git lfs pull
uv run --frozen ard process products/500138301 --registry registry
```

주요 명령을 확인합니다.

```bash
uv run --frozen ard --help
uv run --frozen ard workflow --help
uv run --frozen ard parse --help
uv run --frozen ard model --help
uv run --frozen ard validate --help
uv run --frozen ard release --help
uv run --frozen ard github --help
uv run --frozen ard llm --help
```

제품 이력과 영향을 조회할 수 있습니다.

```bash
uv run --frozen ard history 500138301
uv run --frozen ard diff 'product-key@v1..v2'
uv run --frozen ard impact table <table-id> --registry registry
```

## LLM 프로필

모델, API 방식, timeout과 output 한도는 review되는 `config/llm-profiles.yaml`이 소유합니다. runtime에서 모델명을 임의로 바꾸지 않습니다.

```bash
export ARD_LLM_PROFILE='openai-compatible-default'
export ARD_LLM_BASE_URL='https://api.openai.com/v1'
read -s ARD_LLM_API_KEY
export ARD_LLM_API_KEY

uv run --frozen ard llm profiles
uv run --frozen ard llm validate
uv run --frozen ard llm smoke-test --profile "$ARD_LLM_PROFILE"
```

OpenAI-compatible, Azure OpenAI, Vertex AI Gemini, Vertex AI Claude 프로필을 지원합니다. API key와 service-account JSON은 파일, fixture, log, Issue, PR에 저장하지 않습니다. 운영 연결 검증은 보호된 `ARD LLM provider smoke test` workflow에서 수행합니다.

## 결과 해석

### 품질 상태

| 상태 | 의미 | 다음 조치 |
|---|---|---|
| `PASS` | 경고 없이 필수 조건 통과 | 게시 가능 |
| `WARN` | canonical 생성은 가능하지만 OCR, 검증된 복구, review debt 또는 제외된 선택적 제안이 있음 | audit와 validation status 확인 |
| `FAIL` | source loss, schema, identity, table grid 등 필수 계약 위반 | 게시하지 않고 원인 수정 |

`WARN`을 자동 실패로 해석하지 않습니다. `quality-report.json`의 `hard_errors`, `validation-report.json`의 `status`와 `publishable`, `application-report.json`의 실제 적용 결과를 함께 확인합니다. 단, `review_pending`의 `publishable=true`는 generated output과 PR을 계속 만들 수 있다는 뜻이며 숫자 릴리스 허가는 아닙니다. immutable release는 semantic validation이 `verified`일 때만 가능합니다.

### Lifecycle exit code

| exit | 의미 | 처리 |
|---:|---|---|
| `0` | success/no-op | 다음 단계 진행 |
| `10` | validation | 입력·schema·fidelity 수정 |
| `20` | configuration | profile·Environment 설정 수정 |
| `30` | transient | 같은 exact input으로 재시도 |
| `40` | conflict | head·version·tag 충돌 해결 |
| `50` | security | 권한·경로·source 신뢰 경계 조사 |
| `70` | partial mutation | result와 mutation journal로 같은 입력을 수렴 |

Lifecycle 결과는 `.ard/run/<command>-result.json` version 1 envelope에 원자적으로 기록됩니다. 장애와 rollback, release 수렴 절차는 [Semantic PDF 운영 가이드](docs/operations/semantic-pdf-rollout.md)를 참고하세요.

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
  quality/

registry/
  products/
  tables/
  mappings/
  changesets/
  indexes/
```

제품 폴더에 버전별 사본을 중복 저장하지 않습니다. 현재 상태만 파일로 보관하고 과거 상태는 Git과 `product/<product-id>/vN`, `table/<table-id>/vN` tag로 조회합니다.

## 문서 지도

현재 계약과 운영 문서:

- [ARD 변환 정책과 거버넌스](docs/policy-and-governance.md)
- [시멘틱 PDF 파이프라인](docs/semantic-pdf-pipeline.md)
- [GitHub Actions 운영 설정](docs/github-actions-setup.md)
- [GitHub Enterprise 이전 및 신규 저장소 구축](docs/github-enterprise-migration.md) — GHES 3.18.12 기준과 3.18.13 운영 보안 gate 포함
- [Semantic PDF 운영, 검증, rollback](docs/operations/semantic-pdf-rollout.md)
- [완료 기록과 다음 작업](docs/next-steps.md)

역사적 설계와 구현 기록:

- [초기 ARD/Ossie 아키텍처 설계](docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md)
- [설계 기록 모음](docs/superpowers/specs/)
- [구현 계획 모음](docs/superpowers/plans/)

역사적 문서가 현재 코드나 위 정책 문서와 충돌하면 현재 구현과 현재 계약 문서를 따릅니다.

## 라이선스

Apache License 2.0
