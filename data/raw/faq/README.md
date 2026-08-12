# FAQ 원문 스테이징 폴더

실 사내 위키/포털 연동이 붙기 전까지, 사규와 동일한 방식으로 FAQ 원문을 미리
채워두는 용도입니다. `pipeline/connectors/faq.py`의 `LocalFileFaqConnector`가
자동으로 읽어 `RawDocument`로 변환합니다 — docx/doc/pdf를 그대로 올려두시면
됩니다.

## 파일 구성

한 파일에 Q&A 하나를 권장합니다. 첫 줄이 `title`(질문)로, 나머지 전체가
`body`(답변 포함 본문)로 추출됩니다:

```
고령투자자 기준이 뭔가요?

만 65세 이상 투자자를 말하며, 강화된 설명의무 및 조력자 참여 절차가
추가로 적용됩니다. 근거: 자본시장법 제46조(적합성 원칙).
```

## 근거 문서 연결 (ANSWERED_BY)

FAQ와 근거 법령/규정 간 관계(`ANSWERED_BY`)는 자동 추출되지 않습니다.
필요하면 `LocalFileFaqConnector().fetch()`가 반환한 `RawDocument` 목록을
받아 `relations=[(RelationType.ANSWERED_BY, "regulation:67")]` 등을 직접
채운 뒤 `ingest_documents()`에 넘기세요 — `data/raw/regulation/README.md`와
동일한 패턴입니다.

## 자동 추출되는 필드 / 알려진 제약

`data/raw/regulation/README.md`와 동일합니다 (external_id, title, body,
effective_date, DRM/레거시 인코딩 제약 포함).
