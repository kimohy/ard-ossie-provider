# ARD Ossie 다음 작업 로드맵

이 문서는 ARD Ossie의 운영 전환 기록과 이후 acceptance 작업 순서를 정리합니다. 완료된 P0는 감사 기록으로 유지하고, P1부터는 의존성 순서대로 실행하며 각 단계의 증거를 남긴 뒤 다음 단계로 이동합니다.

## 현재 기준점

| 항목 | 상태 |
|---|---|
| 최초 구현 PR | [PR #1](https://github.com/kimohy/ard-ossie-provider/pull/1), 2026-08-11 병합, merge commit `d6603bd941523eff3de145361368e28df74347d3` |
| bootstrap 검증 | PR #1의 검토 대상 head `c6812b3dddcd2c79556514cecbee153732d41f34`에서 run #4의 `static`, `pytest`, `wheel`, aggregate 성공 |
| 운영 설정 | labels, 두 Environments, LLM Secret/Variables, Actions 권한과 `main` 보호 규칙 적용·read-back 검증 완료 |
| 영구 gate 전환 | [정리 PR #2](https://github.com/kimohy/ard-ossie-provider/pull/2)에서 일회성 workflow를 제거하고 같은 head의 `ard/quality-gate`, `ard/changeset` 성공 후 병합 |
| 다음 작업 | P1 Issue 기반 합성 데이터 acceptance |

`ard-initial-bootstrap.yml`은 영구 required status가 아직 없던 PR #1만 검증하기 위한 일회성 workflow였습니다. PR #1 병합 뒤 운영 설정과 정상 gate를 검증한 [정리 PR #2](https://github.com/kimohy/ard-ossie-provider/pull/2)에서 제거했으므로, 현재 운영자가 이 workflow를 다시 실행하거나 복원해서는 안 됩니다. 이후 모든 검증은 기본 브랜치의 영구 `ARD repository change gate`를 사용합니다.

## 작업 순서 요약

| 우선순위 | 상태 | 작업 | 선행조건 | 완료 증거 |
|---|---|---|---|---|
| P0 | 완료 | PR #1 최종 검토와 최초 병합 | bootstrap run 성공 | merge commit `d6603bd941523eff3de145361368e28df74347d3` |
| P0 | 완료 | 저장소 운영 설정 bootstrap | PR #1 병합 | desired state read-back 결과와 GitHub 설정 화면 |
| P0 | 완료 | 일회성 bootstrap workflow 제거 | 영구 branch protection 적용 | [정리 PR #2](https://github.com/kimohy/ard-ossie-provider/pull/2)의 두 required status 성공 |
| P1 | 대기 | Issue 기반 신규 제품 E2E | LLM Environment 설정 | 생성 PR, 품질 보고서, 두 status 성공 |
| P1 | 대기 | 병합·릴리스·후속 연계 E2E | 신규 제품 PR 승인 | tag, GitHub Release, artifact hash, dispatch 기록 |
| P1 | 대기 | 직접 브랜치 업데이트 E2E | 최초 제품 `v1` 릴리스 | 정확한 `v2`, diff/history 및 새 release |
| P2 | 대기 | shared-table changeset E2E | 서로 같은 테이블을 쓰는 제품 2개 | readiness, 병합 순서, 제품·테이블 버전 증거 |
| P2 | 대기 | review 보호와 운영 runbook | 비소유자 writer 확보 | 1인 승인 보호와 재시도/복구 리허설 기록 |
| P3 | 대기 | 후속 기능·유지보수 backlog | 운영 E2E 안정화 | 각 기능별 별도 spec/PR |

## P0. 최초 운영 전환 완료 기록

P0는 2026-08-11에 연속 실행했습니다. PR #1의 정확한 head에서 bootstrap 네 job을 확인한 뒤 병합했고, 이어서 다음 desired state를 적용하고 GitHub UI/API read-back으로 검증했습니다.

- `ard:submission`, `ard:approved`, `ard:processing`, `ard:failed`, `ard:pr-created` labels가 있습니다.
- `ard-llm`과 `production-linkage`는 repository owner 승인을 요구하고 `main`만 deployment branch로 허용합니다.
- `ard-llm`에는 `ARD_LLM_PROFILE`, provider endpoint/project, `ARD_MAX_ATTACHMENT_BYTES` Variables와 선택한 provider의 Environment Secret이 있습니다. 모델과 API 방식은 저장소 프로필에 있으며 Secret 값은 읽거나 교체하거나 기록하지 않습니다.
- Actions 기본 권한은 read이며 workflow의 pull request 생성을 허용합니다.
- `main`은 pull request, 최신 base, conversation resolution, `ard/quality-gate`, `ard/changeset`을 요구합니다. 관리자 우회, force push와 삭제는 허용하지 않으며, 비소유자 writer가 없으므로 required approvals는 0입니다.
- 정리 PR #2에서 일회성 workflow와 해당 테스트 계약을 제거하고 운영 문서를 갱신했습니다. 영구 gate의 `static`, `pytest`, `wheel`과 aggregate가 성공하고 trusted finalizer가 같은 PR head에 두 required status를 게시한 뒤 병합했습니다.

자세한 repository 설정과 재검증 절차는 [GitHub Actions 설정](github-actions-setup.md)을 따릅니다. required review 수는 비소유자 writer가 준비된 뒤 `ard github enable-review-protection`으로만 활성화합니다.

## P1. 핵심 운영 경로 acceptance

실제 고객 데이터 대신 공개 가능한 최소 합성 fixture를 사용합니다. 검증 결과는 `docs/acceptance/initial-production-verification.md`에 run URL, Issue/PR/commit/tag 링크, artifact SHA-256과 관찰 결과를 기록합니다. Secret 값과 원문 민감 데이터는 기록하지 않습니다.

### 1. 승인된 Issue에서 신규 제품 `v1` 생성

- [ ] 제품 HTML, 시멘틱 DOCX 또는 PDF, 데이터 딕셔너리 XLSX를 GitHub Issue에 직접 첨부합니다.
- [ ] 승인 전에는 LLM job, writeback, Secret 접근이 발생하지 않는지 확인합니다.
- [ ] write 이상 권한의 관리자가 `ard:approved`를 적용합니다.
- [ ] `ard/issue-<number>-<product-key>` 브랜치와 하나의 Draft PR이 생성되는지 확인합니다.
- [ ] HTML은 일반 Git 파일로, DOCX/PDF와 XLSX는 Git LFS 객체로 저장되는지 확인합니다.
- [ ] 다섯 generated 산출물과 다섯 quality 보고서가 같은 PR에 커밋되는지 확인합니다.
- [ ] 제품·테이블·컬럼·metric·relationship ID와 `v1`이 Registry/생성물 사이에서 일치하는지 확인합니다.
- [ ] hard error가 0이고 `ard/quality-gate`, `ard/changeset`이 정확한 head에 success인지 확인합니다.

### 2. 최초 릴리스와 후속 연계

- [ ] 신규 제품 PR을 병합하고 `ARD numeric release` run을 확인합니다.
- [ ] `product/<product-id>/v1`과 필요한 `table/<table-id>/v1` tag가 불변 SHA를 가리키는지 확인합니다.
- [ ] GitHub Release asset과 manifest의 SHA-256을 다시 계산해 일치 여부를 확인합니다.
- [ ] `production-linkage` 승인 전에는 dispatch가 발생하지 않고 승인 후에만 `ard_product_released`가 발생하는지 확인합니다.
- [ ] 동일 입력 재시도 시 immutable tag를 이동하지 않고 결과가 no-op 또는 수렴 상태가 되는지 확인합니다.

### 3. 직접 브랜치 업데이트로 `v2` 생성

- [ ] 기존 제품의 `product_id`, `operation: update`, 현재 `base_version`, 정확히 `+1`인 버전을 사용합니다.
- [ ] 하나의 제품 source만 변경한 same-repository branch에서 read-only signal이 실행되고, 기본 브랜치의 `workflow_run` coordinator가 자동 Draft PR을 생성/재사용하는지 확인합니다.
- [ ] coordinator와 processor가 `trusted/`의 기본 브랜치 CLI만 실행하고 exact candidate는 `--repository candidate/` 데이터로만 읽는지 확인합니다.
- [ ] 내용이 바뀌지 않은 테이블은 버전을 유지하고 실제 변경된 제품/테이블만 `v2`가 되는지 확인합니다.
- [ ] `ard history`, `ard diff`, Registry current version, release tag와 Git history가 같은 변화를 설명하는지 확인합니다.
- [ ] stale base, 건너뛴 버전과 fork writeback이 fail-closed인지 별도 거부 테스트로 확인합니다.

P1 완료 기준:

- Issue 생성, 직접 업데이트, 병합, release, protected dispatch 전 경로의 성공·거부 증거가 남아 있고 운영자가 결과 envelope의 exit `0`, `10`, `30`, `40`, `50`, `70`을 구분할 수 있습니다.

## P2. 다중 제품과 운영 통제

### 1. shared-table changeset

- [ ] 동일한 Registry table ID를 참조하는 합성 제품 2개를 준비합니다.
- [ ] `ARD shared-table changeset coordinator`의 `create` 모드로 중앙 Registry PR과 제품별 Draft 추적 PR을 만듭니다.
- [ ] 중앙 초기 PR을 먼저 병합하고, 제품별 준비가 끝나기 전 `ard/changeset`이 pending인지 확인합니다.
- [ ] readiness PR에 제품 버전, PR 번호, exact head가 누적되고 모든 제품 준비 후에만 success가 되는지 확인합니다.
- [ ] readiness PR → 제품 PR 순으로 병합하고, 릴리스가 같은 changeset의 모든 제품으로 확장되는지 확인합니다.
- [ ] 이후 독립 변경에서 활성 `changeset_id`가 `null`로 전환되고 과거 감사 JSON/marker가 유지되는지 확인합니다.

### 2. 운영 runbook과 보호 강화

- [ ] 비소유자 writer를 준비한 뒤 1인 승인 보호를 활성화하고 direct push가 거부되는지 확인합니다.
- [ ] exit `30` 재시도, exit `70` partial mutation 수렴, revert PR 복구 절차를 리허설합니다.
- [ ] downstream consumer가 `(product_id, version, tag, commit)`으로 중복 dispatch를 제거하는 계약을 테스트합니다.
- [ ] Git LFS 누락, symlink, 혼합 code/data PR, secret pattern, 잘못된 release asset을 각각 fail-closed 사례로 기록합니다.

P2 완료 기준:

- 공유 테이블의 원자적 버전 전환, 승인 통제, 일시 장애 재시도와 부분 실패 복구가 실제 GitHub 기록으로 재현됩니다.

## P3. 운영 안정화 이후 backlog

각 항목은 운영 E2E가 안정화된 뒤 별도 design/spec과 PR로 진행합니다.

1. 제품 retire를 tombstone으로 전환하고 retired ID 재활성화를 차단하는 lifecycle.
2. provider/model 변경에 대한 고정 fixture 기반 품질 회귀와 비용·latency 기록.
3. action SHA, `uv.lock`, Python/Docling/Ossie 버전 갱신 절차와 공급망 검증 자동화.
4. 실제 대용량 DOCX/PDF/XLSX 경계값과 Git LFS 장애를 포함한 주기적 acceptance 실행.
5. release/dispatch 감사 로그를 이용한 운영 지표: 처리 성공률, 승인 대기시간, 재시도율, hard-error 유형, 릴리스 lead time.

## 완료 정의

ARD Ossie의 최초 운영 전환은 다음 조건을 모두 만족할 때 완료로 판단합니다.

- [x] PR #1이 병합되고 일회성 bootstrap workflow가 후속 PR에서 제거되었습니다.
- [x] repository bootstrap desired state가 read-back 검증에 수렴하며 영구 required status 두 개가 정상 gate에서 게시됩니다.
- [ ] Issue 기반 `v1`, 직접 업데이트 `v2`, shared-table changeset 경로가 실제 GitHub Actions에서 검증되었습니다.
- [ ] 태그, Release asset, manifest hash와 Registry 버전이 일치합니다.
- [ ] Secret 비노출, fork/권한/경로/LFS/혼합 PR fail-closed 증거가 남아 있습니다.
- [ ] 운영자가 transient/partial failure를 강제 변경 없이 재시도하거나 revert PR로 복구할 수 있습니다.
