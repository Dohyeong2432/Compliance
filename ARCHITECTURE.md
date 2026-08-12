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
   1. 벡터 스토어 top-K 후보 (이미 dept/시계열 1차 필터링됨)
   2. 그래프 존재확인 — 그래프에 없는 id는 노이즈로 폐기 (환각 방지 핵심)
   3. 시계열 최신판 치환 — SUPERSEDES 체인에서 as_of 시점 유효 버전으로 교체
   4. RBAC 하드필터 — 치환된 버전에 대해 재검사(치환 전 버전과 다를 수 있으므로)
   5. 1-hop 그래프 확장 — 관련 판례/해석/FAQ를 추가 컨텍스트로 편입
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
`HybridRetriever.retrieve()`가 위 "전체 흐름"의 5단계를 그대로 구현합니다.
`dept` 없이는 호출 자체가 `ValueError`로 거부됩니다 — RBAC를 생략한 검색 경로가
아예 존재하지 않습니다.

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

### `pipeline/`
6개 커넥터(`pipeline/connectors/*`) 중 REGULATION/REVIEW/FAQ는 로컬 파일
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
