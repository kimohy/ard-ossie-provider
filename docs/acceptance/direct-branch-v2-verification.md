# Direct Branch `v2` Production Verification

검증일은 2026-08-16이며 대상은 합성 Marketing Insight 제품 `500138301`입니다. 이 기록은 직접 브랜치 업데이트의 성공, immutable release, 재실행 수렴, stale/gap 거절, fork writeback 차단 계약을 실제 GitHub 실행으로 검증한 결과입니다.

## Scope and baseline

- 제품 ID: `prd_019ff10c-8be8-79d0-af07-21450abedf9e`
- 검증 시작 기준: `origin/main` `ee9c00d50706d260645712c76c674fe838788845`
- 기존 제품: `v1`, metric ID 11개, relationships 0개
- semantic PDF LFS OID: `ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6`, size `114912`
- dictionary XLSX LFS OID: `10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a`, size `14813`
- 기존 mapping: table 4개가 모두 `table_version: 1`
- 기존 table record SHA-256:
  - `tbl_01a00585-94b8-7e49-ac43-97e00a165e26`: `90c84b96199dcd2dfe4e5d04e497193dd281b1d8c680dc9b5c5475f1998d2dab`
  - `tbl_01a00585-94b9-70f1-b339-c7b2e9d77704`: `159075b97aed2ead80ab03942352c10325fb14ea3e8982136f31aa124d08aae2`
  - `tbl_01a00585-94b9-72c1-8f98-d818ed98b0a8`: `d05ee564ae3641781d0562c626ccb6bca8c327beb441fde1a89d711e261fe713`
  - `tbl_01a00585-94b9-7cea-a110-ad22ea63a258`: `8233b871e7870a0ee8093c19df1c769b9f67cb5842898ec372d23b2ac8aa8e6e`

## Successful candidate and trusted workflow

- 최종 authored candidate: `3fe31814678256ac862205b3bfc23e0de0406e17`
- source-only signal: [run 31920955288](https://github.com/kimohy/ard-ossie-provider/actions/runs/31920955288), exact candidate head에서 success
- trusted coordinator/processor: [run 31920963044](https://github.com/kimohy/ard-ossie-provider/actions/runs/31920963044), success
- processor commit: `54e534779af9121292525cc2783717b027665554`
- 제품 PR: [PR #43](https://github.com/kimohy/ard-ossie-provider/pull/43), processor head에서 `ard/quality-gate`와 `ard/changeset` 모두 success
- 두 required status target URL은 모두 coordinator run `31920963044`였습니다.

실제 production 실행 중 발견한 두 workflow 계약 결함도 TDD로 수정한 뒤 별도 보호 PR로 병합했습니다.

- 직접 업데이트 config 탐지: [PR #41](https://github.com/kimohy/ard-ossie-provider/pull/41), merge `967dbb4250163934d6c016ffd5bdec79d087900e`
- private repository branch head 조회: [PR #42](https://github.com/kimohy/ard-ossie-provider/pull/42), merge `81cf3cf203edbe8a1bcbccfd924f648ab28808e7`
- 보존된 진단 실행:
  - [run 31918386733](https://github.com/kimohy/ard-ossie-provider/actions/runs/31918386733): `CHANGESET_CONFIG_PRODUCT_MISMATCH`
  - [run 31918575855](https://github.com/kimohy/ard-ossie-provider/actions/runs/31918575855): `PRODUCT_ID_CONFLICT`
  - [run 31920003509](https://github.com/kimohy/ard-ossie-provider/actions/runs/31920003509): protected source-check 성공 뒤 `REMOTE_BRANCH_LOOKUP_FAILED`

## Product, metric, relationship, and table invariants

- Registry product version은 `2`이고 제품 ID/key는 유지됐습니다.
- 정렬한 v1/v2 metric ID 배열은 완전히 같고 개수는 11개입니다.
- relationships는 계속 빈 배열입니다.
- quality report는 hard error 0개입니다. 기존과 같은 `LLM_METRIC_SQL_UNSAFE` warning 2개는 unsafe optional metric 제안을 제외한 감사 기록입니다.
- validation report는 `status: verified`, `publishable: true`, fidelity coverage `1.0`입니다.
- 네 mapping은 모두 `table_version: 1`이고 `registry/tables/**`는 v1 기준과 byte-identical입니다.
- 네 table record의 SHA-256은 위 baseline 값과 같고 PDF/XLSX LFS pointer도 바뀌지 않았습니다.

## Merge, tag, Release asset, and SHA-256

- PR merge commit: `ba832033d308821a91fe6a163226a6ea36acf37a`
- numeric release: [run 31921644512](https://github.com/kimohy/ard-ossie-provider/actions/runs/31921644512), attempt 1과 attempt 2 모두 success
- product tag: `product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v2`, target `ba832033d308821a91fe6a163226a6ea36acf37a`
- [Marketing Insight v2 Release](https://github.com/kimohy/ard-ossie-provider/releases/tag/product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v2)
- asset: `prd_019ff10c-8be8-79d0-af07-21450abedf9e-v2.zip`
- workflow result, run artifact, GitHub Release asset의 SHA-256: `60d7438bfb55bae2b112afbd2718cceec9ee7872205305858ddef0cedac1c629`
- ZIP 전체 무결성 검사는 오류 없이 통과했습니다.
- release 결과의 `table_tags`는 빈 배열이고 `table/*/v2` tag는 존재하지 않습니다.

## `production-linkage` policy and dispatch convergence

- Environment는 custom deployment branch policy를 사용하며 허용 pattern은 `main` 하나입니다.
- `can_admins_bypass: true`이고 required-reviewer protection은 없습니다.
- 최초 linkage job은 downstream dispatch와 exact success status `ard/dispatched:prd_019ff10c-8be8-79d0-af07-21450abedf9e:v2`를 기록했습니다.
- 동일 release의 attempt 2는 기존 Release를 `noop`으로 재사용했습니다. artifact SHA-256은 그대로였고 linkage 결과는 `status: noop`, `dispatched: false`, mutations 0개였습니다.
- exact success status가 재-dispatch를 막는 단위 계약 테스트도 통과했습니다.

## `ard history` and `ard diff`

최신 `main`을 병합한 planning worktree에서 다음 명령을 실행했습니다.

```text
uv run --frozen ard history 500138301
uv run --frozen ard diff '500138301@v1..v2'
```

history는 최초 v1 생성 이력과 최종 processor commit `54e5347`을 포함합니다. diff는 product version `1 → 2`, 승인된 설명 변경과 regenerated product metadata를 보여주며 table record나 mapping version의 증가는 없습니다. v1 tag target은 `28f943db0afb28b820cf67818bd1b945c75c6765`, v2 tag target은 `ba832033d308821a91fe6a163226a6ea36acf37a`입니다.

## Stale-base rejection

- authored head: `c7d4a59d58df14c8d289e5c529fb6c1455be2acf`
- signal: [run 31921947691](https://github.com/kimohy/ard-ossie-provider/actions/runs/31921947691), success
- coordinator: [run 31921955295](https://github.com/kimohy/ard-ossie-provider/actions/runs/31921955295), `VERSION_STALE`로 failure
- remote branch head는 authored head 그대로이며 Draft PR, processor commit, repository status writeback이 없습니다.

## Skipped-version rejection

- authored head: `39b1c9edcea6aa5b4907828db977f44d83a9845e`
- signal: [run 31922024300](https://github.com/kimohy/ard-ossie-provider/actions/runs/31922024300), success
- coordinator: [run 31922033108](https://github.com/kimohy/ard-ossie-provider/actions/runs/31922033108), `VERSION_GAP`으로 failure
- remote branch head는 authored head 그대로이며 Draft PR, processor commit, repository status writeback이 없습니다.

## Fork identity-guard evidence

`tests/integration/test_workflow_contracts.py::test_direct_change_uses_read_only_signal_and_default_branch_coordinator`가 통과했습니다. 계약은 `github.event.workflow_run.head_repository.full_name == github.repository`를 요구하고 candidate checkout의 credential을 제거하며 validation permission을 read-only로 유지합니다. fork나 다른 repository identity는 writeback 단계에 도달하지 못합니다.

## Security and residual follow-ups

- Secret 값, provider prompt/response, private key, 원본 source payload는 이 문서에 기록하지 않았습니다.
- 성공·실패 candidate branch와 실패 run은 acceptance 증거로 보존했습니다. force push, tag 이동, generated 파일 수동 수정은 수행하지 않았습니다.
- `production-linkage` required reviewer 추가는 P1 데이터 경로와 별개의 운영 보호 후속 작업입니다.
- shared-table changeset E2E, review protection, 장애 훈련, representative PDF 표본은 다음 우선순위로 남습니다.
