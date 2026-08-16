# GitHub Enterprise 이전 및 신규 저장소 구축

이 문서는 ARD Ossie Provider를 GitHub Enterprise 환경의 새 저장소로 옮기고, 저장소 생성부터 GitHub Actions 운영 전환까지 검증하는 절차입니다. 기준일은 **2026-08-15**, 주 대상은 **GitHub Enterprise Server(GHES) 3.18.12**입니다. GHES는 기능과 bundled Action이 제품 버전에 묶이므로 설치 patch가 바뀌면 해당 버전의 release note와 Action runtime을 다시 확인해야 합니다.

## GHES 3.18.12 결론부터 확인합니다

GHES 3.18.12를 호환성 기준으로 사용할 수는 있지만, 현재 `main`을 그대로 실행할 수는 없습니다. 운영 cutover 전에는 아래 두 gate를 모두 통과해야 합니다.

1. **Appliance gate:** 3.18.12보다 뒤에 나온 3.18.13 hotpatch를 먼저 적용합니다. 3.18.13은 인증되지 않은 공격자가 Git LFS object, Release asset, 첨부 파일 등을 삭제할 수 있던 HIGH 등급 취약점을 수정합니다. 정확히 3.18.12로 고정된 환경은 격리된 사전 검증에만 사용하고, PDF/LFS 이전·Issue 첨부·Release 게시를 포함한 운영 cutover를 승인하지 않습니다.
2. **Repository gate:** 별도 GHES 호환성 PR에서 self-hosted runner, Node 20 Action, artifact v3 계열, deployment 권한, Issue 첨부 host를 수정하고 아래 smoke matrix를 통과합니다.

3.18 계열은 **2026-10-14**에 지원이 끝납니다. 3.18.13 적용은 단기 보안 gate이고, 3.20 또는 3.21로의 feature upgrade는 별도 일정으로 즉시 계획합니다.

## 먼저 대상 제품을 구분합니다

| 대상 | 현재 저장소와의 거리 | 권장 판단 |
|---|---|---|
| GitHub Enterprise Cloud, `github.com` | 가장 작음 | 현재 workflow를 기준으로 설정·Secret·보호 규칙을 다시 만들고 검증 |
| GitHub Enterprise Cloud with data residency, `*.ghe.com` | 중간 | runner와 API host는 지원되지만 Issue 첨부 host 호환성 수정 필요 |
| GitHub Enterprise Server | 큼 | self-hosted runner, Action mirror, artifact v3 계열, 첨부 host 정책을 반영한 별도 호환성 변경 필요 |

**중요:** 현재 `main`의 workflow를 GHES 3.18.12에서 바로 활성화하지 마세요. `ubuntu-24.04` GitHub-hosted runner, Node 24 기반 Action, `actions/upload-artifact@v7`, `actions/download-artifact@v4`, deployment 권한, `github.com` 전용 Issue 첨부 URL 검증 때문에 현재 상태는 GHES용 drop-in 구성이 아닙니다.

## 1. 이전 전 기록과 동결

먼저 source 저장소의 exact 상태와 운영 설정을 별도 보안 위치에 기록합니다.

- default branch와 현재 commit SHA
- 모든 branch와 annotated/lightweight tag
- Git LFS object 수와 용량
- GitHub Release와 첨부 asset
- open Issue/PR, label, milestone, CODEOWNERS와 팀 매핑
- Actions Secret·Variable의 **이름과 scope만** 기록하고 값은 내보내지 않음
- `ard-llm`, `production-linkage` Environment의 reviewer와 branch policy
- branch protection 또는 ruleset과 required status 이름
- self-hosted/larger runner와 runner group
- webhook, GitHub App, deploy key, OIDC trust, repository dispatch consumer
- 최근 성공한 `ARD repository change gate`, Issue 처리, numeric release run의 URL과 SHA

이전 창구를 연 뒤에는 source 변경을 동결합니다. GitHub Enterprise Importer는 delta migration을 지원하지 않으므로 시험 이전과 본 이전 사이의 변경은 자동 합쳐지지 않습니다.

## 2. 새 Enterprise 저장소 준비

1. Enterprise 안에 전용 organization과 **private** 저장소를 만듭니다.
2. default branch는 `main`으로 계획하되 아직 workflow를 실행하지 않습니다.
3. Actions를 repository 수준에서 비활성화하거나 허용 정책을 빈 allowlist로 둡니다.
4. source 저장소와 동일한 merge 정책을 사용할 관리자와 비소유자 reviewer를 준비합니다.
5. Git LFS, artifact, cache, release bundle을 수용할 quota와 retention을 확인합니다.
6. GHES라면 site administrator가 Actions와 외부 blob storage를 먼저 구성해야 합니다. GHES는 GitHub-hosted runner를 지원하지 않으므로 self-hosted runner도 준비합니다.

CLI로 빈 저장소를 만들 때도 host와 owner를 명시하고 자동 push는 하지 않습니다.

```bash
export GH_HOST='github.example.com'
gh repo create ENTERPRISE_ORG/ard-ossie-provider \
  --private \
  --description 'ARD document validation and Ossie publication'
gh repo view ENTERPRISE_ORG/ard-ossie-provider \
  --json nameWithOwner,visibility,defaultBranchRef
```

생성 직후 `Settings → Actions → General`에서 Actions를 비활성화한 상태인지 확인합니다. Enterprise 정책이 repository 설정을 강제할 수 있으므로 UI의 실제 effective policy를 기준으로 판단합니다.

GitHub Enterprise Importer를 사용한 Cloud 이전에서는 workflow 파일은 이동하지만 Actions secrets, variables, environments, runners, artifacts, run history, rulesets, Git LFS object는 이동하지 않습니다. Branch protection도 일부 예외·우회·deployment 조건이 빠질 수 있습니다. 따라서 이전 완료를 곧바로 운영 준비 완료로 간주하면 안 됩니다.

## 3. 저장소 데이터 이전

### Enterprise Cloud로 metadata까지 이전

Issue, PR, release와 사용자 이력을 보존해야 하면 GitHub Enterprise Importer를 우선 사용합니다. 먼저 시험 organization으로 trial migration을 수행하고 결과를 승인한 뒤 본 이전을 진행합니다.

- destination ruleset이 migration push를 막으면 `Repository migrations` 전용 bypass를 사용합니다.
- 이전된 저장소는 기본적으로 private인지 확인합니다.
- migration 직후 Actions가 다시 활성화될 수 있으므로 실행 상태를 즉시 확인하고, 이 문서의 호환성 검증 전까지 다시 비활성화합니다.
- Git LFS object는 별도로 전송합니다.

### 새 GHES 저장소로 Git 이력만 이전

Git 이력과 tag만 필요하면 mirror clone을 사용할 수 있습니다. 이 방식은 Issue, PR, Release, Actions 이력을 옮기지 않습니다.

```bash
git clone --mirror SOURCE_GIT_URL ard-ossie-provider.git
cd ard-ossie-provider.git
git push --mirror DESTINATION_GIT_URL
```

LFS object는 일반 Git mirror와 별개입니다. 안전한 작업 clone에서 source object를 전부 받은 후 destination에 올립니다.

```bash
git clone SOURCE_GIT_URL ard-ossie-provider-lfs
cd ard-ossie-provider-lfs
git lfs fetch --all SOURCE_REMOTE
git remote add enterprise DESTINATION_GIT_URL
git lfs push --all enterprise
```

`SOURCE_REMOTE`, URL, 목적 저장소를 실행 전에 명시적으로 확인합니다. mirror push는 목적지 ref를 source와 같게 만드는 작업이므로 빈 신규 저장소에만 적용합니다.

### 데이터 무결성 확인

source와 destination에서 다음을 대조합니다.

```bash
git ls-remote --heads SOURCE_GIT_URL
git ls-remote --tags SOURCE_GIT_URL
git ls-remote --heads DESTINATION_GIT_URL
git ls-remote --tags DESTINATION_GIT_URL
git lfs fsck
```

추가로 default branch의 commit SHA, `products/**/sources/**` LFS pointer, annotated product/table tag, Release bundle SHA-256을 표본 검증합니다. 기존 GitHub.com URL이 들어간 역사적 문서는 provenance이므로 일괄 치환하지 않습니다.

## 4. CLI와 API host 설정

### Enterprise Cloud on `github.com`

```bash
gh auth login --hostname github.com
gh auth status --hostname github.com
```

### Enterprise Cloud with data residency 또는 GHES

```bash
export GH_HOST='github.example.com'
gh auth login --hostname "$GH_HOST"
gh auth setup-git --hostname "$GH_HOST"
gh auth status --hostname "$GH_HOST"
```

headless GHES 자동화에는 `GH_ENTERPRISE_TOKEN`을 사용합니다. 현재 프로젝트의 repository bootstrap adapter는 `GH_HOST`를 자식 `gh` 명령에 전달하지만 정확한 public `main` 저장소만 허용하므로 private Enterprise 검증에 사용하지 않습니다. 대상 GHES의 Environment·branch protection·Actions permission API는 8장의 mutation 없는 `gh api` read로 검증합니다. GHES REST endpoint는 `https://HOSTNAME/api/v3`이며 `gh api`가 host에 맞게 해석하도록 전체 `api.github.com` URL을 코드나 스크립트에 넣지 않습니다.

## 5. GHES Actions 기반 준비

GHES site administrator가 다음을 먼저 완료합니다.

1. Actions에 필요한 CPU·메모리 용량을 산정합니다.
2. 지원되는 Azure Blob, Amazon S3, Google Cloud Storage 또는 S3-compatible MinIO 중 하나의 외부 blob storage를 연결합니다.
3. runner가 GHES, LFS, package index, LLM provider와 허용된 Action source에 HTTPS로 접근할 수 있게 합니다.
4. private repository 전용 runner group을 만들고 ARD 저장소에만 사용 권한을 줍니다.
5. ephemeral runner 또는 job마다 초기화되는 runner를 우선합니다. 장기 실행 runner라면 workspace, process, credential 잔존을 별도로 제거합니다.

GHES 3.18의 self-hosted runner 최소 버전은 `2.324.0`입니다. 이 저장소의 3.18.12 호환 기준은 그 최소 버전에서도 실행할 수 있는 **Node 20 Action 계열**입니다. 더 최신 runner를 설치하더라도 현재 Node 24 Action과 GHES 3.18 server 조합을 검증 없이 운영 기준으로 삼지 않습니다.

현재 workflow의 모든 job은 `runs-on: ubuntu-24.04`입니다. GHES에서는 이 라벨이 GitHub-hosted VM을 만들지 않습니다. 호환성 branch에서 조직 runner label로 바꿉니다.

```yaml
runs-on: [self-hosted, linux, x64, ard-ossie]
```

runner image에는 runner `2.324.0+`, Python 3.12, Git, Git LFS, `gh`, `uv` 실행에 필요한 CA trust와 build dependency를 준비합니다. outbound가 차단된 환경에서는 Python tool cache, uv binary, Python package mirror, OCR/문서 처리 의존성을 내부에서 제공해야 합니다.

## 6. Action 공급망과 버전 호환성

이 저장소는 모든 외부 Action을 40자리 commit SHA로 고정합니다. Enterprise 정책도 `Allow select actions`와 SHA pinning을 유지합니다.

GHES는 bundled GitHub-authored Action이 시점별 snapshot일 수 있습니다. 최신 Action이 필요하면 GitHub Connect를 허용하거나 `actions-sync`로 승인된 Action을 내부에 동기화합니다. 외부 `astral-sh/setup-uv`도 별도 승인·동기화가 필요합니다. 인터넷이 없는 runner에서는 Action 코드뿐 아니라 Action이 다운로드하는 tool과 package도 mirror해야 합니다. 특히 현재 repository static gate는 checksum을 검증하며 `actionlint 1.7.7`을 `github.com/rhysd/actionlint`에서 직접 받으므로, air-gapped 환경에서는 내부 mirror URL을 주입할 수 있는 코드 변경이 필요합니다. runner image에 binary만 넣는 것으로는 checksum manifest download를 우회할 수 없습니다.

### 현재 workflow의 3.18.12 차단 항목

| 항목 | 현재 사용 | 3.18.12 호환성 PR의 조치 |
|---|---|---|
| runner | `ubuntu-24.04` | 모든 job을 `[self-hosted, linux, x64, ard-ossie]`처럼 승인된 실제 label로 변경 |
| checkout | `actions/checkout@v6.0.2` SHA, Node 24 | Node 20 기반 `v4`의 검토된 40자리 SHA로 변경 |
| Python | `actions/setup-python@v6.2.0` SHA, Node 24 | Node 20 기반 `v5`의 검토된 40자리 SHA로 변경 |
| uv | `astral-sh/setup-uv@v8.1.0` SHA, Node 24 | Node 20 기반 `v6`의 검토된 40자리 SHA로 변경하거나 runner image에 승인된 uv를 사전 설치 |
| upload artifact | `actions/upload-artifact@v7.0.1` SHA | GHES용 Node 20 variant인 `v3.2.2-node20`의 검토된 40자리 SHA로 변경 |
| download artifact | `actions/download-artifact@v4.3.0` SHA | GHES용 `v3-node20`의 검토된 40자리 SHA로 변경 |
| release result 전달 | hidden `.ard/run/**`와 `include-hidden-files` 사용 | result를 non-hidden staging 경로에 복사하고 v3 왕복 뒤 파일 존재와 SHA-256을 검증 |
| Environment job | required reviewer를 쓰지만 명시적 deployment 권한 없음 | `ard-process.yml`의 2개 job, `ard-direct-change.yml`, `ard-llm-smoke.yml`, `ard-release.yml`의 Environment job에 `deployments: write`를 최소 scope로 추가하고 보안 검토 |
| static gate | `actionlint 1.7.7` archive와 checksum을 GitHub.com에서 직접 download | 허용된 내부 mirror와 고정 checksum을 설정할 수 있게 구현하고, 차단망에서 static group을 통합 시험 |
| Issue 첨부 | initial host가 코드에서 `github.com`으로 고정 | GHES host와 실제 redirect storage를 exact allowlist로 구현·시험하기 전에는 Issue intake 비활성화 |

다음 SHA는 **2026-08-15에 upstream tag가 가리킨 commit을 조사하기 위한 migration 기록**입니다. 실제 호환성 PR에서는 각 commit의 `action.yml`, release provenance, GHES 내부 동기화 결과를 다시 검토한 뒤 이 40자리 값을 직접 pin합니다. major tag 자체를 workflow에 쓰지 않습니다.

| Action 계열 | 조사 시점 commit |
|---|---|
| `actions/checkout` v4, Node 20 | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` v5, Node 20 | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `astral-sh/setup-uv` v6, Node 20 | `d0cc045d04ccac9d8b7881df0226f9e82c39688e` |
| `actions/upload-artifact` v3.2.2-node20 | `c6a3b2bd78b3985e4b2f15397fec357f0fd808de` |
| `actions/download-artifact` v3-node20 | `246d7188e736d3686f6d19628d253ede9697bd55` |

artifact Action은 upload와 download를 같은 backend 세대로 맞춰야 합니다. 단순 버전 치환만 하지 말고 다음 계약을 통합 테스트합니다.

- `ard-process.yml`의 semantic diagnostics가 실패 시에도 업로드되는가
- `ard-release.yml`의 `dist`와 release result가 같은 run의 linkage job으로 전달되는가
- hidden result 누락, 권한 손실, 중복 artifact 이름이 없는가
- retention과 artifact quota가 운영 요구를 만족하는가

## 7. Issue 첨부 신뢰 경계

현재 `src/ard_ossie/github_event.py`는 최초 첨부 URL host를 정확히 `github.com`으로 제한하고 redirect도 GitHub.com의 지정 storage host만 허용합니다. 이는 SSRF 방어를 위한 의도된 제한이며 Enterprise host를 wildcard로 완화하면 안 됩니다.

Enterprise Issue intake를 사용하려면 별도 코드 변경과 보안 검토가 필요합니다.

1. 신뢰할 initial host를 `GITHUB_SERVER_URL` 또는 review된 설정에 exact hostname으로 결합합니다.
2. 실제 Enterprise가 발급하는 attachment path 형식을 fixture로 캡처합니다.
3. redirect의 각 hop을 다시 검증하고, 승인된 exact storage host만 허용합니다.
4. HTTPS, credential 금지, port, query/fragment, path 정규화, filename 제한을 유지합니다.
5. GHES와 GHE.com fixture로 성공·거부 테스트를 모두 추가합니다.

이 변경이 배포되기 전에는 Enterprise Issue Form을 열지 말고, 신뢰된 same-repository branch의 `products/<product-key>/sources/**`와 Git LFS 경로만 사용합니다.

## 8. 저장소 운영 리소스 재생성

이전 도구가 다음 값을 옮겼다고 가정하지 말고 [GitHub Actions 운영 설정](github-actions-setup.md)에 따라 다시 만듭니다.

1. `ard:submission`, `ard:approved`, `ard:processing`, `ard:failed`, `ard:pr-created` label
2. `ard-llm` Environment와 required reviewer, `main` deployment branch
3. `production-linkage` Environment와 required reviewer, `main` deployment branch
4. `ARD_LLM_PROFILE`, provider endpoint/project, `ARD_SEMANTIC_PDF_PIPELINE` Variable
5. 선택한 provider의 Environment Secret
6. `main` PR 요구, 최신 base, conversation resolution, force-push/delete 금지
7. required status `ard/quality-gate`, `ard/changeset`
8. Actions default read permission과 workflow별 최소 권한

`ard github bootstrap`은 현재 GitHub.com의 정확한 public `main` 저장소만 허용하고 그
저장소의 branch protection을 관리합니다. 이는 private Enterprise host와 API 호환성을
의미하지 않으므로 현재 bootstrap CLI를 private Enterprise 저장소의 readiness probe로
사용하거나 검증 없이 apply하지 않습니다. 대상 제품의 private visibility, Environment,
attachment host·path·credential, reusable-workflow Secret, branch protection 또는 ruleset,
Actions, runner, API, LFS와 Release 동작은 mutation 없는 read와 격리된 fixture로 먼저
검증합니다. 이후 승인된 UI/API로 desired state를 재생성하고 다시 read-back합니다.
`ard-private-intake`, 해당 `main` branch policy, `ARD_ATTACHMENT_TOKEN`은 bootstrap 소유가
아니며 대상 Enterprise에서 승인된 UI/API와 숨김 Secret 입력으로 별도 생성·검증합니다.

GHES 3.18.12에서는 위 desired state를 UI 또는 승인된 REST API로 만들고 각각 다시 읽어 실제 상태를 대조합니다. 먼저 mutation 없는 read로 host, private visibility, default branch, Actions permission, Environment, branch protection endpoint의 가용성을 확인합니다.

```bash
export GH_HOST='github.example.com'
gh api --hostname "$GH_HOST" \
  repos/ENTERPRISE_ORG/ard-ossie-provider
gh api --hostname "$GH_HOST" \
  repos/ENTERPRISE_ORG/ard-ossie-provider/actions/permissions
gh api --hostname "$GH_HOST" \
  repos/ENTERPRISE_ORG/ard-ossie-provider/environments
gh api --hostname "$GH_HOST" \
  repos/ENTERPRISE_ORG/ard-ossie-provider/branches/main/protection
```

endpoint 또는 protection field가 3.18에 없으면 강행하지 말고, 지원되는 UI/API로 동일 desired state를 만든 뒤 차이와 승인자를 기록합니다. bootstrap이 해당 GHES host와 API를 명시적으로 지원하도록 수정·검증된 이후에만 `--dry-run`과 apply 경로를 다시 도입합니다. Secret 값은 shell history, migration archive, Issue, log에 남기지 않습니다.

GHES 3.18에서는 required reviewer 또는 deployment protection rule이 있는 Environment job이 성공하려면 `GITHUB_TOKEN`에 명시적 deployment write/admin 권한이 필요합니다. 이 저장소에서는 넓은 repository 기본 권한을 열지 않고 해당 job의 `permissions`에 `deployments: write`만 추가한 뒤, 기존 `contents`, `pull-requests`, `statuses`, `issues` 권한과 함께 최소 권한을 재검토합니다.

## 9. Actions 단계별 적용

한 번에 모든 workflow를 활성화하지 않습니다.

1. **호환성 branch:** private visibility와 attachment 인증 정책, runner label, Node 20 Action SHA, artifact 왕복, Environment job의 deployment 권한을 수정하고 `actionlint`와 unit/integration test를 실행합니다.
2. **읽기 전용 gate:** `ARD repository change gate`만 허용해 clean checkout에서 전체 test, Ruff, actionlint가 통과하는지 확인합니다.
3. **LLM smoke:** `ard-llm` Environment 승인을 거쳐 실제 provider text/structured 요청을 수행합니다. raw 응답이나 Secret이 artifact/log에 없는지 확인합니다.
4. **direct branch 처리:** 테스트 제품 하나를 Git LFS로 올려 candidate 변환, Draft PR, 두 required status를 확인합니다.
5. **Issue intake:** Enterprise attachment host 지원을 구현한 경우에만 관리자 승인, 첨부 download, 같은 PR 재처리를 검증합니다.
6. **numeric release:** 격리된 테스트 제품을 병합해 exact commit의 annotated tag, GitHub Release, bundle hash를 확인합니다.
7. **linkage:** `production-linkage` 승인 뒤 dispatch payload와 downstream idempotency key를 확인합니다.

각 단계가 실패하면 다음 단계 권한을 열지 않습니다. 이전 GitHub.com 저장소의 production workflow는 destination 검증이 끝날 때까지 유지하되 source 변경 동결을 지켜 이중 게시를 막습니다.

## 10. 운영 전 승인 체크리스트

- [ ] destination default branch SHA와 source 이전 기준 SHA가 일치한다.
- [ ] 모든 LFS object가 destination에서 checkout되고 `git lfs fsck`가 통과한다.
- [ ] Enterprise 제품과 정확한 버전, runner version, Action 공급 방식을 기록했다.
- [ ] 3.18.12 appliance를 3.18.13으로 hotpatch하고 보안·알려진 문제를 재확인했다.
- [ ] private destination의 설정은 UI/API read-back으로 확인했고 현재 public-only bootstrap CLI를 실행하지 않았다.
- [ ] Actions, artifact/cache storage, runner group과 outbound allowlist가 준비됐다.
- [ ] air-gapped 환경이면 actionlint, uv, Python package, OCR/문서 처리 dependency의 내부 mirror 경로가 실제 gate에서 검증됐다.
- [ ] 모든 workflow의 runner label이 실제 online runner와 일치한다.
- [ ] upload/download artifact가 같은 GHES 지원 세대이며 release result 왕복이 통과한다.
- [ ] `ard-llm`과 `production-linkage` Secret은 Environment 승인 전 노출되지 않는다.
- [ ] required status가 exact PR head에 게시되고 fork/untrusted code에 write token이 가지 않는다.
- [ ] Issue intake를 켰다면 Enterprise 첨부/redirect allowlist의 양·음성 테스트가 통과한다.
- [ ] direct branch의 PDF/DOCX/XLSX 변환은 hard error `0`이다. candidate PDF는 추가로 validation `status=verified`, `publishable=true`이며, `WARN`이라면 validation은 여전히 `verified`이고 review debt가 없다.
- [ ] merge 뒤 immutable tag, Release, bundle SHA와 downstream dispatch가 일치한다.
- [ ] source 저장소는 read-only 또는 archive 처리되고 게시 owner가 destination 하나로 수렴한다.

## 11. GHES 3.18.12 기준 유의사항

| 점검 항목 | 2026-08-15 판단 | 운영 gate |
|---|---|---|
| server patch | 3.18.12는 2026-07-16 공개됐지만 3.18.13이 2026-08-05 공개됨 | 3.18.13 hotpatch 전 production cutover 금지 |
| 보안 | 3.18.13이 user storage 삭제가 가능한 HIGH 취약점과 support bundle secret redaction 문제를 수정 | LFS, Release, 첨부를 다루기 전에 patch와 backup/restore 시험 완료 |
| 지원 종료 | 3.18 계열은 2026-10-14 지원 종료 | 3.20 또는 3.21 upgrade 일정과 rollback window를 cutover 전에 승인 |
| runner | 3.18의 최소 self-hosted runner는 `2.324.0` | 모든 runner가 최소 버전 이상이고 자동 update/ephemeral 정책이 의도대로인지 확인 |
| Action runtime | 현재 checkout/setup-python/setup-uv는 Node 24 계열 | 3.18.12 호환 branch는 Node 20 계열과 exact SHA를 사용 |
| artifact | upload v4+와 download v4+는 GHES 미지원 | `v3.2.2-node20`/`v3-node20` 조합의 같은-run 왕복 시험 필수 |
| deployment | 3.18부터 protection rule/required reviewer workflow에 명시적 token 권한 필요 | Environment job에 `deployments: write`를 job scope로 부여하고 승인 전 Secret 비노출 검증 |
| Issue 첨부 | 현재 host 검사가 GitHub.com 전용 | exact GHES host 구현 전 Issue intake 비활성화 |

3.18.12에서 3.18.13으로 올리기 전에 backup과 restore point를 확인하고 maintenance window를 확보합니다. 3.18.13의 알려진 문제에 따라 custom firewall rule과 custom NTP 설정을 별도로 기록해 upgrade 후 재적용·검증합니다. hotpatch 중 장기 연결이 있는 경우 frontend 재시작이 최대 수 분 지연될 수 있으므로 Git push와 workflow 시작을 차단한 뒤 진행합니다.

GHES는 최소 runner만 맞춘다고 Action이 모두 동작하는 것이 아닙니다. pinned commit이 instance에 존재하는지, `action.yml`의 runtime이 runner에서 실행되는지, REST endpoint/API version이 3.18에 포함되는지를 함께 확인합니다. feature upgrade는 GitHub Upgrade Assistant로 지원 경로를 확인하고, 지원 종료 직전까지 미루지 않습니다.

버전 업그레이드 전후에 다음 smoke matrix를 다시 실행합니다.

- private repository, Actions permission, Environment, branch protection의 `gh api` read-back
- checkout + LFS pull
- artifact upload/download round trip
- Python 3.12 + uv locked install
- LLM text/structured smoke
- direct branch candidate conversion
- Issue attachment intake(활성화한 경우)
- immutable release와 dispatch 재시도

## 공식 참고자료

- [GitHub Enterprise Importer의 이전 범위와 제외 항목](https://docs.github.com/en/migrations/using-github-enterprise-importer/migrating-between-github-products/about-migrations-between-github-products)
- [GitHub 제품 간 이전 전체 절차](https://docs.github.com/en/enterprise-cloud@latest/migrations/using-github-enterprise-importer/migrating-between-github-products/overview-of-a-migration-between-github-products)
- [GHES 3.18.12·3.18.13 release note와 알려진 문제](https://docs.github.com/en/enterprise-server@3.18/admin/release-notes)
- [GHES 3.18 지원 기간](https://docs.github.com/en/enterprise-server@3.18/admin/all-releases)
- [GHES 3.18 Actions 최초 구성과 외부 storage 요구사항](https://docs.github.com/en/enterprise-server@3.18/admin/managing-github-actions-for-your-enterprise/getting-started-with-github-actions-for-your-enterprise/getting-started-with-github-actions-for-github-enterprise-server)
- [GHES 3.18 self-hosted runner 추가](https://docs.github.com/en/enterprise-server@3.18/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
- [GitHub.com Action 접근과 GitHub Connect](https://docs.github.com/en/enterprise-server@3.18/admin/managing-github-actions-for-your-enterprise/managing-access-to-actions-from-githubcom/enabling-automatic-access-to-githubcom-actions-using-github-connect)
- [upload-artifact의 GHES 지원 범위](https://github.com/actions/upload-artifact#ghes-support)
- [download-artifact의 GHES 지원 범위](https://github.com/actions/download-artifact#ghes-support)
- [checkout Action과 Node runtime](https://github.com/actions/checkout)
- [setup-python Action과 Node runtime](https://github.com/actions/setup-python)
- [setup-uv Action](https://github.com/astral-sh/setup-uv)
- [GitHub CLI Enterprise host 환경 변수](https://cli.github.com/manual/gh_help_environment)
