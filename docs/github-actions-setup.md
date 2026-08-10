# GitHub Actions 운영 설정

이 저장소는 public ARD 컨텐츠, GitHub Issue 승인, 같은 PR writeback, 숫자 릴리스와 승인된 후속 연계를 전제로 합니다.

## 자동 bootstrap

관리자는 웹 설정 대신 동일한 desired state를 CLI로 계획하고 적용할 수 있습니다.

```bash
uv run ard github bootstrap --repo kimohy/ard-ossie-provider --dry-run
uv run ard github bootstrap --repo kimohy/ard-ossie-provider
```

두 번째 명령은 redacted plan 확인 뒤 LLM API key를 숨김 입력으로 요청하며,
key는 `gh secret set ... --env ard-llm`의 표준 입력에만 전달됩니다. 기존 key 교체는
기본적으로 거부됩니다. 초기 `main` 보호는 두 required status, 최신 base, PR 및 conversation
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

LLM Secret은 repository-level Secret이 아니라 보호된 Environment에 둡니다. `Settings → Environments`에서 `ard-llm`을 만들고 required reviewers와 허용 branch를 지정한 뒤 아래 Secrets를 그 환경에 설정합니다.

Secrets:

- `ARD_LLM_API_KEY` — 필수 OpenAI-compatible API 키
- `ARD_LLM_BASE_URL` — endpoint 자체를 숨겨야 하는 경우에만 선택적으로 사용

Repository 또는 `ard-llm` Environment Variables:

- `ARD_LLM_BASE_URL` — Secret이 없을 때 사용할 endpoint; 기본값 `https://api.openai.com/v1`
- `ARD_LLM_MODEL` — 필수 provider model 이름
- `ARD_LLM_API_STYLE` — `chat_completions`
- `ARD_MAX_ATTACHMENT_BYTES` — 파일 하나의 최대 byte; 기본값 `52428800`

변경 경로 검사를 통과한 same-repository 브랜치만 `ard-llm` 환경 승인을 요청합니다. workflow는 API 키를 승인 전 Issue job, fork PR, artifact, commit 또는 PR 코멘트에 전달하지 않습니다. Secret 값은 public ARD 문서에 절대로 포함하면 안 됩니다.

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

### 최초 설치 PR bootstrap

`main`에 아직 프로젝트와 trusted CLI가 없는 PR #1만
`.github/workflows/ard-initial-bootstrap.yml`을 사용합니다. 이 workflow는 PR 번호, 초기
base commit, same-repository head branch를 모두 고정하고, 검증 코드도 이미 독립 검증된
commit `cb79416c4585d383181e75e7f87579bbf368ca65`에서 checkout합니다. 후보 exact head는
별도 checkout에 두고 `static`, `pytest`, `wheel`을 각각 다른 read-only matrix runner에서
실행합니다.

bootstrap에는 Secret, status/PR 쓰기 권한, persisted checkout credential이 없습니다.
또한 check 이름을 `ard/quality-gate` 또는 `ard/changeset`으로 만들지 않으므로 병합 뒤
비활성화된 bootstrap job이 영구 required status를 대신 만족할 수 없습니다. PR #1 병합
후에는 기본 브랜치의 `ARD repository change gate`만 신뢰 가능한 집계와 status 게시를
담당합니다. bootstrap workflow는 후속 정리 PR에서 삭제할 수 있습니다.

## 5. 승인 환경

`Settings → Environments`에서 `production-linkage` 환경도 생성합니다.

- required reviewers를 지정합니다.
- 가능하면 `main` branch만 deployment branch로 허용합니다.

제품/테이블 태그와 GitHub Release가 만들어진 뒤 이 환경의 승인을 받아야 `ard_product_released` repository dispatch가 발행됩니다. payload에는 product ID, 숫자 버전, tag, merged commit과 artifact SHA-256만 포함합니다.

dispatch가 성공하고 status 기록만 실패한 경우 재시도에서 같은 event가 다시 전달될 수 있습니다.
downstream은 `(product_id, version, tag, commit)`을 중복 제거 키로 사용해야 합니다.

## 6. Issue 처리

1. 제출자는 `AI Ready Data submission` Form에 HTML, DOCX/PDF, XLSX를 각각 하나씩 첨부합니다.
2. 내용과 첨부가 public 공개 가능한지 검토합니다.
3. 관리자가 `ard:approved`를 붙입니다.
4. `ARD approved issue intake` workflow가 Draft PR을 만듭니다.
5. 변환 결과와 보고서를 검토하고 누락 경고를 보완합니다.
6. hard error가 0이고 두 required status가 성공한 뒤 병합합니다.

Issue 첨부에는 외부 URL을 쓰지 않습니다. GitHub Issue에 직접 업로드된 파일만 허용됩니다.

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

## 9. 설치 후 점검

```bash
uv sync --frozen
git lfs install
git lfs pull
uv run pytest -q
actionlint .github/workflows/*.yml
uv run ruff check src tests
```

테스트용 실제 API 키를 public branch나 fixture에 넣지 마세요.

## 10. Result와 재시도

Lifecycle 결과는 `.ard/run/` 아래 version 1 JSON envelope로 기록됩니다. exit `30`은 원격/API
일시 장애이므로 같은 exact head에서 재시도합니다. exit `70`은 tag, commit, dispatch 같은 일부
mutation이 이미 성공했을 수 있음을 뜻하므로 result의 `outputs`와 `mutations`를 보존하고 같은
입력으로 수렴시킵니다. immutable tag를 이동하거나 managed branch를 강제로 덮어쓰지 않습니다.
