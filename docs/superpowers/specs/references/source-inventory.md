# Source Inventory

## 확인한 요구사항 자료

| 자료 | 범위 | 상태 | 신뢰도 |
|---|---|---|---|
| 사용자 요구사항 | Git 배치, 모노레포, 복수 프러덕트, 자동 생성, 프러덕트별 버전 | 확인 | 높음 |
| 사용자 요구사항 | HTML 프러덕트 정보, Word/PDF 시맨틱 문서, Excel 데이터 딕셔너리 | 확인 | 높음 |
| 사용자 요구사항 | 프러덕트·테이블 불변 ID와 프러덕트–테이블 다대다 매핑 | 확인 | 높음 |
| 사용자 요구사항 | OpenAI-compatible API 설정 | 확인 | 높음 |

## 확인한 공식 기술 자료

| 자료 | 확인 내용 | 상태 |
|---|---|---|
| [Docling supported formats](https://docling-project.github.io/docling/usage/supported_formats/) | HTML, PDF, DOCX, XLSX 입력과 통합 문서 표현 지원 | 확인 |
| [Apache Ossie repository](https://github.com/apache/ossie) | JSON/YAML 스펙, converters, examples, validation 구성 | 확인 |
| [Ossie 0.1.1 schema](https://github.com/apache/ossie/blob/osi-0.1.1-rc1/core-spec/osi-schema.json) | 허용 dialect, vendor, dataset/field/relationship/metric 구조 | 확인 |
| [Ossie development schema](https://github.com/apache/ossie/blob/main/core-spec/osi-schema.json) | 0.2.0.dev0 상태와 BIGQUERY/vendor 확장 차이 | 확인 |
| [dbt Ossie integration](https://docs.getdbt.com/docs/build/ossie-semantic-models) | dbt Core 1.12+, Ossie 0.1.0/0.1.1, source 제약, I078 경고 | 확인 |
| [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) | JSON Schema 기반 구조화 출력과 SDK 지원 | 확인 |
| [SQLGlot](https://github.com/tobymao/sqlglot) | SQL parser/transpiler 사용 가능성 | 확인 |

## 구현 전에 필요한 실제 자료

| 자료 | 필요한 이유 | 현재 상태 |
|---|---|---|
| 대표 데이터 프러덕트 HTML 1개 | heading, table, link 추출 규칙 검증 | 미제공 |
| 대표 Word 또는 PDF 시맨틱 문서 1~2개 | 지표·계산식·업무 규칙 및 OCR 요구 검증 | 미제공 |
| 대표 Excel 데이터 딕셔너리 1개 | sheet/header/PK/FK/수식 템플릿 확정 | 미제공 |
| 실제 warehouse FQN 규칙 | FQN 정규화 및 동일 테이블 판별 | 미확정 |
| dbt manifest 또는 사용 여부 | dbt validation profile 활성화 판단 | 미확정 |
| OpenAI-compatible endpoint capability | API style과 JSON Schema 지원 검증 | 미확정 |
| CI 플랫폼과 secret 정책 | workflow와 credential injection 확정 | 미확정 |

실제 문서가 제공되기 전까지 본 설계는 source plan과 시스템 계약을 정의한다. 구현 단계의 parser mapping, 품질 임계값 및 golden fixture는 대표 문서를 기준으로 확정해야 한다.
