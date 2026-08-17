# Semantics 문서

# Marketing Insight Data Semantics

| 항목 | 내용 |
| --- | --- |
| 도메인 | Marketing Insight |
| 관련 키워드 | 가상 캠페인\, 광고 소재\, 집행 신호\, 성과 신호\, 합성 데이터 |
| 작성일 | 2026\-08\-11 |
| 작성자 | Synthetic Insights |
| 버전 | v1\.0 |

## 1\. 개요

본 문서는 Marketing Insight 도메인의 AI\-Ready Data Semantics 정의서이며\, AI Agent가 합성 데이터를 실제 운영 데이터로 오해하지\
않도록 업무 맥락과 용어의 의미를 정의합니다\.

대상은 가상 캠페인\, 광고 소재\, 집행 집계\, 성과 신호 데이터입니다\. 모든 명칭· 수치· 코드· 규칙은 설명을 위해 임의 생성되었으며 실제 고객\, 계정\, 매체\, 조직 또는 운영 정책을 나타내지 않습니다\.

## 2\. 테이블 리스트

| 테이블 명 | 설명 | Primary Key | Foreign Key |
| --- | --- | --- | --- |
| synthetic\_workspace\.marketing\_campaign | 가상 캠페인 기획 및 상태 데이터 | campaign\_id | \- |
| synthetic\_workspace\.marketing\_creative | 가상 광고 소재 메타데이터 | creative\_id | campaign\_id |
| synthetic\_workspace\.marketing\_delivery | 일자·캠페인·소재별 가상 집행 데이터 | event\_date\, campaign\_id\, creative\_id | campaign\_id\, creative\_id |
| synthetic\_workspace\.marketing\_outcome | 일자·캠페인별 가상 성과 신호 데이터 | event\_date\, campaign\_id | campaign\_id |

## 3\. 핵심 업무 용어

| 용어 | 정의 | 동의어 | 관련 테이블 |
| --- | --- | --- | --- |
| 캠페인 \(Campaign\) | 공통 목표와 기간을 갖는 가상 광고 기획 단위\. campaign\_id로 식별\. | Initiative | marketing\_campaign |
| 광고 소재 \(Creative\) | 메시지와 형식을 가진 가상 광고 표현 단위\. creative\_id로 식별\. | Variant | marketing\_creative |
| 목표 유형 \(Objective Type\) | Awareness\, Interest\, Action으로 구성한 추상 목표 분류\. | Goal Group | marketing\_campaign |
| 채널 그룹 \(Channel Group\) | Search\, Social\, Video\, Display로 구성한 추상 채널 그룹\. | Media Group | marketing\_campaign |
| 가상 권역 \(Market Zone\) | North\, South\, East\, West의 비현실적 분석 구분\. | Zone | marketing\_campaign |
| 소재 형식 \(Format Type\) | Image\, Short Video\, Text Card의 가상 표현 형식\. | Creative Format | marketing\_creative |
| 메시지 테마 \(Message Theme\) | Discovery\, Benefit\, Reminder의 가상 메시지 방향\. | Theme | marketing\_creative |
| 집행 \(Delivery\) | 일자·캠페인·소재 조합으로 집계한 가상 노출·반응·비용\. | Serving | marketing\_delivery |

## 3\. 핵심 업무 용어 \(계속\)

| 용어 | 정의 | 동의어 | 관련 테이블 |
| --- | --- | --- | --- |
| 노출 수 \(Impression Count\) | 가상 광고가 표시되었다고 가정한 합성 횟수\. | Impressions | marketing\_delivery |
| 반응 수 \(Engagement Count\) | 가상 광고에 반응했다고 가정한 합성 횟수\. | Engagements | marketing\_delivery |
| 관심 신호 \(Interest Signal\) | 콘텐츠 탐색을 모사한 비식별 합성 집계 신호\. | Interest Event | marketing\_outcome |
| 행동 신호 \(Action Signal\) | 의도 행동을 모사한 비식별 합성 집계 신호\. | Outcome Signal | marketing\_outcome |
| 가상 가치 \(Modeled Value\) | 성과 비교를 위해 임의 생성한 비화폐성 값\. | Synthetic Value | marketing\_outcome |
| 관찰 창 \(Attribution Window\) | D1\, D3\, D7로 표현한 설명용 가상 관찰 기간\. | Window | marketing\_outcome |
| 가상 군집 \(Audience Cluster\) | Aster\, Breeze\, Cobalt로 구성한 무의미한 군집 라벨\. | Synthetic Cluster | marketing\_delivery |

## 4\. 관계 및 집계 단위

| 개체 | 데이터 단위 | 결합 기준 |
| --- | --- | --- |
| Campaign | campaign\_id 당 1행 | campaign\_id |
| Creative | creative\_id 당 1행 | campaign\_id |
| Delivery | event\_date \+ campaign\_id \+ creative\_id 당 1행 | campaign\_id\, creative\_id |
| Outcome | event\_date \+ campaign\_id 당 1행 | campaign\_id |

## 5\. 업무 규칙 \(비즈니스 로직\)

| 번호 | Rule | 내용 |
| --- | --- | --- |
| 1 | 캠페인 기간 | end\_date는 start\_date보다 같거나 늦어야 한다\. |
| 2 | 캠페인·소재 관계 | 하나의 캠페인에는 여러 소재가 연결될 수 있으며 소재는 하나의 캠페인을 참조한다\. |
| 3 | 집행 데이터 단위 | 집행 데이터는 event\_date\, campaign\_id\, creative\_id 조합으로 유일해야 한다\. |
| 4 | 성과 데이터 단위 | 성과 신호는 event\_date\, campaign\_id 수준으로 집계한다\. |
| 5 | 테이블 결합 | 집행을 캠페인 수준으로 먼저 집계한 후 성과와 결합하여 중복 집계를 방지한다\. |
| 6 | 수치 범위 | 노출\, 반응\, 관심\, 행동\, 비용 및 가치 단위는 0 이상이어야 한다\. |
| 7 | 비율 계산 | 분모가 0인 경우 비율은 0이 아닌 공란으로 처리한다\. |
| 8 | 합성 데이터 경계 | 개인 식별자\, 실제 계정\, 실제 매체명 및 실제 화폐 금액을 포함하지 않는다\. |
| 9 | 의사 결정 제한 | 합성 수치와 임계값은 실제 예산 ·성과 판단에 사용하지 않는다\. |
| 10 | 상태 코드 | 캠페인\, 소재\, 집행 상태는 정의된 가상 코드 집합만 사용한다\. |

## 6\. 지표 정의서

| 지표명 | 정의 | 집계 공식 | 단위 | 관련 테이블\/컬럼 |
| --- | --- | --- | --- | --- |
| 캠페인 수 \(Campaign Count\) | 가상 캠페인의 수 | COUNT\(DISTINCT campaign\_id\) | 건 | marketing\_campaign\.campaign\_id |
| 활성 캠페인 수 \(Active Campaign Count\) | Active 상태의 가상 캠페인 수 | COUNT\(DISTINCT campaign\_id\) WHERE campaign\_status \= Active | 건 | marketing\_campaign |
| 소재 수 \(Creative Count\) | 가상 광고 소재 수 | COUNT\(DISTINCT creative\_id\) | 건 | marketing\_creative\.creative\_id |
| 노출 수 \(Impressions\) | 가상 광고 노출 합계 | SUM\(impression\_count\) | 회 | marketing\_delivery\.impression\_count |
| 반응 수 \(Engagements\) | 가상 광고 반응 합계 | SUM\(engagement\_count\) | 회 | marketing\_delivery\.engagement\_count |
| 반응률 \(Engagement Rate\) | 노출 대비 반응 비율 | SUM\(engagement\_count\) \/ NULLIF\(SUM\(impression\_count\)\,0\) | \% | marketing\_delivery |

## 6\. 지표 정의서 \(계속\)

| 지표명 | 정의 | 집계 공식 | 단위 | 관련 테이블\/컬럼 |
| --- | --- | --- | --- | --- |
| 집행 비용 단위 \(Spend Units\) | 시뮬레이션 비용 합계 | SUM\(spend\_units\) | unit | marketing\_delivery\.spend\_units |
| 반응당 비용 단위 \(Cost per Engagement\) | 반응 1 건당 시뮬레이션 비용 | SUM\(spend\_units\) \/ NULLIF\(SUM\(engagement\_count\)\,0\) | unit | marketing\_delivery |
| 관심 신호 수 \(Interest Signals\) | 가상 관심 신호 합계 | SUM\(interest\_signal\_count\) | 회 | marketing\_outcome |
| 행동 신호 수 \(Action Signals\) | 가상 행동 신호 합계 | SUM\(action\_signal\_count\) | 회 | marketing\_outcome |
| 행동 신호율 \(Action Signal Rate\) | 노출 대비 행동 신호 비율 | SUM\(action\_signal\_count\) \/ NULLIF\(SUM\(impression\_count\)\,0\) | \% | delivery \+ outcome |
| 가상 가치 단위 \(Modeled Value Units\) | 설명용 가상 가치 합계 | SUM\(modeled\_value\_units\) | unit | marketing\_outcome |
| 가상 효율 지수 \(Modeled Efficiency\) | 비용 단위 대비 가치 단위 | SUM\(modeled\_value\_units\) \/ NULLIF\(SUM\(spend\_units\)\,0\) | index | delivery \+ outcome |

## 7\. 활용 시 주의 사항

| 구분 | 내용 |
| --- | --- |
| 보안 | 개인정보와 실제 운영 식별자를 포함하지 않습니다\. |
| 수치 | 모든 결과값과 임계값은 합성 예시입니다\. |
| 도메인 | 특정 광고 플랫폼의 공식 정의와 일치한다고 가정하지 않습니다\. |
| 의사 결정 | 예산 배분\, 성과 평가\, 외부 보고에 사용할 수 없습니다\. |
| 결합 | Delivery는 캠페인 수준으로 선집계한 뒤 Outcome과 결합합니다\. |

## 8\. 품질 기준

| 품질 차원 | 가상 기준 |
| --- | --- |
| 완전성 | 필수 키 100\% 존재 |
| 유일성 | 정의된 복합 키 100\% 유일 |
| 유효성 | 수치 0 이상\, 날짜 범위 및 코드 집합 준수 |
| 최신성 | 예시 적재 주기 24시간 이내 |
| 개인정보 | 대상 컬럼 0 개 |
