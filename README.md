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

로컬 개발 서버 (in-memory 백엔드, SSO는 반드시 설정해야 요청이 열립니다):

```bash
export SSO_JWT_ALGORITHM=HS256
export SSO_JWT_SECRET="로컬-테스트용-시크릿-32바이트-이상"
uvicorn api.main:app --reload
```

시드 데이터(고령투자자 랩상품 시나리오)를 채우려면:

```python
from bootstrap import build_components
from pipeline.ingest import IngestPipeline
from seed_data.seed import seed_all

components = build_components()
pipeline = IngestPipeline(components.embedder, components.vector_store, components.graph_store)
seed_all(pipeline)
```

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VECTOR_STORE_BACKEND` | `memory` | `memory` \| `chroma` |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | Chroma 영속 경로 |
| `GRAPH_STORE_BACKEND` | `memory` | `memory` \| `kuzu` |
| `KUZU_DB_PATH` | `./data/graph.kuzu` | Kuzu DB 파일 경로 |
| `EMBEDDER_BACKEND` | `hash` | `hash`(더미) \| `voyage` |
| `VOYAGE_API_KEY` | - | `EMBEDDER_BACKEND=voyage`일 때 필수 |
| `AUDIT_LOG_PATH` | `./data/audit.jsonl` | 감사로그 경로 |
| `SSO_JWT_ALGORITHM` | - | `HS256` \| `RS256`. 미설정 시 서버는 모든 `/chat` 요청을 501로 거부(fail-closed) |
| `SSO_JWT_SECRET` | - | `HS256`일 때 필수 |
| `SSO_JWT_JWKS_URL` | - | `RS256`일 때 필수 |
| `SSO_JWT_AUDIENCE` / `SSO_JWT_ISSUER` | - | 선택 |

## 남은 작업 (TODO)

1. **6대 소스 실 크롤러 연동** — 현재 `pipeline/connectors/*`는 전부 스텁이며 dev-mode로
   주입한 문서만 반환합니다. 국가법령정보센터 OC 인증키 발급, 금융위/금감원 질의회신·
   제재정보 접근 방식 확정, 사내 EDMS(규정/검토서) 접근 권한이 필요합니다.
2. **Voyage 실 임베딩 검증 및 재보정** — `HashEmbedder`는 파이프라인 검증용 더미(문자
   n-gram 해싱)이며 의미 유사도를 반영하지 않습니다. `VOYAGE_API_KEY` 발급 후
   `VoyageEmbedder`로 교체하고, `knowledge/vector_store.py`의 `DEFAULT_MIN_SCORE`를 실
   임베딩의 점수 분포에 맞게 재보정해야 합니다.
3. **실 사내 IdP 연동** — `agent/sso.py`는 HS256/RS256+JWKS 검증 로직을 갖추고 있으나
   실제 사내 IdP(RS256/JWKS)를 대상으로 한 라이브 검증은 아직 수행하지 않았습니다.
4. **평가셋 확장** — 현재 시드 데이터는 마스터 기획서 시나리오 1건(고령투자자 랩상품)
   수준입니다. 실데이터 기반 평가셋으로 확장이 필요합니다.
