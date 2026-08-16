# Campaign Governance Monitor

- Product ID: `prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d`
- Product key: `500138302`
- Version: `v1`

## Overview

- **Description:** 이 데이터는 가상 캠페인과 소재의 상태, 집행 및 성과 신호를 함께 살펴보고 합성 데이터 거버넌스 분석을 연습하도록 구성되었습니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.

## Data source

- **Data type:** 정형
- **Storage location:** 가상 분석 저장소
- **Source name:** Marketing Insight

## Tags

- **Tag:** 가상 캠페인, 광고 소재, 집행 신호, 성과 신호

## Access and security

- **Access:** 손님 이상

## Ownership

- **Owner:** Synthetic Lab
- **Contact:** 가상 담당자/HE Lab(Synthetic Lab)

## AI readiness and quality

- **AI readiness:** Level 4 (Gold)
- **Quality:** 접근 권한 관리 여부 Y, 운영 관리 여부 Y, 메타 데이터 보유 여부 Y

## Datasets

| Dataset | Table ID | Table version | Source |
|---|---|---:|---|
| marketing_campaign | `tbl_01a00585-94b8-7e49-ac43-97e00a165e26` | v1 | `synthetic_workspace.marketing_insight.marketing_campaign` |
| marketing_creative | `tbl_01a00585-94b9-70f1-b339-c7b2e9d77704` | v1 | `synthetic_workspace.marketing_insight.marketing_creative` |
| marketing_delivery | `tbl_01a00585-94b9-72c1-8f98-d818ed98b0a8` | v1 | `synthetic_workspace.marketing_insight.marketing_delivery` |
| marketing_outcome | `tbl_01a00585-94b9-7cea-a110-ad22ea63a258` | v1 | `synthetic_workspace.marketing_insight.marketing_outcome` |
