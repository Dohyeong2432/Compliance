# 아키텍처

## 왜 하이브리드(벡터+그래프) RAG인가

마스터 기획서는 "Gemini Gems로 0원에 구축"하는 방안도 제시했지만, Gems 같은
프롬프트 빌더는 RBAC/시계열/인용검증을 전부 **프롬프트 지시문**으로만 강제할 수
있다는 구조적 한계가 있습니다. 프롬프트는 모델이 지키기로 "약속"하는 것이지 시스템이
강제하는 것이 아니므로, 탈옥·프롬프트 인젝션·단순 모델 실수 앞에서 깨질 수 있습니다.

이 구현체는 그 대신 각 요구사항을 **코드 레벨의 구조적 제약**으로 만듭니다.

| 요구사항 | 프롬프트만으로 구현 시 한계 | 이 구현체의 강제 지점 |
|---|---|---|
| 차이니즈월(RBAC) | "이 부서 문서는 보여주지 마" 지시는 우회 가능 | `dept`는 검증된 JWT 클레임에서만 파생(`agent/sso.py`), 도구 호출 인자로 override 불가(`agent/tools.py`) |
| 시계열 인지 | 모델이 "최신 버전"을 스스로 추론 → 환각 위험 | `SUPERSEDES` 체인을 그래프에서 직접 탐색해 질의 시점에 유효했던 버전을 결정적으로 반환(`knowledge/graph_store.py`) |
| 할루시네이션 방지 | "지어내지 마" 지시는 모델이 무시할 수 있음 | 벡터 검색 결과를 그래프 존재 여부로 교차검증하고, 답변의 `[[CITE:id]]`를 재검증해 통과분만 각주로 치환(`knowledge/retriever.py`, `agent/citation.py`) |
| 근거의 규범적 위계 | "법령을 우선 근거로 삼아라" 지시만으로는 내부 검토서가 법령보다 먼저·같은 무게로 인용될 수 있음 | 검색 결과를 권위 순으로 정렬해 제시하고, 문서 블록마다 `authority` 속성으로 규범적 지위를 명시(`ontology/schema.py` AUTHORITY_RANK, `agent/tools.py`) |

벡터 스토어 단독으로는 "그럴듯하게 유사한 텍스트"를 반환할 뿐 그 문서가 실제로
존재하고 여전히 유효한지 보장하지 않습니다. 지식그래프를 두 번째 진실 소스로 두고
모든 벡터 히트를 그래프에 대조 검증하는 것이 이 시스템의 핵심 방어선입니다.

## 전체 흐름

```
[배포 계층]      FastAPI(/chat)  ←  Authorization: Bearer <SSO JWT>
                        │
                        │  agent.sso.build_session_context(token)
                        │  (SSO 미설정 시 모든 요청 501 fail-closed)
                        ▼
[에이전트 하네스]  ComplianceAgent (agent/harness.py)
     │  system prompt + tool-calling 루프 (agent/prompts.py, llm_client.py)
     ├─ SessionContext.dept: 서명된 JWT 클레임에서만 옴, 그 외 어떤 경로로도 설정 불가
     ├─ search_knowledge 도구 → ToolDispatcher (세션 dept를 하드코딩해 주입, 도구 인자 무시)
     └─ CitationGuard: [[CITE:id]] 마커를 "이번 턴에 검색된 id ∩ 그래프에 실재하는 id"로
        재검증 → 통과분만 번호 각주로 치환, 실패분은 인라인으로 명시적 표시(침묵 삭제도,
        무조건 신뢰도 하지 않음)
                        │
[하이브리드 리트리버]  HybridRetriever.retrieve() (knowledge/retriever.py)
   1. 조문 인용("제N조") title 직접 매칭 — 조문 번호는 임베딩이 잡을 의미 정보가
      거의 없어 순수 벡터로는 못 찾음. 리랭킹 대상에서 제외해 보호
   2. 후보 회수 2채널 — BM25 어휘 검색(knowledge/lexical.py, 정형 용어 정확 일치)
      + 벡터 유사도 검색. 점수 스케일이 달라 RRF(순위 기반 융합)로 병합
   3. 그래프 존재확인 — 그래프에 없는 id는 노이즈로 폐기 (환각 방지 핵심)
   4. 시계열 최신판 치환 — SUPERSEDES 체인에서 as_of 시점 유효 버전으로 교체
   5. RBAC 하드필터 — 치환된 버전에 대해 재검사(치환 전 버전과 다를 수 있으므로)
   6. 리랭킹(knowledge/reranker.py) — (질의, 문서)를 함께 읽는 별도 모델로 후보
      풀을 재채점. RERANKER_BACKEND=none이면 RRF 순위 그대로(NoOp)
   7. 1-hop 그래프 확장 — 관련 판례/해석/FAQ를 추가 컨텍스트로 편입
   8. 권위 위계 정렬 — 법령 > 사내규정 > 유권해석 > 제재사례 > 검토서 > FAQ
      (ontology/schema.py AUTHORITY_RANK). 관련성이 "무엇을" 가져올지 정하고,
      규범적 위계가 "어떤 순서로 LLM에 제시할지"를 정한다
                        │
[지식 계층]  VectorStore(Chroma/InMemory) + GraphStore(Kuzu/NetworkX) + Embedder(Voyage/Hash)
                        │
[데이터 파이프라인]  IngestPipeline (pipeline/ingest.py)
     RawDocument → (REVIEW 타입이면 PII 마스킹) → Entity/Relation → 그래프+벡터 동시 색인
                        ▲
     IngestSyncer (pipeline/sync.py) — 시작 시 1회 + SYNC_INTERVAL_SECONDS마다
     반복 재색인, 사라진 문서는 그래프+벡터 양쪽에서 명시적으로 삭제
                        ▲
     6대 소스 커넥터(pipeline/connectors/*):
     LAW(국가법령정보센터) / INTERPRETATION(금융위·금감원 질의회신) /
     CASE(금감원 제재정보공개) — fetch_items 콜백으로 실 크롤러 연결
     REGULATION / REVIEW(부서 한정) / FAQ — 로컬 파일 스테이징(docx/doc/pdf)
```

## 모듈별 책임

### `ontology/schema.py`
6대 소스를 `EntityType`으로, 관계를 9종 `RelationType`으로 정의합니다.
`Entity.is_effective_at()` / `is_visible_to()`가 시계열·RBAC 판정의 단일 진실
소스이며, 그래프 스토어와 벡터 스토어 양쪽에서 동일한 의미로 재사용됩니다.

### `knowledge/graph_store.py`
`GraphStore`는 CRUD 원시 연산(엔티티/관계 추가·조회)만 추상 메서드로 두고,
`supersede_chain()` / `resolve_effective_version()` / `expand_related()`는
그 위에 구현한 **공유 로직**입니다. 따라서 `NetworkXGraphStore`(인메모리)와
`KuzuGraphStore`(임베디드 영속)가 시계열/확장 의미론에서 절대 어긋날 수 없습니다.

### `knowledge/vector_store.py`
`InMemoryVectorStore` / `ChromaVectorStore` 모두 검색 시 dept·시계열 필터와
`DEFAULT_MIN_SCORE` 컷오프를 동일하게 적용합니다. **`DEFAULT_MIN_SCORE`는
`HashEmbedder`의 점수 분포에 맞춘 경험적 값**이며, `VoyageEmbedder`로 교체 시
반드시 재보정해야 합니다.

### `knowledge/embedder.py`
`HashEmbedder`는 문자 n-gram 해싱 기반 더미로, **의미 유사도가 아니라 표면적
문자열 중첩만** 반영합니다. 개발 중 제재사례가 정답 조항보다 어휘적으로 더 겹쳐
순위가 밀려나는 사례가 실제로 확인되었습니다 — 파이프라인 동작 검증용으로만
신뢰하고, 검색 품질 판단에는 사용하지 마십시오. `VoyageEmbedder`는 요청 구성
(`_build_request`)과 응답 파싱(`_parse_response`)을 분리해 API 키 없이도 단위
테스트가 가능합니다.

실사규 4건을 `LocalFileRegulationConnector`로 색인해 재현한 두 번째 사례:
"자금세탁방지 고객확인" 질의에서 실제 정답 문서(자금세탁행위 등 방지 업무규정
시행세칙)의 원점수는 0.049로, `DEFAULT_MIN_SCORE`(0.05) 바로 아래에서 걸러져
검색결과 0건이 됐습니다. Voyage 등 의미 기반 임베더로 교체하기 전까지는 이런
근소한 컷오프 탈락이 상시 발생할 수 있다는 뜻이며, `DEFAULT_MIN_SCORE`를 낮추는
임시방편은 그만큼 무관한 문서의 오검색률을 높이므로 권장하지 않습니다.

### `pipeline/connectors/local_file.py`
`LocalFileConnector`는 entity_type을 매개변수로 받는 제네릭 로컬 파일
커넥터로, `data/raw/{regulation,review,faq}/`에 올려둔 원문(docx/doc/pdf)을
`pandoc`/`catdoc`/`pdftotext` CLI로 텍스트 추출해 `RawDocument`로 변환합니다.
`LocalFileRegulationConnector`/`LocalFileReviewConnector`/`LocalFileFaqConnector`는
이를 감싼 엔티티 타입별 얇은 서브클래스입니다. 실 EDMS/사내 위키 연동 전까지
쓰는 임시 로더입니다. 파일 하나가 파싱 실패해도(예: 확장자만 `.docx`인 DRM
암호화 파일) 전체 배치가 죽지 않고 `connector.errors`에 사유와 함께 기록되며
나머지 파일은 정상 처리됩니다 — 이 동작은 실제로 사규 파일 중 1개가 사내
문서보안 솔루션(`DOCUMENT SAFER`)으로 암호화되어 있던 것을 만나며 확인했습니다.

디렉터리는 재귀적으로 순회하며, 파일이 루트 바로 아래가 아니라 한 단계
하위 폴더에 있으면 그 폴더명이 `allowed_depts`가 됩니다(예:
`data/raw/review/IB/문서.docx` → `allowed_depts=("IB",)`). REVIEW는 이
시스템에서 RBAC가 실제로 걸리는 소스이므로, 부서 한정 검토서를 실수로 루트에
두면 전사 공개로 색인된다는 뜻입니다 — `data/raw/review/README.md`에 이 점을
명시해 두었습니다.

`LocalFileRegulationConnector`는 파일 하나를 문서 하나로 색인하는 REVIEW/FAQ와
달리 `split_into_articles()`로 본문을 조문(`제N조`) 단위로 쪼개 조문마다 별도
entity를 만듭니다(`_documents_from_file()` 오버라이드). title을 "{규정명}
제N조(조제목)" 형태로 만드는 게 목적인데, 이래야 `pipeline/citation_extraction.py`가
법령/타 사규 인용을 title 부분문자열로 매칭해 CITES 관계를 자동으로 걸 수
있습니다(그전엔 사규 entity가 파일 통째로 하나라 title에 "제N조"가 없어 이
매칭이 전혀 동작하지 않았습니다). 조문 헤딩이 하나도 없는 문서(정관 별표,
조직도 첨부 등)는 빈 리스트가 반환되어 예전처럼 파일 전체 단위 색인으로
폴백합니다.

조문 경계 판별에 괄호 제목 `(...)`을 필수 조건으로 둔 이유가 있습니다:
catdoc/pdftotext는 고정 폭으로 줄을 바꾸는데, 실제 사규 원문에서 "...법
제47조제4항에 따른..."처럼 문장 중간의 타 법령 인용이 줄바꿈 때문에 우연히
줄 맨 앞에 오는 사례가 다수 확인됐습니다(제목 없는 "제47조"). 반면 진짜 조문
헤딩은 예외 없이 "제N조(제목)"로 제목이 붙어 있어, 괄호를 필수로 요구하면 이
오탐이 걸러집니다. 부칙은 항상 "제1조(시행일)"부터 번호를 다시 매겨
시작하므로 별도 조문으로 분리하지 않고 통째로 마지막 조문 뒤에 붙입니다.
실사규 79건(지원 확장자 64건)으로 검증한 결과 파싱 에러 0건, 59개 파일이
조문 단위로 분리되어 총 1298개 조문 entity가 생성되었고, 조문 본문에서
837건의 법령/사규 인용 패턴이 검출됐습니다.

이 변경으로 entity id 체계가 파일당 1개(`regulation:64`)에서 조문당 1개
(`regulation:64-1`, `regulation:64-2`, ...)로 바뀝니다 — 기존에 파일 단위로
색인돼 있던 사규 데이터가 있다면 재동기화 시 옛 id는 삭제, 새 id는 전량
재임베딩되므로(`pipeline/sync.py`), 유료 임베더(Voyage/Gemini) 사용 중이라면
전환 시점의 1회성 재임베딩 비용을 고려해야 합니다.

`_AUTO_CITATION_SOURCE_TYPES`(`pipeline/ingest.py`)에도 REGULATION이
포함됩니다 — LAW는 여전히 인용 "대상"일 뿐 "출처"가 아니라 스캔 대상에서
제외되지만, 사규는 자신의 근거 법조항을 「」로 인용하는 게 실제 관행이라
(`「금융지주회사법」 제47조에 따라 ...`) 스캔 대상에 포함됩니다. 이로써 "법
제47조 개정 → CITES 역추적 → 영향받는 사규 조문 목록"을 유사도 검색이 아니라
그래프 순회로 구할 수 있습니다.

### `pipeline/connectors/crawler_base.py`
`CrawledSourceConnector`는 `LawConnector`/`InterpretationConnector`/
`CaseConnector`의 공통 베이스입니다. law.go.kr은 인증키가, 금융위/금감원
질의회신·제재정보는 공개 API 자체가 없어 사이트별 크롤링이 필요한데, 이
코드베이스는 그 크롤링(HTTP 호출, HTML/XML 파싱)을 대신 해주지 않습니다 —
검증할 수 없는 사이트 구조를 하드코딩하는 대신, `fetch_items`로 주입받은
콜백(인자 없이 `list[dict]` 반환)의 결과를 `RawDocument`/`Relation`으로
매핑하는 부분만 담당합니다. 관계 매핑은 편의 키(`supersedes`/`interprets`/
`violates`)와 범용 `relations` 키를 함께 지원합니다. `documents=[...]`로
생성하면(dev/test 전용) 크롤러 없이 고정 목록을 그대로 반환하는 기존 경로가
그대로 유지되며, `seed_data/seed.py`가 이 경로를 씁니다.

### `knowledge/retriever.py`
`HybridRetriever.retrieve()`가 위 "전체 흐름"의 8단계를 그대로 구현합니다.
`dept` 없이는 호출 자체가 `ValueError`로 거부됩니다 — RBAC를 생략한 검색 경로가
아예 존재하지 않습니다. 검색 경로가 4개(조문 인용/어휘/벡터/1-hop 확장)로 늘었지만
권한·시점 판정은 `_resolve()` 한 곳에만 있습니다. `entity_types`로 소스를 한정할 수
있고(도구의 `source_types` 파라미터), 이 필터는 4개 경로 전부에 동일하게 적용됩니다.

### `knowledge/lexical.py`
BM25 역색인. 임베딩은 "의미가 비슷한" 문서에 강하지만 정형 용어 정확 일치에는
약한데, 법률 도메인은 반대로 "이해상충", "겸직"처럼 토씨 하나가 개념을 가르는
용어 덩어리입니다. 한국어 형태소 분석기는 운영 부담이 커서 쓰지 않고, 어절 전체
term(정확 일치, IDF 높음) + 문자 바이그램(부분 일치 흡수)을 함께 색인해 대체합니다.
메모리 상주이며 매 sync 사이클 ingest가 다시 채우므로 별도 복구 절차가 없습니다.
RBAC/시점 판정은 하지 않고 `(entity_id, score)`만 돌려줍니다 — 권한 판정 지점을
늘리지 않기 위한 의도적 설계입니다.

### `knowledge/reranker.py`
회수 채널(벡터/BM25)은 질의와 문서를 따로 채점하지만, 리랭커는 둘을 함께 읽고
관련성을 다시 매깁니다. 후보 20~30개를 그대로 LLM에 넣으면 상당수가 노이즈이고
정답이 그 사이에 묻히는데, 이 구간을 담당합니다. 기본값은 `NoOpReranker`(자르기만
수행) — 질의마다 외부 API 호출이 추가돼 지연·비용이 늘기 때문에 켜는 것은 배포
환경의 선택(`RERANKER_BACKEND=voyage`)으로 남겼습니다.

### `agent/sso.py`
`build_session_context()`가 `SessionContext`를 만드는 유일한 함수입니다. `dept`는
`dept` JWT 클레임에서만 옵니다. `SSOConfig.from_env()`가 `None`을 반환하면(SSO
미설정) `build_session_context()`는 `SSOConfigError`를 던지고, API 계층은 이를
501로 변환해 **인증 없이는 어떤 요청도 통과시키지 않습니다**.

### `agent/tools.py` / `agent/citation.py` / `agent/audit.py`
- `ToolDispatcher`: 도구 호출 인자에 `dept`가 섞여 들어와도 무시하고 세션 dept만
  사용합니다.
- `CitationGuard`: 검증 실패 인용을 조용히 지우지도, 무조건 신뢰하지도 않고
  `[출처 미확인]`으로 명시합니다 — 할루시네이션이 "보이지 않는 오류"가 되지
  않도록 하는 설계입니다.
- `AuditLogger`: 모든 턴을 JSONL로 append-only 기록(요청자 dept, 검색된 id,
  검증/거부된 인용, 최종 답변).

### `agent/contract_review.py` / `agent/contract_docx.py` — 계약서 검토 (`POST /contract-review`)
`/chat`이 "짧은 질문 → 답변" 패턴인 것과 달리, 계약서 초안은 조항마다 개별
검토가 필요한 문서다. 계약서는 지식 그래프에 색인할 대상(`ontology.EntityType`)이
아니라 그때그때 검토하고 버리는 일회성 입력이므로, RAG 색인 파이프라인과는
별도의 요청-응답 경로로 만들었다. 새 LLM 오케스트레이션은 만들지 않고, 조항마다
기존 `ComplianceAgent.ask()`를 그대로 호출한다 — 도구 호출 루프, RBAC,
인용 검증, 감사 로그가 전부 조항 단위로 자동 재사용된다.

- `pipeline/korean_article_parser.py`의 `split_into_articles()`(원래
  `LocalFileRegulationConnector` 전용 "제N조(제목)" 파서, `pipeline/connectors/local_file.py`가
  재-export)를 계약서 조항 분리에도 그대로 재사용한다. 조문 헤딩이 없는
  계약서(영문 계약, 단순 번호 목록 등)는 전체 본문을 단일 "전체 본문" 조항으로
  취급해 통째로 검토한다 — 사규 파서와 동일한 폴백.
- `review_contract(text, agent)`가 조항마다
  `agent.ask(CLAUSE_REVIEW_PROMPT_TEMPLATE.format(...))`를 순차 호출해
  `ClauseReview`(라벨/원문/`AgentTurnResult`) 목록을 만든다.
- **조항 유형별 체크리스트** (`agent/contract_checklist.py`): "문제 있는지
  검토하세요"라는 자유형식 지시만으로는 조항마다 검토 깊이가 들쭉날쭉해진다.
  `checklist_for_label(label)`이 조항 라벨의 괄호 제목(예: "제15조(손해배상)"의
  "손해배상")을 키워드로 매칭해 손해배상/해지/관할/비밀유지/면책/업무위탁 등
  유형별 체크리스트(민법 제398조, 약관법 제14조, 금융지주회사법 제47조 등
  관련 법리 포함)를 골라 프롬프트에 삽입한다. 매칭 안 되는 유형과
  "전체 본문" 폴백은 범용 체크리스트로 떨어진다. `CLAUSE_REVIEW_PROMPT_TEMPLATE`도
  "위험도/문제 조항/근거/수정 제안" 4개 필드를 강제해 답변을 구조화한다.
- **도구 호출 한도**: 계약 조항은 여러 법령이 얽혀 근거 조사가 한 번의
  왕복으로 안 끝날 수 있다. `agent/contract_review.py`의
  `CONTRACT_REVIEW_MAX_TOOL_ITERATIONS = 8`을 `/contract-review`에서만 쓰고
  (`api/main.py`의 `_build_agent(..., max_tool_iterations=...)`),
  `/chat`은 기존 `agent.harness.MAX_TOOL_ITERATIONS`(4) 그대로 둔다 — 짧은
  채팅 질문에까지 한도를 늘리면 지연·비용만 커진다.
- **계약검토 선례 DB** (`EntityType.PRECEDENT`, `pipeline/connectors/precedent.py`):
  "과거에 비슷한 조항을 어떻게 판단했는지"를 지속적으로 보완하려면 근거
  축적이 필요한데, `/contract-review`가 만든 검토 결과를 자동으로 이 소스에
  채워넣지 않는다 — 검증 안 된 자동 판단이 그대로 "선례"로 굳어지면 같은
  오류가 반복 재생산될 위험이 있기 때문이다. 대신 REGULATION/REVIEW/FAQ와
  동일한 로컬 파일 스테이징 패턴을 그대로 재사용한다: 준법감시부가
  검토·승인한 사례 문서만 `data/raw/precedent/`(`PRECEDENT_DOCS_DIR`)에
  올리면 `LocalFilePrecedentConnector`가 자동 색인한다(조문 분리 없이
  파일 하나 = 문서 하나, REVIEW/FAQ와 동일). 권위 위계상 최하위(FAQ보다도
  아래, `AUTHORITY_RANK[PRECEDENT] = 7`)로 두고, `CLAUSE_REVIEW_PROMPT_TEMPLATE`이
  `source_types=["precedent"]`로 유사 사례를 찾아보라고 LLM에 안내한다.
  체크리스트(정적으로 "무엇을 확인할지")와 선례 DB(동적으로 "그 판단에
  참고할 근거")는 상호보완적인 별개의 두 축이다.
- `build_review_document(...)`가 `python-docx`로 검토의견서를 조립한다. 원본
  워드파일에 코멘트를 삽입하는 대신 별도 문서를 새로 생성하는 방식을
  택했다 — python-docx의 네이티브 코멘트 API는 저수준 OOXML 조작이 필요해
  원본 문서 구조를 깨뜨릴 위험이 있다. 구조화 필드(위험도/문제 조항/근거/
  수정 제안) 라벨은 볼드 처리해 훑어보기 쉽게 한다.
- `POST /contract-review`(`api/main.py`)는 업로드 파일을 임시파일로 저장 후
  기존 `_extract_text()`(pandoc/catdoc/pdftotext)로 텍스트화하고, 검토 완료 후
  `.docx` 바이너리를 응답으로 돌려준다. **동기 처리**다 — 조항 수만큼
  `agent.ask()`가 순차로 도므로(조항당 최대 8회 도구 호출), 조항이 많은
  계약서는 응답까지 수 분 걸릴 수 있다. Job 큐는 의도적으로 만들지 않았다;
  타임아웃이 실제로 문제가 되면 이후 비동기 방식으로 전환 검토.

### `pipeline/`
7개 커넥터(`pipeline/connectors/*`) 중 REGULATION/REVIEW/FAQ/PRECEDENT는 로컬 파일
스테이징으로, LAW/INTERPRETATION/CASE는 주입된 크롤러 콜백으로 실제 동작합니다
(둘 다 `documents=`를 주입하면 dev-mode로 고정 목록을 그대로 반환하는 경로도
그대로 남아 있어, `seed_data/seed.py`나 테스트에서 파이프라인 전체를
end-to-end로 검증할 수 있습니다). `IngestPipeline`은 `EntityType.REVIEW` 문서에
대해 커넥터 구현 여부와 무관하게 항상 `pipeline/masking.py`로 마스킹을 적용합니다.

### `pipeline/sync.py`
`IngestSyncer`가 "문서 자동 재색인/동기화"를 담당합니다. `GraphStore.add_entity`
/ `VectorStore.upsert`가 이미 id 기준 upsert이므로, 각 커넥터의 현재 `fetch()`
결과를 그대로 다시 `ingest_documents()`에 넘기기만 해도 추가/수정 반영은 공짜로
됩니다. 이 클래스가 그 위에 얹는 유일한 로직은 **삭제 감지**입니다: 소스별로
마지막에 본 id 집합을 기억해두었다가, 이번 fetch()에서 사라진 id를
`graph_store.delete_entity()` / `vector_store.delete()`로 명시적으로 지웁니다
— 그냥 재색인만 반복하면 삭제된 원문(예: data/raw/regulation/에서 지운
파일)이 계속 검색 결과에 남는데, 이 프로젝트가 막으려는 "조용히 틀린 답"의
전형적인 사례이기 때문입니다. 소스 하나의 `fetch()`가 실패해도(크롤러 다운 등)
나머지 소스는 계속 처리되며, `state_path`를 지정하면 마지막 id 집합을 JSON으로
영속화해 프로세스 재시작 후에도 삭제 감지가 끊기지 않습니다(다만 그 상태
자체가 한 번도 기록되기 전에 지워진 문서는 소급 감지할 수 없다는 한계는
클래스 docstring에 명시해 두었습니다). `api/main.py`의 lifespan이 시작 시
1회 `sync_once()`를 실행하고, `SYNC_INTERVAL_SECONDS > 0`이면
`run_forever()`를 백그라운드 태스크로 띄웁니다. `POST /admin/resync`는 이
주기를 기다리지 않고 즉시 재색인을 트리거합니다(같은 SSO 인증 게이트 사용,
아직 별도 역할 기반 권한 검사는 없음).

## 테스트로 검증한 시나리오

`seed_data/seed.py`는 마스터 기획서 시나리오(고령투자자 랩상품 적합성 원칙)를
법령 신구법, 유권해석, 제재사례, IB 전용 검토서, FAQ로 구성해 다음을 재현합니다
(`tests/test_e2e_seed.py`):

- 2021년 시점 질의 → 구법 반환, 신법·2022년 이후 제재사례는 배제
- 2024년 시점 질의 → 신법 반환, 관련 제재사례가 확장 노출
- IB 전용 검토서는 IB 세션에만 노출, 다른 부서 세션에는 검색 결과 자체가 0건
- 검토서 원문의 연락처·계좌번호는 색인 전 마스킹됨
- 존재하지 않는 id를 인용하면 각주가 아니라 `[출처 미확인]`으로 표시되고 감사로그에
  거부 이력이 남음

## 남은 작업

README.md의 "남은 작업" 절 참고.
