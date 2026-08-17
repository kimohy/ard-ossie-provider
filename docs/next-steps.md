# ARD Ossie 완료 기록과 다음 작업

이 문서는 이미 검증된 운영 이력과 아직 끝나지 않은 작업을 분리합니다. 현재 동작은 [정책과 거버넌스](policy-and-governance.md), [시멘틱 PDF 파이프라인](semantic-pdf-pipeline.md), [GitHub Actions 운영 설정](github-actions-setup.md)을 기준으로 판단합니다.

## 현재 기준점

| 영역 | 상태 | 증거 |
|---|---|---|
| 최초 repository bootstrap | 완료 | [PR #1](https://github.com/kimohy/ard-ossie-provider/pull/1), merge `d6603bd9` |
| 영구 repository gate 전환 | 완료 | [PR #2](https://github.com/kimohy/ard-ossie-provider/pull/2) |
| 범용 semantic PDF candidate pipeline | 완료 | [PR #29](https://github.com/kimohy/ard-ossie-provider/pull/29) 이후 candidate/shadow/legacy 경로 |
| 저신뢰 LLM 복구와 안전한 review continuation | 완료 | bounded recovery, safe fallback, audit·privacy 회귀 테스트 |
| 한글·table-cell 공백 복구 | 완료 | 생성+독립 검증, protected token와 table-cell invariant |
| heading hierarchy와 Markdown 검증 | 완료 | hierarchy-derived 12-heading Issue #3 fixture, visible escape/raw HTML 검사 |
| 선택적 unsafe metric 격리 | 완료 | [PR #37](https://github.com/kimohy/ard-ossie-provider/pull/37) |
| spacing verification `VALID` 호환 | 완료 | [PR #38](https://github.com/kimohy/ard-ossie-provider/pull/38) |
| Issue #3 제품 v1 | 완료 | [Issue #3](https://github.com/kimohy/ard-ossie-provider/issues/3), [제품 PR #5](https://github.com/kimohy/ard-ossie-provider/pull/5), merge `673b8311` |
| immutable release와 dispatch | 완료 | [Marketing Insight v1](https://github.com/kimohy/ard-ossie-provider/releases/tag/product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v1), linkage success |
| fresh-runner annotated tag identity | 완료 | [PR #39](https://github.com/kimohy/ard-ossie-provider/pull/39), merge `28f943d` |
| 직접 브랜치 제품 v2 | 완료 | [검증 기록](acceptance/direct-branch-v2-verification.md), [제품 PR #43](https://github.com/kimohy/ard-ossie-provider/pull/43), merge `ba83203` |
| Issue #46 공개 intake·same-source replay·v1 release | 완료 | [검증 기록](acceptance/issue-46-same-source-replay-release.md), [fix PR #51](https://github.com/kimohy/ard-ossie-provider/pull/51), [재처리 run](https://github.com/kimohy/ard-ossie-provider/actions/runs/31991352055), [제품 PR #49](https://github.com/kimohy/ard-ossie-provider/pull/49), [Release](https://github.com/kimohy/ard-ossie-provider/releases/tag/product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v1) |

## Issue #3 acceptance 결과

Issue #3은 조직 정책을 준수하는 합성 Marketing Insight 제품으로 최초 공개 Issue 기반 PDF 경로를 검증했습니다.

- source PDF 5페이지와 XLSX를 Git LFS 객체로 검증
- source text coverage `1.0`
- unmatched, duplicated, degraded block 모두 `0`
- heading 12개, GFM table 10개
- decision 65개 모두 `selected`, unresolved `0`
- 기존 후보 적용 64개, 생성·독립 검증된 spacing repair 1개
- visible escape와 raw HTML `0`
- `validation-report.json`은 `verified`, `publishable=true`
- unsafe optional metric 제안 2개는 제외하고 `WARN`으로 기록
- 제품 tag 1개와 table tag 4개가 release commit을 가리킴
- GitHub Release bundle 게시, downstream dispatch success
- 동일 release 재실행은 asset `noop`, 동일 dispatch는 status `noop`

Issue는 제품 PR의 `Closes #3`가 병합된 뒤 정상적으로 닫혔습니다. 처리 실패나 Draft PR 생성 시점에 닫는 것은 완료 조건이 아닙니다.

## Issue #46 공개 intake, same-source replay, v1 release 결과

현재 GitHub.com 저장소를 공개 상태로 복원하면서, 향후 private GitHub Enterprise 이전에 재검증할 격리된 attachment 인증 경로를 실제 합성 Issue로 확인했습니다.

- [정책 PR #48](https://github.com/kimohy/ard-ossie-provider/pull/48)은 exact head `dfaaedf562ce8d680f4bb15a58abb22c7d6ab4db`에서 필수 체크 7개를 통과한 뒤 merge commit `e285a6aa83e0c16b9ed50b02a54a3989667dbcca`로 병합했습니다.
- 저장소 read-back은 `public`, unarchived, default branch `main`, 관리자 권한을 확인했습니다.
- `ARD_ATTACHMENT_TOKEN`은 `ard-private-intake` Environment에만 존재하며 metadata update timestamp는 `2026-08-16T22:32:49Z`입니다. Secret 값과 signed attachment URL은 기록하지 않았습니다.
- `ard-private-intake`, `ard-llm`, `production-linkage` Environment는 각각 정확한 `main` branch policy 하나만 가집니다. `ard-private-intake`에는 reviewer가 없고, `ard-llm`과 `production-linkage`에는 `kimohy` required reviewer가 있습니다.
- `main` protection은 strict `ard/changeset`·`ard/quality-gate`, admin enforcement, PR 필수, conversation resolution 필수이며 force push와 deletion은 비활성화되어 있습니다. 현재 required approving review count는 `0`입니다.
- Issue #46은 공개 게시 동의가 체크된 합성 Campaign Governance Monitor 입력이며 실제 고객, 계정, 플랫폼, 조직 또는 운영 수치를 포함하지 않습니다.
- 최초 [attachment-auth run](https://github.com/kimohy/ard-ossie-provider/actions/runs/31977762165)은 authorization부터 finalization까지 통과했지만, 이후 동일 PDF의 한글 단어 경계가 `정의 서이며`로 달라지는 source-fidelity 결함을 발견했습니다. 이 실행은 인증 경로 증거로만 보존하고 최종 제품 acceptance로 사용하지 않습니다.
- [same-source replay fix PR #51](https://github.com/kimohy/ard-ossie-provider/pull/51)은 exact head `33dbe6cb1062d43d77523cef316f5b90eaabb742`에서 repository gate, model/schema, pytest, wheel, finalizer와 두 required status를 통과한 뒤 `51989c1e67b2f024e3cab6cfc1d7c61cff1e2018`로 병합됐습니다.
- `ard:approved`를 재적용한 최종 [재처리 run](https://github.com/kimohy/ard-ossie-provider/actions/runs/31991352055)은 fix merge SHA에서 base sync, protected validation, processing, finalization을 모두 통과했습니다.
- [제품 PR #49](https://github.com/kimohy/ard-ossie-provider/pull/49)의 refreshed exact head는 `721a143a2ffc0183bab418dd9448543b82e912b8`입니다. `ard/changeset`과 `ard/quality-gate`는 이 SHA에 대해 success이며, 생성 문서는 `정의서이며`를 포함하고 `정의 서이며`는 포함하지 않습니다.
- 제품 `500138301`과 `500138302`의 semantic PDF Git blob은 모두 `d2a5bff166760671d23bb167d5e8e1779c804345`, 생성된 canonical Markdown Git blob은 모두 `1a3fd5f2f0d85d41ada7ed75c0e4d4acbb143deb`입니다. 최종 validation의 model call은 `0`회입니다.
- PR #49는 `282228635a36e8709ef8cb01fc0bfba4259ed01b`로 병합됐고 Issue #46도 닫혔습니다. [numeric release run](https://github.com/kimohy/ard-ossie-provider/actions/runs/31992478728)은 detect, release, downstream linkage를 모두 통과했습니다.
- 제품 ID는 `prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d`이며 tag `product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v1`은 merge SHA를 가리킵니다. Actions artifact에서 추출한 release ZIP과 GitHub Release asset은 byte-identical이고 SHA-256은 `192c3d3db7999f865ecc2708773c03fd83481f23b7ef868e8e81afeaf9970387`입니다.
- 최종 품질은 `PASS`, validation은 `verified`, `publishable=true`, coverage `1.0`이며 hard error, warning, finding, missing/duplicate/degraded block은 모두 `0`입니다. 18개 bundle 파일의 개별 SHA-256도 release result와 일치하고 exact dispatch status가 success입니다.
- 전체 명령과 증적 값은 [Issue #46 검증 기록](acceptance/issue-46-same-source-replay-release.md)에 보존했습니다.
- intake가 고정한 source SHA-256은 다음과 같습니다.
  - `dictionary/dictionary.xlsx`: `10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a`
  - `product-info/product.html`: `b39248654c0cd9b6f3f28111a6c44036d86a2440a1d7dc2c9bfd7bd40281d7f9`
  - `semantic/semantic.pdf`: `ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6`

Enterprise 이전 시에는 현재 성공을 그대로 전제하지 않고 private visibility, attachment host와 credential 전달, Environment·reusable workflow Secret, branch/ruleset, Actions runner·API·LFS·Release 동작을 mutation-free read-back부터 다시 검증합니다.

## 다음 우선순위

### P1. 직접 브랜치 업데이트 `v2`

- [x] 기존 제품에 `operation: update`, 같은 `product_id`, 현재 `base_version`, 정확히 `+1`인 버전을 제출했습니다.
- [x] trusted `workflow_run` coordinator가 exact candidate data만 읽고 처리하는지 확인했습니다.
- [x] 내용이 같은 table은 버전을 유지하고 실제 변경 엔터티만 `v2`가 되는지 확인했습니다.
- [x] `ard history`, `ard diff`, Registry, tag, Release가 같은 변경을 설명하는지 확인했습니다.
- [x] stale base, skipped version, fork writeback을 fail-closed 사례로 남겼습니다.

완료: [production 검증 기록](acceptance/direct-branch-v2-verification.md), [제품 PR #43](https://github.com/kimohy/ard-ossie-provider/pull/43), [성공 coordinator](https://github.com/kimohy/ard-ossie-provider/actions/runs/31920963044), [release](https://github.com/kimohy/ard-ossie-provider/actions/runs/31921644512), [stale 거절](https://github.com/kimohy/ard-ossie-provider/actions/runs/31921955295), [gap 거절](https://github.com/kimohy/ard-ossie-provider/actions/runs/31922033108)에 성공·거부·수렴 증거가 있습니다.

### P2. Shared-table changeset E2E

- [ ] 같은 Registry table ID를 참조하는 합성 제품 두 개를 준비합니다.
- [ ] 중앙 changeset PR과 제품별 Draft tracking PR을 생성합니다.
- [ ] readiness가 모두 모이기 전 `ard/changeset`이 pending인지 확인합니다.
- [ ] readiness PR을 제품 PR보다 먼저 병합하고 릴리스 대상이 changeset 전체로 확장되는지 확인합니다.
- [ ] 다음 독립 변경에서 활성 `changeset_id`를 `null`로 전환하고 과거 감사 기록은 보존합니다.

완료 기준: 공유 table의 원자적 전환과 병합 순서가 실제 GitHub 기록으로 재현됩니다.

### P2. 운영 보호와 장애 훈련

- [ ] 비소유자 writer를 준비한 뒤 1인 review protection을 활성화합니다.
- [ ] `production-linkage` Environment에 required reviewer를 추가하고 승인 대기·거절을 리허설합니다.
- [ ] exit `30`, exit `70`, `TAG_TARGET_CONFLICT`, LFS 누락, symlink, 혼합 code/data PR 복구를 리허설합니다.
- [ ] `review_pending`의 PR 계속 처리·merge 보류·release 차단과 `review_required`의 canonical 차단·재처리를 운영자가 구분하는지 확인합니다.
- [ ] downstream consumer가 `(product_id, version, tag, commit)`으로 중복 dispatch를 제거하는지 검증합니다.
- [ ] 실패 run과 수렴한 최종 상태를 함께 보존하는 incident template을 만듭니다.

완료 기준: 강제 tag 이동이나 branch 덮어쓰기 없이 재시도·revert·수렴 절차를 수행할 수 있습니다.

### P3. 안정화 이후 backlog

1. 제품 retire를 tombstone으로 전환하고 retired ID 재활성화를 차단하는 lifecycle
2. provider/model 변경의 고정 fixture 품질 회귀와 비용·latency 기록
3. Action SHA, `uv.lock`, Python/Docling/Ossie 갱신과 공급망 검증 자동화
4. 대용량 DOCX/PDF/XLSX와 Git LFS 장애를 포함한 주기적 acceptance
5. release/dispatch 감사 로그 기반 성공률, 대기시간, 재시도율, error 유형, lead time 지표
6. 14일·서로 다른 PDF 20개 안정화 뒤 legacy pipeline 제거 여부 결정

## 완료 정의

최초 Issue 기반 `v1` 경로는 완료됐습니다. 전체 운영 전환의 다음 완료 조건은 다음과 같습니다.

- [x] repository bootstrap과 영구 required status gate
- [x] Issue 기반 `v1` 생성, 검증, 병합, immutable release, downstream dispatch
- [x] LLM 저신뢰·공백·heading·unsafe optional metric의 감사 가능한 처리
- [x] 직접 브랜치 `v2` 업데이트
- [x] 공개 Issue attachment 인증과 Enterprise 이전 경계 검증
- [x] 동일 semantic source의 canonical replay와 Issue #46 conflict-protected v1 release
- [ ] shared-table changeset E2E
- [ ] 1인 review protection과 운영 장애 훈련
- [ ] representative PDF 안정화 표본과 운영 지표
