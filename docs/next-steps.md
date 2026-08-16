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
- [ ] shared-table changeset E2E
- [ ] 1인 review protection과 운영 장애 훈련
- [ ] representative PDF 안정화 표본과 운영 지표
