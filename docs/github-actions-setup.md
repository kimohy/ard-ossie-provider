# GitHub Actions 운영 설정

이 저장소는 public ARD 컨텐츠, GitHub Issue 승인, 같은 PR writeback, 숫자 릴리스와 승인된 후속 연계를 전제로 합니다. 변환 판단의 정책은 [ARD 변환 정책과 거버넌스](policy-and-governance.md), PDF 처리 상태와 보고서 계약은 [시멘틱 PDF 파이프라인](semantic-pdf-pipeline.md)을 먼저 확인하세요. Enterprise Cloud 또는 GHES의 새 저장소를 구성한다면 이 문서보다 먼저 [GitHub Enterprise 이전 및 신규 저장소 구축](github-enterprise-migration.md)의 제품별 호환성 게이트를 완료합니다. 해당 매뉴얼은 GHES 3.18.12를 호환성 기준으로 삼고 3.18.13 보안 hotpatch를 운영 전제조건으로 둡니다.

## 자동 bootstrap

관리자는 웹 설정 대신 동일한 desired state를 CLI로 계획하고 적용할 수 있습니다.

```bash
uv run ard github bootstrap --repo kimohy/ard-ossie-provider --dry-run
uv run ard github bootstrap --repo kimohy/ard-ossie-provider
uv run ard github bootstrap --repo kimohy/ard-ossie-provider \
  --profile openai-compatible-default \
  --azure-endpoint https://RESOURCE.openai.azure.com \
  --gcp-project-id GCP_PROJECT_ID --dry-run
```

두 번째 명령은 redacted plan 확인 뒤 LLM API key를 숨김 입력으로 요청하며,
key는 `gh secret set ... --env ard-llm`의 표준 입력에만 전달됩니다. 기존 key 교체는
기본적으로 거부됩니다. bootstrap은 Variable과 기존 `ARD_LLM_API_KEY`만 수렴시키며 Azure와
Vertex Secret은 읽거나 교체하지 않습니다. 초기 `main` 보호는 두 required status, 최신 base, PR 및 conversation
resolution을 요구하지만 승인 수는 0으로 둡니다. 비소유자 writer가 준비된 뒤에만 다음 명령으로
승인 수를 1로 전환합니다.

```bash
uv run ard github enable-review-protection --repo kimohy/ard-ossie-provider
```

## 1. Repository Actions 설정

`Settings → Actions → General`에서 다음을 설정합니다.

- Actions 사용을 허용합니다.
- `Workflow permissions`는 기본 read로 유지하고 각 workflow의 명시적 권한을 사용합니다.
- `Allow GitHub Actions to create and approve pull requests`를 활성화합니다. 이 프로젝트는 PR 생성만 사용하며 자동 승인은 하지 않습니다.
- 외부 Action은 workflow에 기록된 40자리 commit SHA로 고정합니다.

모든 자동화는 repository `GITHUB_TOKEN`을 사용합니다. 별도 PAT를 저장하지 않습니다.

## 2. Secrets와 Variables

LLM Secret은 repository-level Secret이 아니라 보호된 Environment에 둡니다. `Settings → Environments`에서 `ard-llm`을 만들고 required reviewers와 허용 branch를 지정합니다. 운영 processor는 아래 고정 이름만 읽습니다.

| 종류 | 이름 | 용도 | 필수 조건 |
|---|---|---|---|
| Variable | `ARD_LLM_PROFILE` | `config/llm-profiles.yaml`의 기본 프로필 이름 | 항상 필수; 초기값 `openai-compatible-default` |
| Variable/Secret | `ARD_LLM_BASE_URL` | OpenAI-compatible endpoint | 해당 provider 선택 시 필수; 같은 이름의 Secret이 우선 |
| Secret | `ARD_LLM_API_KEY` | OpenAI-compatible API 키 | 해당 provider 선택 시 필수 |
| Variable | `ARD_AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint | Azure 프로필 선택 시 필수 |
| Secret | `ARD_AZURE_OPENAI_API_KEY` | Azure OpenAI API 키 | Azure 프로필 선택 시 필수 |
| Variable | `ARD_GCP_PROJECT_ID` | Vertex AI project ID | Gemini 또는 Claude 프로필 선택 시 필수 |
| Secret | `ARD_VERTEX_CREDENTIALS_JSON` | Vertex service-account JSON | Gemini 또는 Claude 프로필 선택 시 필수 |
| Variable | `ARD_MAX_ATTACHMENT_BYTES` | 파일 하나의 최대 byte | 선택; 기본값 `52428800` |
| Variable | `ARD_SEMANTIC_PDF_PIPELINE` | PDF parser mode: `candidate`, `shadow`, `legacy` | 선택; 미설정 시 `candidate` |

선택하지 않은 provider의 값은 없어도 됩니다. 선택된 프로필에 필요한 값만 읽고 검증합니다.
기존 `ARD_LLM_MODEL`과 `ARD_LLM_API_STYLE` Variable은 삭제합니다. 해당 값은 이제 아래처럼
review를 거치는 저장소 프로필에 들어갑니다.

```yaml
version: 1
defaults:
  timeout_seconds: 120
  max_output_tokens: 4096
  temperature: 0
profiles:
  openai-compatible-default:
    provider: openai_compatible
    model: gpt-5.6-terra
    max_output_tokens: model_maximum
    structured_output: native
    api: chat_completions
    base_url_env: ARD_LLM_BASE_URL
    api_key_env: ARD_LLM_API_KEY
  azure-production:
    provider: azure_openai
    model: AZURE_DEPLOYMENT_NAME
    structured_output: native
    api: responses
    endpoint_env: ARD_AZURE_OPENAI_ENDPOINT
    api_key_env: ARD_AZURE_OPENAI_API_KEY
  vertex-gemini-production:
    provider: vertex_gemini
    model: GEMINI_MODEL_ID
    structured_output: native
    project_env: ARD_GCP_PROJECT_ID
    location: global
    credentials_env: ARD_VERTEX_CREDENTIALS_JSON
  vertex-claude-production:
    provider: vertex_claude
    model: CLAUDE_MODEL_ID
    structured_output: prompt_json
    project_env: ARD_GCP_PROJECT_ID
    location: us-east5
    credentials_env: ARD_VERTEX_CREDENTIALS_JSON
```

placeholder model/deployment/region은 계정에서 실제로 허용된 값으로 교체해 PR review를 받아야
합니다. Claude는 Vertex AI 경로만 사용하며 Phase 1에서는 `structured_output: prompt_json`을
명시합니다. 프로필 이름, 모델, endpoint, region, API 방식은 Issue 내용이나 production
workflow input으로 전달할 수 없습니다. production은 관리자 Variable `ARD_LLM_PROFILE`만
사용합니다.

프로필을 기본값으로 바꾸기 전에 `main`의 **ARD LLM provider smoke test** workflow를 수동
실행합니다. 이 workflow는 `ard-llm` 승인을 받은 read-only job에서만 실제 text/structured
요청을 보내고 artifact를 만들지 않습니다.

```bash
uv run ard llm profiles
ARD_LLM_PROFILE=openai-compatible-default uv run ard llm validate
uv run ard llm smoke-test --profile openai-compatible-default
```

`validate`가 보호된 Environment 밖에서 credential 상태를 `unavailable`로 표시하는 것은
정상이며 credential 성공을 의미하지 않습니다. 실제 연결 검증은 보호된 smoke workflow로
수행합니다.

`ARD_SEMANTIC_PDF_PIPELINE=candidate`는 전역 invariant를 통과한 canonical Markdown을 생성합니다. 모델 판단이 끝나지 않아도 결정적으로 안전한 fallback이 있으면 `review_pending`으로 generated output과 PR 처리를 계속하고 `semantic-review.json`을 남깁니다. 안전한 후보가 없는 `review_required`와 전역 invariant가 깨진 `failed`는 canonical 승격을 차단합니다. `WARN`을 무조건 실패로 취급하지 말고 `validation-report.json`, `application-report.json`, `semantic-review.json`의 적용 상태를 함께 확인하세요. 다만 `review_pending` PR은 병합하지 않습니다. 숫자 릴리스는 semantic validation이 정확히 `verified`일 때만 허용되므로, 검토 부채를 해결한 뒤 재처리합니다.

`ard-llm`과 `production-linkage`의 deployment branch는 모두 `main`만 허용합니다. direct branch의 push workflow는 `contents: read` signal만 남기고, 기본 브랜치에서 로드되는 `workflow_run` coordinator가 exact candidate를 검증한 뒤 보호된 processor를 호출합니다. processor는 `trusted/`의 기본 브랜치 CLI만 실행하고 candidate checkout은 `--repository`로 지정한 데이터와 Git state로만 사용합니다. API 키는 credential-free validation job, fork PR, artifact, commit 또는 PR 코멘트에 전달하지 않습니다. Secret 값은 public ARD 문서에 절대로 포함하면 안 됩니다.

## 3. Labels

다음 라벨을 생성합니다.

| Label | 용도 |
|---|---|
| `ard:submission` | Issue Form으로 제출됨 |
| `ard:approved` | write 이상 관리자가 공개 수집과 LLM 처리를 승인함 |
| `ard:processing` | 수집/변환 진행 중 |
| `ard:failed` | 수집 또는 변환 실패 |
| `ard:pr-created` | 제품 Draft PR 생성 완료 |

`ard:approved`를 붙인 사용자의 collaborator permission이 `write`, `maintain`, `admin` 중 하나가 아니면 처리를 즉시 중단합니다.

## 4. Main branch 보호

`main` ruleset 또는 branch protection에 다음을 적용합니다.

- direct push 금지
- pull request 필수
- bootstrap 초기에는 review 0명, 비소유자 writer 준비 후 최소 1명 review 필수
- head branch가 최신 base를 포함하도록 요구
- conversation resolution 요구
- required status checks:
  - `ard/quality-gate`
  - `ard/changeset`

shared table 변경이 아닌 PR에도 `ard/changeset=success`가 게시됩니다. shared 변경은 모든 필수 제품이 같은 changeset에 준비되기 전까지 pending입니다.

코드·workflow·문서만 바꾸는 PR은 `ARD repository change gate`가 전체 테스트, Ruff와 actionlint를 실행한 뒤 같은 두 status를 게시합니다. `products/` 또는 `registry/` 변경을 코드 변경과 한 PR에 섞으면 `MIXED_CODE_AND_ARD_DATA_NOT_ALLOWED`로 차단합니다.

이 gate의 `pull_request_target` 정의는 항상 기본 브랜치에서 읽습니다. 정적 검증은
pristine `candidate/` checkout에서 후보 코드를 실행하지 않고 수행합니다. `pytest`와
wheel build는 각각 별도 matrix runner의 격리된 checkout에서 실행되며 모든 검증 job은
`contents: read`만 갖고 `GH_TOKEN`이나 status 쓰기 권한을 받지 않습니다. 두 required
status는 candidate 코드를 실행하지 않는 별도 finalizer job이 기본 브랜치의 CLI로만
게시합니다.

### 최초 운영 전환 기록

최초 구현 [PR #1](https://github.com/kimohy/ard-ossie-provider/pull/1)은
2026-08-11 merge commit
`d6603bd941523eff3de145361368e28df74347d3`로 병합했습니다. 병합 직후 repository
bootstrap을 적용해 다음 운영 계약을 구성했습니다.

- `ard-llm`과 `production-linkage`는 repository owner 승인을 요구하며 `main`에서만
  deployment할 수 있습니다.
- `ard-llm`에는 기본 `ARD_LLM_PROFILE`, provider endpoint/project Variables와 선택한
  provider의 Environment Secret이 있습니다. Secret 값은 운영 기록과 workflow log에
  남기지 않습니다.
- Actions 기본 권한은 read이며 workflow의 pull request 생성을 허용합니다.
- `main`은 pull request, 최신 base, conversation resolution,
  `ard/quality-gate`, `ard/changeset`을 요구하고 관리자 우회, force push, 삭제를 허용하지
  않습니다. 비소유자 writer가 준비되기 전까지 승인 수는 0입니다.

PR #1만 검증하던 일회성 `ard-initial-bootstrap.yml`은 운영 전환 뒤 제거했습니다. 이후
코드·workflow·문서 PR은 기본 브랜치의 `ARD repository change gate`만 사용하며, trusted
finalizer가 정확한 PR head에 두 required status를 게시합니다. P1 이후의 후속 acceptance는
[다음 작업 로드맵](next-steps.md)을 따릅니다.

## 5. 승인 환경

`Settings → Environments`에서 `production-linkage` 환경도 생성합니다.

- required reviewers를 지정합니다.
- `main` branch만 deployment branch로 허용합니다.

제품/테이블 태그와 GitHub Release가 만들어진 뒤 이 환경의 승인을 받아야 `ard_product_released` repository dispatch가 발행됩니다. payload에는 product ID, 숫자 버전, tag, merged commit과 artifact SHA-256만 포함합니다.

dispatch가 성공하고 status 기록만 실패한 경우 재시도에서 같은 event가 다시 전달될 수 있습니다.
downstream은 `(product_id, version, tag, commit)`을 중복 제거 키로 사용해야 합니다.

## 6. Issue 처리

1. 제출자는 `AI Ready Data submission` Form에 HTML, DOCX/PDF, XLSX를 각각 하나씩 첨부합니다.
2. 내용과 첨부가 public 공개 가능한지 검토합니다.
3. 관리자가 `ard:approved`를 붙입니다.
4. `ARD approved issue intake` workflow가 Draft PR을 만듭니다.
5. 변환 결과와 보고서를 검토합니다. `deferred_review`가 있으면 적용된 fallback과 attempt audit를 확인하고 후속 개선 사항을 기록합니다.
6. hard error가 0이고 두 required status가 성공한 뒤 병합합니다.

제품 PR 본문은 승인된 Issue를 `Closes #<issue-number>`로 연결합니다. Issue는 이 PR이 병합될 때만 정상 완료로 닫습니다. 처리 실패, `ard:failed`, Draft PR 생성만으로 Issue를 닫지 않습니다.

Issue 첨부에는 외부 URL을 쓰지 않습니다. GitHub Issue에 직접 업로드했을 때 생성되는 다음 두 형태만 최초 링크로 허용합니다.

- `https://github.com/user-attachments/assets/<UUID>`
- `https://github.com/user-attachments/files/<양의 숫자 ID>/<파일명>`

두 형식 모두 query와 fragment를 허용하지 않으며, `files` 형식은 정확히 한 개의 안전한 파일명 경로만 허용합니다. raw branch, repository raw route, avatar, 임의 `*.githubusercontent.com`과 asset storage 직접 링크는 거부합니다. 다운로드 redirect는 검증된 GitHub asset storage host만 허용하고 매 hop을 다시 검사합니다.

`create` Issue의 Existing product ID는 반드시 비워 두며 `update`에서만 기존 `prd_<uuidv7>` 값을 입력합니다. XLSX는 1행 영문 정규 헤더 형식과, 테이블별 시트에 `저장 플랫폼 및 세부 위치`, `테이블 명`, `테이블 설명`, `컬럼명`, `Type`, `Key 여부`, `Null 허용`을 갖는 한국어 Data Dictionary 형식을 지원합니다. 한국어 형식의 위치가 `catalog.schema` 두 부분이면 platform은 추론하지 않고 `unspecified`로 기록합니다. 대상 테이블과 컬럼이 없는 `FK` 표시는 관계로 생성하지 않습니다.

## 7. Shared table changeset

`ARD shared-table changeset coordinator`를 `create` 모드로 수동 실행합니다.

- `changeset_id`: `cst_<uuidv7>`
- `table_ids`: 쉼표로 구분한 변경 테이블 ID
- `product_ids`: 쉼표로 구분한 필수 제품 ID
- `initiating_pr`: 영향도 요약을 게시할 PR 번호

workflow는 중앙 Registry PR과 제품별 Draft 추적 PR을 생성합니다. 다음의 두 단계를 순서대로 운영합니다.

1. 최초 중앙 PR의 두 상태 검사를 확인하고 먼저 병합하여 빈 changeset 레코드를 main에 게시합니다.
2. 제품별 PR을 처리합니다. 각 성공 시 기록된 제품 버전, PR 번호와 정확한 head SHA가 중앙 readiness PR에 누적됩니다. 모든 제품이 준비되면 그 readiness PR을 제품 PR보다 먼저 병합하고, 이어서 모든 제품 PR을 병합합니다.

릴리스는 required 제품의 Registry 현재 버전이 readiness 버전과 같은지, 기록된 PR head가 바뀌지 않았는지, 모든 PR이 병합되었고 merge commit이 릴리스 commit의 조상인지 확인합니다. 마지막 제품 병합이나 readiness 레코드 병합 시 changeset에 속한 제품 전체가 다시 릴리스 대상으로 확장됩니다.

changeset이 끝난 뒤 다음의 독립 변경에서는 Issue의 Changeset ID를 비워 `product.yaml`의 값을 `null`로 전환합니다. 과거 changeset JSON과 제품별 추적 marker는 감사 이력으로 남지만 새 릴리스의 활성 changeset으로 재사용하지 않습니다.

## 8. 버전과 복구

- 제품/테이블 현재 버전은 Registry JSON 한 곳에만 존재합니다.
- 과거 상태는 Git commit, `product/<product-id>/vN`, `table/<table-id>/vN`, GitHub Release로 조회합니다.
- 성공 반영은 Registry, `generated/`, `quality/`를 모두 staging하고 함께 승격합니다. 승격 중 실패하면 세 영역을 이전 상태로 복구합니다. 검증 hard error에서는 기존 Registry와 생성물을 보존하고 상세 FAIL quality report를 남깁니다.
- 잘못된 자동 commit은 강제 덮어쓰기보다 revert PR로 복구합니다.
- 숫자 릴리스는 `main` push 중 `products/**` 또는 `registry/**` 변경에만 실행됩니다. 코드-only 수정 PR은 릴리스 workflow를 다시 시작하지 않습니다.
- annotated release tag를 만들기 전에 CLI가 repository-local `github-actions[bot]` identity를 설정합니다. 기존 tag가 같은 commit이면 재사용하고 다른 commit이면 `TAG_TARGET_CONFLICT`로 중단합니다.
- 실패한 run을 재실행하면 원래 commit의 workflow와 코드를 다시 사용합니다. 이후 코드 수정이 필요한 장애였다면 단순 rerun으로 수정 코드가 적용된다고 가정하지 마세요.

## 9. 설치 후 점검

```bash
uv sync --frozen
git lfs install
git lfs pull
uv run pytest -q
actionlint .github/workflows/*.yml
uv run ruff check src tests
uv run ard llm profiles
uv run ard llm validate --profile openai-compatible-default
```

테스트용 실제 API 키를 public branch나 fixture에 넣지 마세요.

## 10. Result와 재시도

Lifecycle 결과는 `.ard/run/` 아래 version 1 JSON envelope로 기록됩니다. exit `30`은 원격/API
일시 장애이므로 같은 exact head에서 재시도합니다. exit `70`은 tag, commit, dispatch 같은 일부
mutation이 이미 성공했을 수 있음을 뜻하므로 result의 `outputs`와 `mutations`를 보존하고 같은
입력으로 수렴시킵니다. immutable tag를 이동하거나 managed branch를 강제로 덮어쓰지 않습니다.

| exit | 분류 | 운영 조치 |
|---:|---|---|
| `0` | success 또는 no-op | result와 status를 확인하고 다음 단계 진행 |
| `10` | validation | 입력, schema, fidelity finding 수정 후 새 PR |
| `20` | configuration | profile, Variable, Environment 설정 수정 |
| `30` | transient | 같은 exact input으로 재시도 |
| `40` | conflict | head, branch, version, immutable tag 충돌 해결 |
| `50` | security | 권한·경로·source 신뢰 경계 위반 조사 |
| `70` | partial mutation | mutation journal과 원격 상태를 대조해 같은 입력으로 수렴 |

릴리스가 실패하면 먼저 artifact의 `workflow.release-product-result.json`을 확인합니다. `TAG_CREATE_FAILED`가 push 전에 발생했고 tag가 없다면 runner의 Git identity 경로를 점검합니다. tag 또는 Release가 일부 존재하면 삭제하거나 이동하지 말고 같은 product/version/commit과 bundle hash로 `release-product`를 재실행합니다. 성공 result를 얻은 뒤에만 `release-dispatch`를 실행합니다. 상세 명령과 검증 순서는 [Semantic PDF 운영 가이드](operations/semantic-pdf-rollout.md#릴리스-복구)를 따릅니다.
