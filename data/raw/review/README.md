# 검토서 원문 스테이징 폴더

실 EDMS 검토서함 연동이 붙기 전까지, 사규와 동일한 방식으로 검토서 원문을
미리 채워두는 용도입니다. `pipeline/connectors/review.py`의
`LocalFileReviewConnector`가 자동으로 읽어 `RawDocument`로 변환합니다 —
docx/doc/pdf를 그대로 올려두시면 됩니다.

## ⚠️ 부서 한정(Chinese-wall) 문서는 반드시 하위 폴더에 넣을 것

검토서는 이 시스템의 온톨로지에서 RBAC(차이니즈월)가 실제로 걸리는 소스입니다.
**이 폴더 바로 아래에 놓인 파일은 전사 공개(`ALL`)로 색인됩니다.** 특정 부서만
봐야 하는 검토서는 반드시 그 부서 코드 이름의 하위 폴더에 넣으세요:

```
data/raw/review/
├── IB/
│   └── 1. 2024 랩상품 출시 검토서.docx      -> allowed_depts=("IB",)
├── RETAIL/
│   └── 2. 신규 펀드 판매 검토서.docx        -> allowed_depts=("RETAIL",)
└── 3. 전사 공통 컴플라이언스 가이드.docx     -> allowed_depts=("ALL",) (폴더 없이 루트)
```

부서 코드는 별도로 정해진 값이 아니라 SSO 토큰의 `dept` 클레임과 정확히
일치해야 하는 자유 문자열입니다 (예: `IB`, `RETAIL`, `WM` 등 조직에서 실제
쓰는 코드를 그대로 폴더명으로 사용).

## 자동 추출되는 필드

사규 스테이징 폴더(`data/raw/regulation/README.md`)와 동일합니다 —
`external_id`(파일명 선두 번호), `title`(본문 첫 줄), `body`, `effective_date`
(제정/개정 날짜 패턴)까지 동일한 방식으로 추출됩니다.

## 마스킹

검토서 본문에 포함된 연락처·계좌번호 등은 색인 전에 자동으로 마스킹됩니다
(`pipeline/masking.py`, `IngestPipeline`이 REVIEW 타입에 대해 항상 적용) —
커넥터가 무엇이든 상관없이 적용되는 공통 단계이므로 별도 조치가 필요 없습니다.

## 알려진 제약

`data/raw/regulation/README.md`의 "알려진 제약"(DRM/암호화 파일, 레거시 .doc
인코딩)이 그대로 적용됩니다.
