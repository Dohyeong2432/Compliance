# 사내 규정/지침 원문 스테이징 폴더

`RegulationConnector`(`pipeline/connectors/regulation.py`)의 실 EDMS 연동이 붙기
전까지, 사규 원문을 미리 채워두는 용도입니다. **이 폴더를 자동으로 읽어 색인하는
로더는 아직 없습니다** — 지금은 파일을 여기 모아두는 스테이징 단계이고, 실제
`ingest.py` 파이프라인에 태우려면 각 문서를 다음 필드를 채운
`RawDocument`(`pipeline/connectors/base.py`)로 변환하는 과정이 한 번 더 필요합니다.

| 필드 | 설명 |
|---|---|
| `external_id` | 규정 고유 식별자 (예: `employee-financial-product-trading-guideline`) |
| `title` | 규정명 |
| `body` | 조문 본문 (검색/인용에 실제로 쓰이는 텍스트) |
| `effective_date` | 이번 개정의 시행일 |
| `superseded_date` | 다음 개정으로 대체된 날짜 (현행이면 비움) |
| `allowed_depts` | 열람 가능 부서. 전사 공통이면 `("ALL",)` |
| `relations` | 다른 법령/규정과의 관계 (예: `INTERPRETS`, `CITES`) |

## 파일 올리는 방법

원본 형식(docx/pdf) 그대로 올려두셔도 됩니다 — 나중에 로더를 만들 때
`pandoc -t markdown` 등으로 변환해 위 필드에 채워 넣습니다. 다만 다음 두 가지는
지금 파일명/디렉토리 구조로 표시해 주시면 변환 작업이 수월합니다.

1. **개정본이 여러 개인 규정**은 개정일자를 파일명에 포함해 주세요.
   예: `employee-financial-product-trading-guideline_2022-10-26.docx`
   (이전 개정본도 함께 올려두시면 `SUPERSEDES` 체인을 정확히 구성할 수 있습니다.)
2. **특정 부서 전용 문서**(전사 공개가 아닌 경우)는 하위 폴더명에 부서를 명시해
   주세요. 예: `ib-only/...`. 이 폴더는 `RBAC`가 걸리지 않은 상태이므로, 실제
   색인 전까지는 접근 권한이 필요한 문서를 여기 올리지 않는 것을 권장합니다.

## 다음 단계 (아직 미구현)

이 폴더가 채워지면 `LocalFileRegulationConnector` 같은 파일 기반 커넥터를 추가해
`ingest.py`로 바로 흘려보낼 수 있습니다. 필요해지면 요청해 주세요.
