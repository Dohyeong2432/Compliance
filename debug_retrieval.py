"""검색 랭킹 진단용 1회성 스크립트.

두 가지를 한 번에 보여준다:
  [1] 실제 /chat과 동일한 조건 -- 지정한 top_k(기본 6, 내부 회수폭은
      top_k * _RECALL_WIDTH_MULTIPLIER)로 retrieve()를 호출한 최종 결과.
      LLM이 search_knowledge를 이 top_k로 호출했다면 정확히 이걸 받는다.
  [2] 회수 폭과 무관한 채널 내 진짜 전체 순위 -- _recall_candidates()를
      사실상 무제한에 가까운 폭으로 직접 호출해, BM25/벡터 채널에서 각
      문서가 원래 몇 등이었는지를 보여준다. [1]에 법령이 없었을 때, 이걸로
      "법령이 몇 등이라 회수 폭이 부족했는지" 정확히 알 수 있다.

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

# 진단: [1]에 법령이 없는데 [2]에는 있다면, 정확히 몇 등이라 놓쳤는지와
# 회수 배율을 얼마로 올려야 하는지 계산해서 알려준다.
real_has_law = any(d.entity.id.startswith("law:") for d in real_results)
law_in_full = [d for d in full_pool if d.entity.id.startswith("law:")]
if not real_has_law and law_in_full:
    true_rank = full_pool.index(law_in_full[0]) + 1
    needed_multiplier = -(-true_rank // REAL_TOP_K)  # 올림 나눗셈
    print(
        f"진단: 법령 최고 순위는 채널 융합 기준 {true_rank}등입니다. "
        f"top_k={REAL_TOP_K}에서 이 문서를 회수하려면 "
        f"_RECALL_WIDTH_MULTIPLIER가 최소 {needed_multiplier} 이상이어야 합니다 "
        f"(knowledge/retriever.py의 현재 값과 비교해보세요)."
    )
elif real_has_law:
    print("진단: 법령이 이미 [1] 실사용 조건에서도 회수됨 -- 회수 폭 문제 아님.")
else:
    print("진단: [2] 무제한 회수에서도 법령이 전혀 안 잡힘 -- 회수 폭 문제가 아니라 이 쿼리 표현 자체가 법령 원문과 어휘/의미적으로 거리가 먼 것.")
