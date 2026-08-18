"""검색 랭킹 진단용 1회성 스크립트.

/chat은 LLM이 top_k=6(기본값)로 search_knowledge를 호출한 결과만 보여주므로,
법령이 "아예 후보에 없었는지" vs "후보엔 있었는데 6등 밖으로 밀렸는지"를
구분할 수 없다. 이 스크립트는 retriever.retrieve()를 top_k를 크게 줘서 직접
호출해, 실제 후보 풀 전체와 순위·점수·매칭 경로(reason)를 보여준다.

사용법:
    python debug_retrieval.py "자금세탁방지 보고책임자의 직급이 차장인데 문제 없을까?"
"""

from __future__ import annotations

import sys

from bootstrap import build_components
from ontology.schema import authority_rank

QUERY = sys.argv[1] if len(sys.argv) > 1 else "자금세탁방지 보고책임자의 직급이 차장인데 문제 없을까?"
DEPT = sys.argv[2] if len(sys.argv) > 2 else "compliance"
TOP_K = 30  # 실제 /chat 기본값(6)보다 훨씬 넉넉하게 -- 6등 밖에서 뭐가 있었는지 보려고

components = build_components()
results = components.retriever.retrieve(QUERY, dept=DEPT, top_k=TOP_K)

print(f"질의: {QUERY!r}  (dept={DEPT}, top_k={TOP_K})")
print(f"반환된 후보 수: {len(results)}")
print("-" * 100)
print(f"{'순위':<4} {'id':<45} {'권위':<4} {'점수':<10} {'경로(reason)'}")
print("-" * 100)
for i, doc in enumerate(results, start=1):
    print(
        f"{i:<4} {doc.entity.id:<45} {authority_rank(doc.entity.type):<4} "
        f"{doc.score:<10.4f} {doc.reason}"
    )

law_hits = [d for d in results if d.entity.id.startswith("law:")]
print("-" * 100)
if not law_hits:
    print(f"law: 접두사 id는 top_k={TOP_K} 안에도 전혀 없음 -- 이 질의로는 벡터/BM25가 법령을 아예 못 끌어옴.")
else:
    for d in law_hits:
        rank = results.index(d) + 1
        print(f"법령 최고 순위: {rank}등 -- {d.entity.id} (점수 {d.score:.4f}, {d.reason})")
    print(f"(참고: 실제 /chat 기본 top_k는 6이므로, 6등 밖이면 이번 답변에는 안 실렸을 것)")
