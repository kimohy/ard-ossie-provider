<pre>500138301-Marketing Insight | Synthetic Data Semantics </pre>

# 1<br>Semantics 문서<br>

# Marketing Insight Data Semantics<br>

<pre>항목 </pre>

<pre>내용
</pre>

<pre>도메인 </pre>

<pre>Marketing Insight
</pre>

<pre>관련 키워드 </pre>

<pre>가상 캠페인, 광고 소재, 집행 신호, 성과 신호, 합성 데이터
</pre>

<pre>작성일 </pre>

<pre>2026-08-11
</pre>

<pre>작성자 </pre>

<pre>Synthetic Insights
</pre>

<pre>버전 </pre>

<pre>v1.0
</pre>

# 1\. 개요<br>

본 문서는 Marketing Insight 도메인의 AI\-Ready Data Semantics 정의서이며\, AI Agent가 합성 데이터를 실제 운영 데이터로 오해하지<br>않도록 업무 맥락과 용어의 의미를 정의합니다\.<br>

대상은 가상 캠페인\, 광고 소재\, 집행 집계\, 성과 신호 데이터입니다\. 모든 명칭·수치·코드·규칙은 설명을 위해 임의 생성되었으며 실제 고객\,<br>계정\, 매체\, 조직 또는 운영 정책을 나타내지 않습니다\.<br>

# 2\. 테이블 리스트<br>

<pre>테이블 명 </pre>

<pre>설명 </pre>

<pre>Primary Key </pre>

<pre>Foreign Key
</pre>

<pre>synthetic_workspace.marketing_campaign </pre>

<pre>가상 캠페인 기획 및 상태 데이터 </pre>

<pre>campaign_id </pre>

<pre>-
</pre>

<pre>synthetic_workspace.marketing_creative </pre>

<pre>가상 광고 소재 메타 데이터 </pre>

<pre>creative_id </pre>

<pre>campaign_id
</pre>

<pre>synthetic_workspace.marketing_delivery </pre>

<pre>일자·캠페인·소재별 가상 집행 데이터 </pre>

<pre>event_date, campaign_id,
</pre>

<pre>creative_id
</pre>

<pre>campaign_id,
</pre>

<pre>creative_id
</pre>

<pre>synthetic_workspace.marketing_outcome </pre>

<pre>일자·캠페인별 가상 성과 신호 데이터 </pre>

<pre>event_date, campaign_id </pre>

<pre>campaign_id</pre>

<pre>500138301-Marketing Insight | Synthetic Data Semantics </pre>

# 2<br>3\. 핵심 업무 용어<br>

<pre>용어 </pre>

<pre>정의 </pre>

<pre>동의어 </pre>

<pre>관련 테이블
</pre>

<pre>캠페인 (Campaign) </pre>

<pre>공통 목표와 기간을 갖는 가상 광고 기획 단위. campaign_id로 식별. </pre>

<pre>Initiative </pre>

<pre>marketing_campaign
</pre>

<pre>광고 소재 (Creative) </pre>

<pre>메시지와 형식을 가진 가상 광고 표현 단위. creative_id로 식별. </pre>

<pre>Variant </pre>

<pre>marketing_creative
</pre>

<pre>목표 유형 (Objective Type) </pre>

<pre>Awareness, Interest, Action으로 구성한 추상 목표 분류. </pre>

<pre>Goal Group </pre>

<pre>marketing_campaign
</pre>

<pre>채널 그룹 (Channel Group) </pre>

<pre>Search, Social, Video, Display로 구성한 추상 채널 그룹. </pre>

<pre>Media Group </pre>

<pre>marketing_campaign
</pre>

<pre>가상 권역 (Market Zone) </pre>

<pre>North, South, East, West의 비현실적 분석 구분. </pre>

<pre>Zone </pre>

<pre>marketing_campaign
</pre>

<pre>소재 형식 (Format Type) </pre>

<pre>Image, Short Video, Text Card의 가상 표현 형식. </pre>

<pre>Creative Format </pre>

<pre>marketing_creative
</pre>

<pre>메시지 테마 (Message
</pre>

<pre>Theme)
</pre>

<pre>Discovery, Benefit, Reminder의 가상 메시지 방향. </pre>

<pre>Theme </pre>

<pre>marketing_creative
</pre>

<pre>집행 (Delivery) </pre>

<pre>일자·캠페인·소재 조합으로 집계한 가상 노출·반응·비용. </pre>

<pre>Serving </pre>

<pre>marketing_delivery</pre>

<pre>500138301-Marketing Insight | Synthetic Data Semantics </pre>

<pre>3
</pre>

# 3\. 핵심 업무 용어 \(계속\)<br>

<pre>용어 </pre>

<pre>정의 </pre>

<pre>동의어 </pre>

<pre>관련 테이블
</pre>

<pre>노출 수 (Impression Count) </pre>

<pre>가상 광고가 표시되었다고 가정한 합성 횟수. </pre>

<pre>Impressions </pre>

<pre>marketing_delivery
</pre>

<pre>반응 수 (Engagement
</pre>

<pre>Count)
</pre>

<pre>가상 광고에 반응했다고 가정한 합성 횟수. </pre>

<pre>Engagements </pre>

<pre>marketing_delivery
</pre>

<pre>관심 신호 (Interest Signal) </pre>

<pre>콘텐츠 탐색을 모사한 비식별 합성 집계 신호. </pre>

<pre>Interest Event </pre>

<pre>marketing_outcome
</pre>

<pre>행동 신호 (Action Signal) </pre>

<pre>의도 행동을 모사한 비식별 합성 집계 신호. </pre>

<pre>Outcome Signal </pre>

<pre>marketing_outcome
</pre>

<pre>가상 가치 (Modeled Value) </pre>

<pre>성과 비교를 위해 임의 생성한 비화폐성 값. </pre>

<pre>Synthetic Value </pre>

<pre>marketing_outcome
</pre>

<pre>관찰 창 (Attribution
</pre>

<pre>Window)
</pre>

<pre>D1, D3, D7로 표현한 설명용 가상 관찰 기간. </pre>

<pre>Window </pre>

<pre>marketing_outcome
</pre>

<pre>가상 군집 (Audience
</pre>

<pre>Cluster)
</pre>

<pre>Aster, Breeze, Cobalt로 구성한 무의미한 군집 라벨. </pre>

<pre>Synthetic Cluster </pre>

<pre>marketing_delivery
</pre>

# 4\. 관계 및 집계 단위<br>개체&#32;

<pre>데이터 단위 </pre>

<pre>결합 기준
</pre>

<pre>Campaign </pre>

<pre>campaign_id 당 1행 </pre>

<pre>campaign_id
</pre>

<pre>Creative </pre>

<pre>creative_id 당 1행 </pre>

<pre>campaign_id
</pre>

<pre>Delivery </pre>

<pre>event_date + campaign_id + creative_id 당 1행 </pre>

<pre>campaign_id, creative_id
</pre>

<pre>Outcome </pre>

<pre>event_date + campaign_id 당 1행 </pre>

<pre>campaign_id</pre>

<pre>500138301-Marketing Insight | Synthetic Data Semantics </pre>

<pre>4
</pre>

# 5\. 업무 규칙 \(비즈니스 로직\)<br>

<pre>번호 </pre>

<pre>Rule </pre>

<pre>내용
</pre>

<pre>1 </pre>

<pre>캠페인 기간 </pre>

<pre>end_date는 start_date보다 같거나 늦어야 한다.
</pre>

<pre>2 </pre>

<pre>캠페인·소재 관계 </pre>

<pre>하나의 캠페인에는 </pre>

<pre>여러</pre>

<pre> 소재가 </pre>

<pre>연결될</pre>

<pre> 수 </pre>

<pre>있</pre>

<pre>으며 소재는 하나의 캠페인을 </pre>

<pre>참</pre>

<pre>조한다.
</pre>

<pre>3 </pre>

<pre>집행 데이터 단위 </pre>

<pre>집행 데이터는 event_date, campaign_id, creative_id 조합으로 유일해야 한다.
</pre>

<pre>4 </pre>

<pre>성과 데이터 단위 </pre>

<pre>성과 신호는 event_date, campaign_id 수</pre>

<pre>준</pre>

<pre>으로 집계한다.
</pre>

<pre>5 </pre>

<pre>테이</pre>

<pre>블 </pre>

<pre>결</pre>

<pre>합 </pre>

<pre>집행을 캠페인 수</pre>

<pre>준</pre>

<pre>으로 </pre>

<pre>먼저</pre>

<pre> 집계한 </pre>

<pre>후</pre>

<pre> 성과와 </pre>

<pre>결</pre>

<pre>합하</pre>

<pre>여 </pre>

<pre>중복</pre>

<pre> 집계를 방지한다.
</pre>

<pre>6 </pre>

<pre>수치 </pre>

<pre>범</pre>

<pre>위 </pre>

<pre>노출, 반응, 관심, 행동, 비용 및 가치 단위는 0 이상이어야 한다.
</pre>

<pre>7 </pre>

<pre>비</pre>

<pre>율</pre>

<pre> 계</pre>

<pre>산 </pre>

<pre>분모가 0인 </pre>

<pre>경우</pre>

<pre> 비</pre>

<pre>율</pre>

<pre>은 0이 </pre>

<pre>아닌</pre>

<pre> 공</pre>

<pre>란</pre>

<pre>으로 </pre>

<pre>처리</pre>

<pre>한다.
</pre>

<pre>8 </pre>

<pre>합성 데이터 </pre>

<pre>경</pre>

<pre>계 </pre>

<pre>개</pre>

<pre>인 식별자, 실제 계정, 실제 매체명 및 실제 화폐 </pre>

<pre>금액</pre>

<pre>을 </pre>

<pre>포함</pre>

<pre>하지 않는다.
</pre>

<pre>9 </pre>

<pre>의사</pre>

<pre>결</pre>

<pre>정 제한 </pre>

<pre>합성 수치와 임계값은 실제 </pre>

<pre>예산</pre>

<pre>·성과 </pre>

<pre>판</pre>

<pre>단에 사용하지 않는다.
</pre>

<pre>10 </pre>

<pre>상태 코드 </pre>

<pre>캠페인, 소재, 집행 상태는 정의</pre>

<pre>된</pre>

<pre> 가상 코드 집합</pre>

<pre>만</pre>

<pre> 사용한다.
</pre>

# 6\. 지표 정의서<br>

<pre>지표명 </pre>

<pre>정의 </pre>

<pre>집계 공식 </pre>

<pre>단위 </pre>

<pre>관련 테이블/컬럼
</pre>

<pre>캠페인 수 (Campaign Count) </pre>

<pre>가상 캠페인의 수 </pre>

<pre>COUNT(DISTINCT campaign_id) </pre>

<pre>건 </pre>

<pre>marketing_campaign.ca
</pre>

<pre>mpaign_id
</pre>

<pre>활</pre>

<pre>성 캠페인 수 (Active
</pre>

<pre>Campaign Count)
</pre>

<pre>Active 상태의 가상 캠페인 수 </pre>

<pre>COUNT(DISTINCT campaign_id) WHERE
</pre>

<pre>campaign_status = Active
</pre>

<pre>건 </pre>

<pre>marketing_campaign
</pre>

<pre>소재 수 (Creative Count) </pre>

<pre>가상 광고 소재 수 </pre>

<pre>COUNT(DISTINCT creative_id) </pre>

<pre>건 </pre>

<pre>marketing_creative.crea
</pre>

<pre>tive_id
</pre>

<pre>노출 수 (Impressions) </pre>

<pre>가상 광고 노출 합계 </pre>

<pre>SUM(impression_count) </pre>

<pre>회 </pre>

<pre>marketing_delivery.imp
</pre>

<pre>ression_count
</pre>

<pre>반응 수 (Engagements) </pre>

<pre>가상 광고 반응 합계 </pre>

<pre>SUM(engagement_count) </pre>

<pre>회 </pre>

<pre>marketing_delivery.eng
</pre>

<pre>agement_count
</pre>

<pre>반응</pre>

<pre>률</pre>

<pre> (Engagement Rate) </pre>

<pre>노출 대비 반응 비</pre>

<pre>율 </pre>

<pre>SUM(engagement_count) /
</pre>

<pre>NULLIF(SUM(impression_count),0)
</pre>

<pre>% </pre>

<pre>marketing_delivery</pre>

<pre>500138301-Marketing Insight | Synthetic Data Semantics </pre>

<pre>5
</pre>

# 6\. 지표 정의서 \(계속\)<br>

<pre>지표명 </pre>

<pre>정의 </pre>

<pre>집계 공식 </pre>

<pre>단위 </pre>

<pre>관련 테이블/컬럼
</pre>

<pre>집행 비용 단위 (Spend Units) </pre>

<pre>시</pre>

<pre>뮬레</pre>

<pre>이</pre>

<pre>션</pre>

<pre> 비용 합계 </pre>

<pre>SUM(spend_units) </pre>

<pre>unit </pre>

<pre>marketing_delivery.spe
</pre>

<pre>nd_units
</pre>

<pre>반응당 비용 단위 (Cost per
</pre>

<pre>Engagement)
</pre>

<pre>반응 1</pre>

<pre>건</pre>

<pre>당 시</pre>

<pre>뮬레</pre>

<pre>이</pre>

<pre>션</pre>

<pre> 비용 </pre>

<pre>SUM(spend_units) /
</pre>

<pre>NULLIF(SUM(engagement_count),0)
</pre>

<pre>unit </pre>

<pre>marketing_delivery
</pre>

<pre>관심 신호 수 (Interest Signals) </pre>

<pre>가상 관심 신호 합계 </pre>

<pre>SUM(interest_signal_count) </pre>

<pre>회 </pre>

<pre>marketing_outcome
</pre>

<pre>행동 신호 수 (Action Signals) </pre>

<pre>가상 행동 신호 합계 </pre>

<pre>SUM(action_signal_count) </pre>

<pre>회 </pre>

<pre>marketing_outcome
</pre>

<pre>행동 신호</pre>

<pre>율</pre>

<pre> (Action Signal
</pre>

<pre>Rate)
</pre>

<pre>노출 대비 행동 신호 비</pre>

<pre>율 </pre>

<pre>SUM(action_signal_count) /
</pre>

<pre>NULLIF(SUM(impression_count),0)
</pre>

<pre>% </pre>

<pre>delivery + outcome
</pre>

<pre>가상 가치 단위 (Modeled Value
</pre>

<pre>Units)
</pre>

<pre>설명용 가상 가치 합계 </pre>

<pre>SUM(modeled_value_units) </pre>

<pre>unit </pre>

<pre>marketing_outcome
</pre>

<pre>가상 </pre>

<pre>효율</pre>

<pre> 지수 (Modeled
</pre>

<pre>Efficiency)
</pre>

<pre>비용 단위 대비 가치 단위 </pre>

<pre>SUM(modeled_value_units) /
</pre>

<pre>NULLIF(SUM(spend_units),0)
</pre>

<pre>index </pre>

<pre>delivery + outcome
</pre>

# 7\. 활용 시 주의사항<br>구분&#32;

<pre>내용
</pre>

<pre>보</pre>

<pre>안 </pre>

<pre>개</pre>

<pre>인정보와 실제 운영 식별자를 </pre>

<pre>포함</pre>

<pre>하지 않습니다.
</pre>

<pre>수치 </pre>

<pre>모든 </pre>

<pre>결</pre>

<pre>과값과 임계값은 합성 </pre>

<pre>예</pre>

<pre>시입니다.
</pre>

<pre>도메인 </pre>

<pre>특</pre>

<pre>정 광고 </pre>

<pre>플랫폼</pre>

<pre>의 공식 정의와 일치한다고 가정하지 않습니다.
</pre>

<pre>의사</pre>

<pre>결</pre>

<pre>정 </pre>

<pre>예산 </pre>

<pre>배</pre>

<pre>분, 성과 </pre>

<pre>평</pre>

<pre>가, </pre>

<pre>외부</pre>

<pre> 보고에 사용</pre>

<pre>할</pre>

<pre> 수 </pre>

<pre>없</pre>

<pre>습니다.
</pre>

<pre>결</pre>

<pre>합 </pre>

<pre>Delivery는 캠페인 수</pre>

<pre>준</pre>

<pre>으로 </pre>

<pre>선</pre>

<pre>집계한 </pre>

<pre>뒤</pre>

<pre> Outcome과 </pre>

<pre>결</pre>

<pre>합합니다.
</pre>

# 8\. 품질 기준<br>

<pre>품질 차원 </pre>

<pre>가상 기준
</pre>

<pre>완</pre>

<pre>전성 </pre>

<pre>필</pre>

<pre>수 키 100% </pre>

<pre>존</pre>

<pre>재
</pre>

<pre>유일성 </pre>

<pre>정의</pre>

<pre>된 </pre>

<pre>복</pre>

<pre>합 키 100% 유일
</pre>

<pre>유</pre>

<pre>효</pre>

<pre>성 </pre>

<pre>수치 0 이상, </pre>

<pre>날짜 </pre>

<pre>범</pre>

<pre>위 및 코드 집합 </pre>

<pre>준</pre>

<pre>수
</pre>

<pre>최</pre>

<pre>신성 </pre>

<pre>예</pre>

<pre>시 적재 </pre>

<pre>주</pre>

<pre>기 24시간 이내
</pre>

<pre>개</pre>

<pre>인정보 </pre>

<pre>대상 </pre>

<pre>컬럼</pre>

<pre> 0</pre>

<pre>개</pre>
