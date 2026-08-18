"""검색 랭킹 진단용 1회성 스크립트.

두 가지를 한 번에 보여준다:
  [1] 실제 /chat과 동일한 조건 -- 지정한 top_k(기본 6, 내부 회수폭은
      top_k * _RECALL_WIDTH_MULTIPLIER)로 retrieve()를 호출한 최종 결과.
      LLM이 search_knowledge를 이 top_k로 호출했다면 정확히 이걸 받는다.
      retrieve() 내부에서 _stratify_by_source()가 "flat top_k 컷"과
      "소스(entity_type)별 상위 _MIN_PER_SOURCE_TYPE개"의 합집합을 만들기
      때문에, 최종 문서 수는 top_k보다 많을 수 있다(등장하는 소스 타입
      수에 비례).
  [2] 회수 폭과 무관한 채널 내 진짜 전체 순위 -- _recall_candidates()를
      사실상 무제한에 가까운 폭으로 직접 호출해, BM25/벡터 채널에서 각
      문서가 원래 몇 등이었는지를 보여준다. [1]에 법령이 없었을 때, 이걸로
      "법령이 애초에 회수 폭(recall width) 밖이라 놓쳤는지" 정확히 알 수
      있다 -- 소스별 최소 보장 덕분에, 회수 폭 안에만 들어오면 최종 top_k
      컷에서 밀려나는 일은 이제 없다.

주의: 여기 찍히는 "점수"는 기본 리랭커(NoOpReranker)가 매기는 값인데,
이건 실제 유사도가 아니라 "이미 RRF로 정렬된 후보 목록에서 몇 번째였는지"를
숫자로 바꾼 것뿐이다(knowledge/reranker.py의 NoOpReranker.rerank() 참고).
순위(등수) 자체는 정확하지만 점수 크기를 관련성 강도로 해석하면 안 된다.

사용법:
    python debug_retrieval.py "질의문" [dept] [top_k]
"""

from __future__ import annotations

import sys
from datetime import date

from bootstrap import build_components
from knowledge.retriever import _RECALL_WIDTH_MULTIPLIER
from ontology.schema import authority_rank

QUERY = sys.argv[1] if len(sys.argv) > 1 else "자금세탁방지 보고책임자의 직급이 차장인데 문제 없을까?"
DEPT = sys.argv[2] if len(sys.argv) > 2 else "compliance"
REAL_TOP_K = int(sys.argv[3]) if len(sys.argv) > 3 else 6  # 실제 /chat 기본값

components = build_components()

# VECTOR_STORE_BACKEND=memory / GRAPH_STORE_BACKEND=memory(기본값)에서는
# build_components()가 만드는 저장소가 이 프로세스 메모리 안에서만 존재한다.
# 이미 떠 있는 uvicorn 서버 프로세스와는 메모리를 전혀 공유하지 않으므로,
# api/main.py의 lifespan()이 기동 시 하는 것과 동일하게 여기서도 한 번
# sync_once()를 직접 돌려 문서를 채워야 한다 -- 이걸 빠뜨리면 완전히 빈
# 저장소를 대상으로 검색해 항상 0건이 나온다.
print("색인 동기화 중 (law.go.kr 크롤링 포함, 시간이 걸릴 수 있습니다)...")
report = components.syncer.sync_once()
for r in report.results:
    print(f"  [{r.name}] ingested={r.ingested} removed={r.removed} errors={r.errors}")
print()

retriever = components.retriever


def _print_results(title: str, docs: list, note: str) -> None:
    print(title)
    print("-" * 100)
    print(f"{'순위':<4} {'id':<45} {'권위':<4} {'경로(reason)'}")
    print("-" * 100)
    for i, doc in enumerate(docs, start=1):
        print(f"{i:<4} {doc.entity.id:<45} {authority_rank(doc.entity.type):<4} {doc.reason}")
    law_hits = [d for d in docs if d.entity.id.startswith("law:")]
    print("-" * 100)
    if not law_hits:
        print("law: 접두사 id가 여기 없음")
    else:
        for d in law_hits:
            print(f"법령 -- {docs.index(d) + 1}등 -- {d.entity.id} ({d.reason})")
    print(note)
    print()


# [1] 실제 /chat 조건 재현
real_results = retriever.retrieve(QUERY, dept=DEPT, top_k=REAL_TOP_K)
_print_results(
    f"[1] 실제 /chat 조건 재현: top_k={REAL_TOP_K} -- LLM이 이 top_k로 검색했다면 정확히 이 결과를 받음",
    real_results,
    "",
)

# [2] 채널 내 진짜 전체 순위 -- 회수 폭을 사실상 무제한(9999)으로 걸어
# _recall_candidates()를 직접 호출한다. 최종 top_k 컷/리랭킹을 거치지
# 않은, BM25+벡터 RRF 융합 직후의 순수 순위다.
full_pool = retriever._recall_candidates(QUERY, DEPT, date.today(), 9999, None, set())
_print_results(
    f"[2] 채널 내 진짜 전체 순위 (회수폭 사실상 무제한, 총 {len(full_pool)}건)",
    full_pool,
    "",
)

# 진단: [1]에 법령이 없는데 [2]에는 있다면, 원인은 이제 둘 중 하나뿐이다 --
# (a) 회수 폭(recall_k = top_k * _RECALL_WIDTH_MULTIPLIER) 밖이라 애초에
#     _recall_candidates()가 못 건져온 경우, 또는
# (b) 회수 폭 안에는 들어왔는데도 [1]에 없는 경우 -- 이건 _stratify_by_source()가
#     "소스별 상위 _MIN_PER_SOURCE_TYPE개는 무조건 포함"하도록 되어 있으므로
#     정상적으로는 발생하지 않아야 하는 상황이다(발생하면 스크립트 인자로 준
#     entity_types와 실제 /chat 호출의 entity_types가 다르거나, 회귀 버그다).
real_has_law = any(d.entity.id.startswith("law:") for d in real_results)
law_in_full = [d for d in full_pool if d.entity.id.startswith("law:")]
recall_k = REAL_TOP_K * _RECALL_WIDTH_MULTIPLIER
if not real_has_law and law_in_full:
    true_rank = full_pool.index(law_in_full[0]) + 1
    if true_rank > recall_k:
        needed_multiplier = -(-true_rank // REAL_TOP_K)  # 올림 나눗셈
        print(
            f"진단: 법령 최고 순위는 채널 융합 기준 {true_rank}등인데, "
            f"top_k={REAL_TOP_K}의 회수 폭은 {recall_k}등까지만 봅니다. "
            f"_RECALL_WIDTH_MULTIPLIER가 최소 {needed_multiplier} 이상이어야 "
            f"이 문서가 회수 후보 풀에 듭니다(knowledge/retriever.py)."
        )
    else:
        print(
            f"진단: 법령(채널 융합 기준 {true_rank}등)이 회수 폭({recall_k}등) "
            "안에는 들어왔는데도 [1] 최종 결과에는 없습니다 -- 정상 동작이라면 "
            "_stratify_by_source()가 소스별 최소 보장으로 반드시 포함시켜야 "
            "하는 상황입니다. entity_types 필터가 다르게 걸렸는지, 혹은 회귀"
            "버그인지 knowledge/retriever.py를 확인하세요."
        )
elif real_has_law:
    print("진단: 법령이 이미 [1] 실사용 조건에서도 회수됨 -- 회수 폭/최종 컷 문제 아님.")
else:
    print("진단: [2] 무제한 회수에서도 법령이 전혀 안 잡힘 -- 회수 폭 문제가 아니라 이 쿼리 표현 자체가 법령 원문과 어휘/의미적으로 거리가 먼 것.")
