# 사내 규정/지침 원문 스테이징 폴더

`RegulationConnector`(`pipeline/connectors/regulation.py`)의 실 EDMS 연동이 붙기
전까지, 사규 원문을 미리 채워두는 용도입니다. 이 폴더는 이제
`pipeline/connectors/local_file.py`의 `LocalFileRegulationConnector`가 자동으로
읽어 `RawDocument`로 변환합니다 — docx/doc/pdf를 그대로 올려두시면 됩니다. 사용법은
루트 [README.md](../../../README.md)의 "사규 원문을 채우려면" 절 참고.

## 자동 추출되는 필드

| 필드 | 추출 방식 |
|---|---|
| `external_id` | 파일명 맨 앞 번호 (예: `67. 임직원...docx` → `67`). 번호가 없으면 파일명을 슬러그화 |
| `title` | 변환된 본문의 첫 번째 비어있지 않은 줄 |
| `body` | 전체 본문 텍스트 |
| `effective_date` | 본문에서 `제정 YYYY.MM.DD` / `개정 YYYY.MM.DD` 패턴을 모두 찾아 가장 최근 날짜 |
| `allowed_depts` | 커넥터 생성 시 지정 (기본값 `("ALL",)`) |

`superseded_date`와 `relations`(다른 법령/규정과의 관계, 예: `SUPERSEDES`,
`CITES`)는 자동 추출되지 않습니다 — 여러 개정본을 함께 올리거나 문서 간 관계를
명시하고 싶다면 `LocalFileRegulationConnector`가 반환한 `RawDocument` 목록을
받아 직접 채운 뒤 `ingest_documents()`에 넘기세요.

## 파일 올리는 방법

원본 그대로(docx/doc/pdf) 올려두시면 됩니다.

1. **개정본이 여러 개인 규정**은 개정일자를 파일명에 포함해 주세요.
   예: `67. 임직원 금융투자상품 매매지침(2022.10.26).docx`
   (이전 개정본도 함께 올려두시면 `SUPERSEDES` 체인을 수동으로 구성할 수 있습니다.)
2. **특정 부서 전용 문서**(전사 공개가 아닌 경우)는 하위 폴더명에 부서를 명시해
   주세요. 이 폴더 자체는 RBAC가 걸리지 않은 상태이므로, 실제 색인 전까지는
   접근 권한이 필요한 문서를 여기 올리지 않는 것을 권장합니다.
3. **파일명에 특수한 의미를 주지 않는 확장자**(.txt, .md 등)는 커넥터가 무시합니다
   — 지원 형식은 `.docx` / `.doc` / `.pdf`뿐입니다.

## 알려진 제약

- **DRM/암호화 파일**: 확장자가 `.docx`여도 사내 문서보안 솔루션으로 암호화된
  파일은 파싱에 실패합니다 (예: `<DOCUMENT SAFER V2010 R2>` 워터마크가 붙은
  파일). 이런 파일은 `connector.errors`에 사유와 함께 기록되고 나머지 파일
  처리는 계속 진행됩니다 — 복호화된 버전으로 다시 올려주세요.
- **레거시 .doc 인코딩**: 대부분 CP949(EUC-KR)로 가정하고 변환합니다. 다른
  인코딩으로 저장된 옛 문서는 깨질 수 있습니다.
