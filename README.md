# 그룹 AI 준법감시 에이전트

한국투자금융지주 그룹 AI 준법감시 에이전트의 하이브리드 RAG(벡터+지식그래프) 백엔드
구현체입니다. 자세한 설계 배경과 각 계층의 책임은 [ARCHITECTURE.md](ARCHITECTURE.md)를
참고하세요.

## 핵심 설계 목표

- **환각 방지**: 벡터 검색 결과를 지식그래프로 교차검증하고, LLM이 답변에 붙인
  `[[CITE:id]]` 인용 마커를 실제로 검색된 문서 id와 그래프 존재 여부로 재검증합니다.
- **시계열 인지**: 법령/규정의 개정 이력을 `SUPERSEDES` 관계 체인으로 표현하여, 질의
  시점(`as_of`) 기준으로 유효했던 버전을 정확히 반환합니다.
- **차이니즈월(RBAC)**: 세션의 `dept`는 오직 검증된 SSO JWT 클레임에서만 파생되며,
  사용자·LLM·도구 호출 인자 그 무엇도 이를 override할 수 없습니다.

## 빠른 시작

```bash
pip install -r requirements.txt
python -m pytest -q          # 전체 테스트 (기본 in-memory 백엔드로 실행)
```

`LocalFileRegulationConnector`(사규 원문 docx/doc/pdf 로더)를 쓰려면 시스템에
CLI 도구 `pandoc`, `catdoc`, `pdftotext`(poppler-utils 패키지)가 설치되어 있어야
합니다 (`apt-get install pandoc catdoc poppler-utils`). Python 패키지만으로는
해결되지 않는 부분이라 requirements.txt에는 포함되어 있지 않습니다.

### Windows에서 처음 설정할 때

사내망 환경(SSL 인터셉션 프록시·백신)과 법령 크롤러 의존성을 하나씩 뒤늦게
발견하며 서버를 여러 번 재시작하는 일이 없도록, 처음 설정 시 아래 순서를
한 번에 따라가세요.

```powershell
# 1. 가상환경 생성 + 활성화
python -m venv venv
.\venv\Scripts\Activate.ps1

# 2. 서버 기본 의존성 설치
pip install -r requirements.txt

# 3. Windows 시스템 인증서 저장소를 그대로 쓰도록 패치 -- 사내 프록시·백신이
#    HTTPS를 가로채는 환경(SSL 인터셉션)에서는 이게 없으면 Gemini/Voyage 등
#    외부 API 호출이 CERTIFICATE_VERIFY_FAILED로 실패합니다. 미리 깔아두면
#    나중에 겪을 필요가 없습니다.
pip install pip-system-certs

# 4. 법령 크롤러(LAW_CRAWLER)를 쓸 경우에만: 크롤러 전용 의존성 설치
#    -- selenium/pandas/beautifulsoup4 등 서버 배포판에는 일부러 안 넣은
#    별도 requirements 파일입니다 (crawlers/requirements.txt 참고).
pip install -r crawlers/requirements.txt
```

Python 패키지 설치만으로 끝나지 않는 것도 두 가지 있습니다:

- **법령 크롤러**: Selenium이 제어할 **Chrome(또는 Chromium) 브라우저**가
  로컬에 설치되어 있어야 합니다(드라이버는 `webdriver-manager`가 자동으로
  받아오지만, 브라우저 자체는 별도 설치가 필요합니다).
- **사규/검토서/FAQ/계약검토 선례 문서 로더**: 위에서 언급한 `pandoc`,
  `catdoc`, `pdftotext`가 PATH에 있어야 합니다. 설치 여부는 아래로 확인:
  ```powershell
  Get-Command pandoc, catdoc, pdftotext -ErrorAction SilentlyContinue
  ```
  `pandoc`은 [pandoc.org](https://pandoc.org/installing.html) 설치파일(.msi) 또는
  `winget install --id JohnMacFarlane.Pandoc`로 쉽게 깔립니다. `catdoc`,
  `pdftotext`(poppler)는 Windows 공식 설치파일이 따로 없어 바이너리를 받아
  PATH에 직접 등록해야 합니다.

설정이 끝나면 서버를 띄운 뒤 `/admin/resync`(아래 "자동 재색인/동기화" 참고)를
호출해 소스별 `ingested` 건수가 0이 아닌지 확인하세요 — 색인 성공 로그
(`logger.info()`)는 기본 로깅 설정상 콘솔에 표시되지 않으므로, 이 API 응답이
실제로 문서가 색인됐는지 확인할 수 있는 가장 확실한 방법입니다.

## 환경변수(.env) 관리

비밀값(SSO 시크릿, API 키 등)은 `.env.example`을 복사해 `.env`로 만들고 채우세요.
`.env`는 `.gitignore`에 있어 커밋되지 않으며, `bootstrap.py`가 시작 시 자동으로
읽어들입니다(이미 shell에 export된 값이 있으면 그쪽이 우선합니다).

```bash
cp .env.example .env
# .env 파일을 열어 SSO_JWT_SECRET 등 실제 값 채우기
```

로컬 개발 서버 (in-memory 백엔드, SSO는 반드시 설정해야 요청이 열립니다):

```bash
uvicorn api.main:app --reload
```

**무료로 POC만 해보고 싶다면** `ANTHROPIC_API_KEY`/`VOYAGE_API_KEY` 없이 Google AI
Studio의 무료 Gemini 키 하나로도 돌릴 수 있습니다:
```
LLM_BACKEND=gemini
EMBEDDER_BACKEND=gemini
GEMINI_API_KEY=<AI Studio에서 발급받은 키>
```
(무료 등급은 분당/일별 요청 수 제한이 있으니, 대량 문서 재색인이나 반복 질의를
많이 돌리면 제한에 걸릴 수 있습니다.) 나중에 실제 운영 단계로 넘어갈 땐 위
두 값만 `anthropic`/`voyage`로 되돌리면 됩니다 — 코드 변경은 필요 없습니다.

시드 데이터(고령투자자 랩상품 시나리오)를 채우려면:

```python
from bootstrap import build_components
from pipeline.ingest import IngestPipeline
from seed_data.seed import seed_all

components = build_components()
pipeline = IngestPipeline(components.embedder, components.vector_store, components.graph_store)
seed_all(pipeline)
```

## 문서 기반 소스: 사규 / 검토서 / FAQ / 계약검토 선례

네 소스 모두 실 시스템(EDMS/사내 위키/사례관리시스템) 연동 전까지 같은 방식으로
채웁니다 — docx/doc/pdf 원문을 아래 디렉터리에 그대로 올려두면 됩니다.
`uvicorn api.main:app`으로 서버를 띄우면 **시작 시 자동으로 색인되므로, 별도
스크립트를 수동으로 돌릴 필요는 없습니다** (아래 "자동 재색인/동기화" 참고).
서버 없이 한 번만 색인하고 싶다면:

```python
from bootstrap import build_components
from pipeline.ingest import IngestPipeline
from pipeline.connectors.local_file import LocalFileRegulationConnector
from pipeline.connectors.review import LocalFileReviewConnector
from pipeline.connectors.faq import LocalFileFaqConnector
from pipeline.connectors.precedent import LocalFilePrecedentConnector

components = build_components()
pipeline = IngestPipeline(components.embedder, components.vector_store, components.graph_store)

for connector in [
    LocalFileRegulationConnector("data/raw/regulation"),
    LocalFileReviewConnector("data/raw/review"),   # 부서 한정 문서는 하위 폴더로 구분 (data/raw/review/README.md)
    LocalFileFaqConnector("data/raw/faq"),
    LocalFilePrecedentConnector("data/raw/precedent"),  # 준법감시부가 승인한 사례만 (data/raw/precedent/README.md)
]:
    pipeline.ingest_connector(connector)
    print(connector.entity_type.value, "실패:", connector.errors)  # DRM/손상 등으로 못 읽은 파일과 사유
```

네 커넥터 모두 시스템에 다음 CLI 도구가 설치되어 있어야 합니다: `pandoc`(.docx),
`catdoc`(.doc, 레거시 CP949 인코딩), `pdftotext`(.pdf, poppler-utils 패키지).
확장자만 `.docx`인데 실제로는 DRM 등으로 암호화된 파일은 파싱에 실패하며
`connector.errors`에 사유와 함께 남고, 나머지 파일 처리는 막히지 않습니다.

## 크롤러 기반 소스: 법령 / 유권해석 / 제재사례

law.go.kr Open API는 인증키가, 금융위/금감원 질의회신·제재정보는 공개 API 자체가
없어 사이트별 크롤링이 필요합니다. 이 코드베이스는 그 크롤링(HTTP 호출, 파싱)을
대신 해주지 않습니다 — `LawConnector` / `InterpretationConnector` / `CaseConnector`는
`fetch_items`로 주입한 콜백(인자 없이 `list[dict]`를 반환)이 반환한 결과를
`RawDocument`/관계로 변환하는 것만 담당합니다. dict 스키마는
[`pipeline/connectors/crawler_base.py`](pipeline/connectors/crawler_base.py) 참고.

law.go.kr 크롤러는 [`crawlers/law_go_kr.py`](crawlers/law_go_kr.py)에 실제
구현되어 있고, `crawl_watchlist_items_incremental()`이 매 사이클 전체를 다시
크롤링하는 대신 공포일자/발령일자가 바뀐 법령만 상세 페이지를 다시 여는
증분 버전입니다 — 자세한 동작 방식은 [`crawlers/README.md`](crawlers/README.md) 참고.

```python
def crawl_law_items() -> list[dict]:
    ...  # law.go.kr Open API 호출 + XML 파싱은 직접 구현
    return [{"id": "...", "title": "...", "body": "...", "effective_date": "2023-07-01",
              "supersedes": "law:이전버전id"}]

connector = LawConnector(fetch_items=crawl_law_items)
```

서버가 이 커넥터들을 자동으로 등록하게 하려면 `.env`에 `LAW_CRAWLER=my_pkg.mod:crawl_law_items`
형태로 지정하세요(`INTERPRETATION_CRAWLER`, `CASE_CRAWLER`도 동일). 미설정 시 해당
소스는 에러 없이 그냥 재색인 대상에서 빠집니다.

## 임베딩 캐시

재색인은 매 사이클 모든 소스의 fetch() 결과 전체를 다시 임베딩 API(Voyage/Gemini)로
보내는 게 기본 동작입니다 — 사규 파일이든 크롤러 결과든, 바뀐 게 없어도
매번 다시 임베딩하면 유료/무료 등급 API 호출량이 불필요하게 커집니다.
`EMBED_CACHE_PATH`(기본 `./data/embed_cache.json`)를 지정하면
`IngestPipeline`이 문서 id별로 "마지막으로 임베딩한 텍스트의 해시값 +
그때 벡터"를 캐시해뒀다가, 텍스트가 그대로면 임베딩 API를 다시 부르지
않고 캐시된 벡터를 그대로 씁니다. 그래프/벡터 스토어에 넣는 것(upsert)
자체는 캐시 여부와 상관없이 매번 합니다 — `VECTOR_STORE_BACKEND=memory`는
재시작하면 비어 있으므로, 임베딩만 아끼고 저장은 항상 다시 해야 검색이
됩니다. 캐시를 끄고 싶으면(항상 재임베딩) `EMBED_CACHE_PATH`를 빈 값으로
두세요.

## 자동 재색인/동기화

`uvicorn api.main:app`으로 띄운 서버는:

1. **시작 시** 등록된 모든 소스(사규/검토서/FAQ 로컬 디렉터리 + 설정된 크롤러)를
   1회 전체 재색인합니다 — 빈 상태로 뜨지 않습니다.
2. **`SYNC_INTERVAL_SECONDS`(기본 1800초)마다** 백그라운드에서 반복 재색인합니다.
3. 파일 하나가 추가/삭제되어도 사람이 다시 스크립트를 돌릴 필요가 없습니다 —
   `GraphStore.add_entity`/`VectorStore.upsert`는 id 기준 upsert라 수정분은 그냥
   반영되고, 이전엔 있었는데 이번엔 사라진 id는 `pipeline/sync.py`의
   `IngestSyncer`가 그래프·벡터 스토어 양쪽에서 명시적으로 삭제합니다(그래야
   삭제된 사규가 계속 검색되는 사고를 막을 수 있습니다).
4. 재시작 후에도 삭제 감지가 끊기지 않도록, 소스별로 마지막에 색인했던 id 목록을
   `SYNC_STATE_PATH`(기본 `./data/sync_state.json`)에 저장해둡니다.

다음 주기를 기다리지 않고 즉시 재색인하려면 (예: 사규 파일을 방금 업로드한 직후):

```bash
curl -X POST http://localhost:8000/admin/resync -H "Authorization: Bearer <SSO JWT>"
```

## 계약서 조항별 검토 (`POST /contract-review`)

`/chat`이 짧은 질문 하나에 답하는 것과 달리, `/contract-review`는 계약서 초안
(.docx/.doc/.pdf)을 업로드받아 조항("제N조(제목)") 단위로 관련 법령/사규를
검색해 검토하고, 결과를 검토의견서 `.docx`로 돌려줍니다. 계약서 자체는 지식
그래프에 색인되지 않는 일회성 입력입니다.

```bash
curl -X POST http://localhost:8000/contract-review \
  -H "Authorization: Bearer <SSO JWT>" \
  -F "file=@계약서초안.docx" \
  -o 검토의견서.docx
```

손해배상/계약해지/관할/비밀유지/면책/업무위탁 등 조항 유형은 라벨의 괄호
제목으로 자동 인식해 유형별 체크리스트(민법 제398조 손해배상액 감액,
약관법 제14조 불공정 관할조항 등)를 검토에 반영하고, 답변은 "위험도/문제
조항/근거/수정 제안" 4개 필드로 구조화됩니다.

**동기 처리**입니다 — 조항 수만큼 순차로 검토가 진행되므로(조항당 최대 8회
도구 호출 — 여러 법령이 얽힌 조항을 충분히 조사할 수 있도록 `/chat`의
기본 4회보다 늘려뒀습니다), 조항이 많은 계약서는 응답까지 수 분 걸릴 수
있습니다. 별도 job 큐는 없습니다. 조문 헤딩("제N조(제목)")이 없는 계약서는
전체 본문을 한 건으로 취급해 검토합니다.

## Windows PowerShell에서 테스트할 때 한글이 깨지는 문제

`curl` 대신 Windows PowerShell의 `Invoke-RestMethod`로 `/chat`을 테스트하면 한글이
두 군데에서 깨질 수 있습니다 — 원인이 서로 달라서 둘 다 고쳐야 합니다.

**1) 보낸 질문 자체가 깨져서 서버에 도착하는 경우** (답변이 "질문이 깨져서
전달됐다"는 식으로 옴): `Invoke-RestMethod -Body <문자열>`이 body를 UTF-8이 아닌
다른 인코딩으로 보내는 게 원인입니다. body를 UTF-8 바이트로 직접 변환해서
넘기세요:
```powershell
$bodyJson = @{ message = "질문 내용" } | ConvertTo-Json
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyJson)

Invoke-RestMethod -Uri "http://localhost:8000/chat" -Method Post `
  -Headers @{ Authorization = "Bearer <SSO JWT>" } `
  -ContentType "application/json; charset=utf-8" `
  -Body $bodyBytes
```

**2) 요청/답변은 정상인데 콘솔에 출력할 때만 깨지는 경우**: PowerShell 콘솔의
출력 인코딩이 UTF-8이 아니라서 그렇습니다. 아래 세 줄을 PowerShell 프로필
(`notepad $PROFILE` — 파일/폴더가 없으면 `New-Item -ItemType Directory -Path
(Split-Path $PROFILE) -Force`로 폴더부터 만든 뒤 다시 시도)에 넣어두면 새
PowerShell 창을 열 때마다 자동 적용됩니다:
```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null
```
프로필 스크립트 자체가 실행 정책에 막혀 로드가 안 되면(`이 시스템에서 스크립트를
실행할 수 없으므로...` 에러), `Set-ExecutionPolicy -Scope CurrentUser
-ExecutionPolicy RemoteSigned`로 사용자 범위 정책을 한 번 바꿔두세요(관리자 권한
불필요, 이후 세션에 계속 적용됨).

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VECTOR_STORE_BACKEND` | `memory` | `memory` \| `chroma` |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | Chroma 영속 경로 |
| `GRAPH_STORE_BACKEND` | `memory` | `memory` \| `kuzu` |
| `KUZU_DB_PATH` | `./data/graph.kuzu` | Kuzu DB 파일 경로 |
| `EMBEDDER_BACKEND` | `hash` | `hash`(더미) \| `voyage` \| `gemini` |
| `VOYAGE_API_KEY` | - | `EMBEDDER_BACKEND=voyage`일 때 필수 |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | `EMBEDDER_BACKEND=gemini`일 때 사용할 모델 |
| `GEMINI_EMBED_DIMENSION` | `768` | `EMBEDDER_BACKEND=gemini`일 때 Matryoshka 축소 차원(기본 3072보다 작게) |
| `GEMINI_EMBED_BATCH_SIZE` | `10` | 한 번의 임베딩 API 요청에 몰아 보낼 문서 수 상한 (무료 등급 한도 초과 방지) |
| `LLM_BACKEND` | `anthropic` | `anthropic` \| `gemini` — `/chat`이 실제로 호출할 LLM 벤더 |
| `ANTHROPIC_API_KEY` | - | `LLM_BACKEND=anthropic`일 때 필수 |
| `GEMINI_API_KEY` | - | `LLM_BACKEND=gemini` 또는 `EMBEDDER_BACKEND=gemini`일 때 필수 (Google AI Studio에서 무료로 발급) |
| `GEMINI_MODEL` | `gemini-3.6-flash` | `LLM_BACKEND=gemini`일 때 사용할 모델. Gemini 쪽은 모델이 자주 폐기/교체되니, `404 ... no longer available` 에러가 나면 [AI Studio](https://aistudio.google.com) 모델 목록에서 현재 쓸 수 있는 이름으로 갱신하세요 |
| `EMBED_CACHE_PATH` | `./data/embed_cache.json` | 문서 내용이 안 바뀌면 재색인 때 임베딩 API 재호출을 건너뛰는 캐시 경로. 빈 값이면 매번 재임베딩 |
| `AUDIT_LOG_PATH` | `./data/audit.jsonl` | 감사로그 경로 |
| `SSO_JWT_ALGORITHM` | - | `HS256` \| `RS256`. 미설정 시 서버는 모든 `/chat` 요청을 501로 거부(fail-closed) |
| `SSO_JWT_SECRET` | - | `HS256`일 때 필수 |
| `SSO_JWT_JWKS_URL` | - | `RS256`일 때 필수 |
| `SSO_JWT_AUDIENCE` / `SSO_JWT_ISSUER` | - | 선택 |
| `REGULATION_DOCS_DIR` | `./data/raw/regulation` | 사규 원문 스테이징 디렉터리 |
| `REVIEW_DOCS_DIR` | `./data/raw/review` | 검토서 원문 스테이징 디렉터리 (부서별 하위 폴더로 RBAC 구분) |
| `FAQ_DOCS_DIR` | `./data/raw/faq` | FAQ 원문 스테이징 디렉터리 |
| `LAW_CRAWLER` / `INTERPRETATION_CRAWLER` / `CASE_CRAWLER` | - | `모듈:함수` 형태의 실 크롤러 콜백. 미설정 시 해당 소스는 재색인에서 제외 |
| `LAW_CRAWL_STATE_PATH` | `./data/law_crawl_state.json` | `crawl_watchlist_items_incremental` 사용 시, 법령별 마지막 공포일자/파싱 결과 캐시 경로 |
| `SYNC_INTERVAL_SECONDS` | `1800` | 주기 재색인 간격(초). `0`이면 시작 시 1회만 수행 |
| `SYNC_STATE_PATH` | `./data/sync_state.json` | 삭제 감지용 소스별 마지막 id 목록 저장 경로 |

## 남은 작업 (TODO)

1. **법령/유권해석/제재사례 실 크롤러 작성** — `LawConnector`/`InterpretationConnector`/
   `CaseConnector`는 이제 `fetch_items` 콜백만 주입하면 실제로 동작하지만(스텁 아님),
   그 콜백 자체(law.go.kr Open API 호출, 금융위/금감원 질의회신·제재정보 크롤링)는
   여전히 별도로 작성해야 합니다. 국가법령정보센터 OC 인증키 발급도 필요합니다.
   사내 EDMS(규정/검토서/FAQ) 접근 권한이 확보되면, 지금의 로컬 파일 스테이징
   커넥터를 EDMS 직접 연동 커넥터로 교체하면 됩니다.
2. **Voyage 실 임베딩 검증 및 재보정** — `HashEmbedder`는 파이프라인 검증용 더미(문자
   n-gram 해싱)이며 의미 유사도를 반영하지 않습니다. `VOYAGE_API_KEY` 발급 후
   `VoyageEmbedder`로 교체하고, `knowledge/vector_store.py`의 `DEFAULT_MIN_SCORE`를 실
   임베딩의 점수 분포에 맞게 재보정해야 합니다.
3. **실 사내 IdP 연동** — `agent/sso.py`는 HS256/RS256+JWKS 검증 로직을 갖추고 있으나
   실제 사내 IdP(RS256/JWKS)를 대상으로 한 라이브 검증은 아직 수행하지 않았습니다.
4. **평가셋 확장** — 현재 시드 데이터는 마스터 기획서 시나리오 1건(고령투자자 랩상품)
   수준입니다. 실데이터 기반 평가셋으로 확장이 필요합니다.
