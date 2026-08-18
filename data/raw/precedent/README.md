# 계약검토 선례 스테이징 폴더

## ⚠️ 완료·승인된 사례만 올릴 것

`POST /contract-review`가 만든 자동 검토 결과(초안)를 그대로 이 폴더에
올리면 안 됩니다. 검증 안 된 자동 판단이 "선례"로 굳어지면, 같은 오류가
이후 검토에서 근거로 재인용되며 반복 재생산될 수 있습니다. **준법감시부가
검토를 마치고 승인한 사례 문서만** 올려주세요. 큐레이션은 이 폴더에
무엇을 올릴지 사람이 판단하는 행위 자체로 이루어지며, 별도 승인 절차나
상태 관리 기능은 시스템에 없습니다.

실 사내 사례관리 시스템 연동이 붙기 전까지, 다른 문서 기반 소스와 동일한
방식으로 로컬 스테이징 디렉터리를 씁니다. `pipeline/connectors/precedent.py`의
`LocalFilePrecedentConnector`가 자동으로 읽어 `RawDocument`로 변환합니다 —
docx/doc/pdf를 그대로 올려두시면 됩니다.

## ⚠️ 부서 한정(Chinese-wall) 문서는 반드시 하위 폴더에 넣을 것

계약검토 사례에는 계약 상대방·거래 금액 등 특정 부서만 봐야 하는 정보가
섞일 수 있습니다. **이 폴더 바로 아래에 놓인 파일은 전사 공개(`ALL`)로
색인됩니다.** 특정 부서만 봐야 하는 사례는 반드시 그 부서 코드 이름의
하위 폴더에 넣으세요(`data/raw/review/README.md`와 동일한 규칙):

```
data/raw/precedent/
├── IB/
│   └── 1. 2024 IB 업무위탁계약 검토 사례.docx  -> allowed_depts=("IB",)
└── 2. 전사 공통 비밀유지계약 검토 사례.docx     -> allowed_depts=("ALL",) (폴더 없이 루트)
```

## 자동 추출되는 필드

사규 스테이징 폴더(`data/raw/regulation/README.md`)와 동일합니다 —
`external_id`(파일명 선두 번호), `title`(본문 첫 줄), `body`, `effective_date`
(제정/개정 날짜 패턴)까지 동일한 방식으로 추출됩니다. 사규와 달리 조문
단위(`split_into_articles`)로 쪼개지 않고 파일 하나를 문서 하나로 색인합니다
— 사례 문서는 "제N조" 구조가 아니라 사례 서술문이기 때문입니다.

## 마스킹

사례 본문에 포함된 연락처·계좌번호 등은 색인 전에 자동으로 마스킹됩니다
(`pipeline/masking.py`, `IngestPipeline`이 REVIEW와 동일하게 PRECEDENT
타입에 대해서도 항상 적용) — 커넥터가 무엇이든 상관없이 적용되는 공통
단계이므로 별도 조치가 필요 없습니다.

## 검색에 활용되는 방식

`/contract-review`가 조항을 검토할 때 `search_knowledge(source_types=["precedent"])`로
유사한 과거 사례를 함께 찾아 참고합니다(`agent/contract_review.py`의
`CLAUSE_REVIEW_PROMPT_TEMPLATE`). 권위 위계상 가장 낮은 등급(참고 사례,
구속력 없음)으로 표시되므로, 법령·사내규정과 상충하는 내용이 있으면
LLM이 그 사실을 명시하도록 되어 있습니다.

## 알려진 제약

`data/raw/regulation/README.md`의 "알려진 제약"(DRM/암호화 파일, 레거시 .doc
인코딩)이 그대로 적용됩니다.
