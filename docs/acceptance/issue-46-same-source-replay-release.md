# Issue #46 Same-Source Replay and v1 Release Verification

검증일은 2026-08-17이며 대상은 합성 Campaign Governance Monitor 제품 `500138302`입니다. 이 기록은 동일 semantic PDF에서 발생한 한글 단어 경계 오염을 차단한 뒤 trusted reprocessing, 제품 병합, 저장소 정책으로 충돌을 차단하는 release, downstream dispatch까지 완료한 실제 GitHub 증거를 보존합니다.

## Scope and incident baseline

- [Issue #46](https://github.com/kimohy/ard-ossie-provider/issues/46)은 공개 게시 동의가 있는 합성 입력이며 실제 고객, 계정, 플랫폼, 조직 또는 운영 수치를 포함하지 않습니다.
- semantic source SHA-256은 `ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6`입니다. 기존 제품 `500138301`과 새 제품 `500138302`가 같은 PDF를 사용합니다.
- 최초 processing은 source text coverage `1.0`을 통과했지만 `정의서이며`를 `정의 서이며`로 바꿨습니다. 문자 coverage만으로는 의미 있는 한글 word-boundary 오염을 잡지 못한 사례입니다.
- 최초 attachment-auth 성공 run은 인증 경로 증거로만 보존합니다. 잘못된 canonical Markdown을 포함한 제품 head는 최종 acceptance나 release에 사용하지 않았습니다.

## Same-source replay fix landing

- 코드 수정: [PR #51](https://github.com/kimohy/ard-ossie-provider/pull/51)
- reviewed head: `33dbe6cb1062d43d77523cef316f5b90eaabb742`
- merge commit: `51989c1e67b2f024e3cab6cfc1d7c61cff1e2018`
- protected checks: repository change gate, model/schema verification, full pytest, wheel build, finalizer, `ard/changeset`, `ard/quality-gate` 모두 success
- replay authority는 repository default branch의 exact SHA에서 읽은 hash-verified artifact로 제한됩니다. 동일 replay identity의 canonical Markdown은 byte-for-byte 일치해야 하고, 신뢰 검증 실패는 redacted security/validation envelope로 종료됩니다.

## Trusted reprocessing and exact-head verification

- 최종 intake/base-sync/processing: [run 31991352055](https://github.com/kimohy/ard-ossie-provider/actions/runs/31991352055), head SHA `51989c1e67b2f024e3cab6cfc1d7c61cff1e2018`, conclusion `success`
- authorize, route, base sync, protected validation, processing, process finalizer, issue finalizer가 모두 success였습니다. 기존 source가 있어 intake job만 의도대로 skipped였습니다.
- refreshed product PR: [PR #49](https://github.com/kimohy/ard-ossie-provider/pull/49)
- exact product head: `721a143a2ffc0183bab418dd9448543b82e912b8`
- exact-head statuses:
  - `ard/changeset=success`, description `No shared-table changeset required`
  - `ard/quality-gate=success`, description `ARD validation passed`
  - 두 status의 target은 모두 run `31991352055`입니다.
- exact head의 `products/500138302/generated/data-semantic.md` 15행은 `정의서이며`를 포함하고 `정의 서이며`는 포함하지 않습니다.
- 두 제품의 semantic PDF Git blob은 `d2a5bff166760671d23bb167d5e8e1779c804345`로 같습니다. 두 generated `data-semantic.md` Git blob도 `1a3fd5f2f0d85d41ada7ed75c0e4d4acbb143deb`로 같습니다.
- 최종 application report의 문제 candidate set `candidate_set_2bea4390072dc548`는 trusted `candidate_3440b6770636a1f7`을 적용했고 invariant code는 없습니다.
- validation report의 `model_call_count`는 `0`입니다. 같은 source의 verified decision history를 재사용했으며 새 semantic adjudication call을 만들지 않았습니다.

## Merge, tag, Release, and dispatch

- PR #49 merge commit: `282228635a36e8709ef8cb01fc0bfba4259ed01b`
- Issue #46은 제품 PR 병합과 함께 정상적으로 닫혔습니다.
- numeric release: [run 31992478728](https://github.com/kimohy/ard-ossie-provider/actions/runs/31992478728), detect, `release (500138302)`, `linkage (500138302)` 모두 success
- product ID: `prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d`
- product tag: `product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v1`, peeled target `282228635a36e8709ef8cb01fc0bfba4259ed01b`
- [500138302 v1 GitHub Release](https://github.com/kimohy/ard-ossie-provider/releases/tag/product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v1)
- release asset: `prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d-v1.zip`, 46,002 bytes
- exact dispatch status: `ard/dispatched:prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d:v1=success`, target run `31992478728`

## Bundle and quality integrity

- workflow result의 `artifact_sha256`: `192c3d3db7999f865ecc2708773c03fd83481f23b7ef868e8e81afeaf9970387`
- Actions artifact에서 추출한 release ZIP과 GitHub Release asset을 각각 내려받아 SHA-256을 계산했습니다. 두 ZIP은 byte-identical이고 위 값과 일치합니다. 외부 Actions artifact archive 자체는 비교 대상이 아닙니다.
- ZIP 무결성 검사는 오류 없이 통과했으며 내부 파일은 18개입니다. release result가 기록한 18개 파일별 SHA-256을 실제 압축 해제 파일과 모두 대조했습니다.
- `quality-report.json`: `status=PASS`, hard error `0`, warning `0`
- `validation-report.json`: `status=verified`, `publishable=true`, finding `0`, character coverage `1.0`, missing atom `0`, duplicate atom `0`, degraded block `0`, model call `0`
- released `generated/data-semantic.md`의 SHA-256은 `97e7dae822a5e459bde624b78dde85f4aa8b651913211d4bfa138db7307c852b`입니다.

## Commands used for final read-back

```text
gh pr view 51 --json state,headRefOid,mergeCommit,statusCheckRollup,url
gh run view 31991352055 --json headSha,status,conclusion,jobs,url
gh pr view 49 --json state,headRefOid,mergeCommit,statusCheckRollup,url
gh run view 31992478728 --json headSha,status,conclusion,jobs,url
gh release view product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v1 --json tagName,assets,url
gh run download 31992478728 --name ard-release-500138302-31992478728
gh release download product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v1
sha256sum prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d-v1.zip
unzip -t prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d-v1.zip
```

## Security and residual follow-ups

- Secret 값, signed attachment URL, provider prompt/response, private key, 원본 source payload는 이 문서에 기록하지 않았습니다.
- 이 release의 변경 불가는 GitHub Release의 `immutable` 설정이 아니라 annotated tag target과 기존 asset digest 충돌을 거부하는 저장소 workflow 정책으로 보장합니다.
- 현재 GitHub.com repository는 `public`, unarchived, default branch `main`입니다. 향후 private GitHub Enterprise 이전 시 attachment host, credential 전달, Environment secret, reusable workflow, branch/ruleset, runner, API, LFS, Release 경계를 mutation-free read-back부터 다시 검증해야 합니다.
- shared-table changeset E2E, 1인 review protection, 운영 장애 훈련, representative PDF 표본과 운영 지표는 [다음 작업](../next-steps.md)에 남아 있습니다.
