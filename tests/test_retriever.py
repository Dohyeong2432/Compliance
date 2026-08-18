from datetime import date

import pytest

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.lexical import LexicalIndex
from knowledge.reranker import RerankedItem, Reranker
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore, VectorRecord
from ontology.schema import Entity, EntityType, Relation, RelationType

EMB = HashEmbedder()


def _seed(graph_store, vector_store):
    law_old = Entity(
        "law:old", EntityType.LAW, "노인 보호 구법", "구법 본문 내용",
        effective_date=date(2020, 1, 1), superseded_date=date(2023, 1, 1),
    )
    law_new = Entity(
        "law:new", EntityType.LAW, "노인 보호 신법", "신법 본문 강화 내용",
        effective_date=date(2023, 1, 1),
    )
    case = Entity("case:1", EntityType.CASE, "제재사례 A", "노인 보호 위반 제재사례", allowed_depts=("ALL",))
    ib_review = Entity("review:1", EntityType.REVIEW, "IB 전용 검토서", "노인 보호 관련 IB 검토", allowed_depts=("IB",))

    for entity in (law_old, law_new, case, ib_review):
        graph_store.add_entity(entity)
        vector_store.upsert(
            [
                VectorRecord(
                    entity.id,
                    EMB.embed_one(entity.title + entity.body),
                    entity.body,
                    allowed_depts=entity.allowed_depts,
                    effective_date=entity.effective_date,
                    superseded_date=entity.superseded_date,
                )
            ]
        )

    graph_store.add_relation(Relation("law:new", RelationType.SUPERSEDES, "law:old"))
    graph_store.add_relation(Relation("case:1", RelationType.VIOLATES, "law:new"))

    # A vector hit with no backing graph entity at all -- must never surface.
    vector_store.upsert(
        [VectorRecord("ghost:1", EMB.embed_one("노인 보호 유령 문서 허위 정보"), "ghost text")]
    )

    return law_old, law_new, case, ib_review


@pytest.fixture
def retriever():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    _seed(graph_store, vector_store)
    return HybridRetriever(EMB, vector_store, graph_store), graph_store, vector_store


def test_ghost_vector_hit_never_surfaces(retriever):
    hr, _, _ = retriever
    docs = hr.retrieve("노인 보호 유령 문서", dept="RETAIL", as_of=date(2024, 1, 1))
    assert "ghost:1" not in {d.entity.id for d in docs}


def test_time_resolution_swaps_to_version_valid_at_as_of(retriever):
    hr, _, _ = retriever
    past = hr.retrieve("노인 보호 기준", dept="RETAIL", as_of=date(2021, 6, 1))
    present = hr.retrieve("노인 보호 기준", dept="RETAIL", as_of=date(2024, 6, 1))

    assert "law:old" in {d.entity.id for d in past}
    assert "law:new" not in {d.entity.id for d in past}
    assert "law:new" in {d.entity.id for d in present}
    assert "law:old" not in {d.entity.id for d in present}


def test_rbac_hides_ib_scoped_review_from_other_depts(retriever):
    hr, _, _ = retriever
    retail_docs = hr.retrieve("IB 검토서 노인 보호", dept="RETAIL", as_of=date(2024, 1, 1))
    ib_docs = hr.retrieve("IB 검토서 노인 보호", dept="IB", as_of=date(2024, 1, 1))

    assert "review:1" not in {d.entity.id for d in retail_docs}
    assert "review:1" in {d.entity.id for d in ib_docs}


def test_dept_is_required(retriever):
    hr, _, _ = retriever
    with pytest.raises(ValueError):
        hr.retrieve("아무 질의", dept="")


def test_expansion_pulls_in_related_case(retriever):
    hr, _, _ = retriever
    docs = hr.retrieve("노인 보호 신법 조항", dept="RETAIL", as_of=date(2024, 1, 1), top_k=1)
    ids = {d.entity.id for d in docs}
    assert "law:new" in ids
    assert "case:1" in ids


def test_citation_query_finds_article_that_vector_search_would_miss(retriever):
    """"제N조" 같은 정확한 조문 인용 질의는 그 자체로 의미 정보가 거의
    없어서 임베딩 유사도만으로는 상위권에 못 들 수 있다(실사용에서
    top_k=20까지 늘려도 재현됨) -- title 직접 매칭으로 찾아내야 한다.
    이걸 확인하려고 벡터는 질의와 전혀 무관한 텍스트로 임베딩해뒀다(순수
    벡터 검색이라면 top_k=1에서 절대 안 걸림)."""
    hr, graph_store, vector_store = retriever
    article = Entity("law:art5", EntityType.LAW, "노인 보호법 제5조(위임)", "이 조항은 세부사항을 대통령령에 위임한다.")
    graph_store.add_entity(article)
    vector_store.upsert(
        [VectorRecord("law:art5", EMB.embed_one("완전히 무관한 다른 주제의 텍스트"), article.body)]
    )

    docs = hr.retrieve("노인 보호법 제5조가 뭐야?", dept="RETAIL", as_of=date(2024, 1, 1), top_k=1)

    matched = next((d for d in docs if d.entity.id == "law:art5"), None)
    assert matched is not None
    assert matched.reason == "citation_match"


def test_citation_query_does_not_bypass_rbac(retriever):
    """조문 인용 직접 매칭도 다른 경로와 동일하게 RBAC를 통과해야 한다 --
    지름길이라고 부서 제한을 우회하면 안 된다."""
    hr, graph_store, vector_store = retriever
    restricted = Entity(
        "review:art9", EntityType.REVIEW, "제9조 관련 IB 전용 검토서", "IB 한정 내용", allowed_depts=("IB",)
    )
    graph_store.add_entity(restricted)
    vector_store.upsert([VectorRecord("review:art9", EMB.embed_one("무관한 텍스트"), restricted.body)])

    retail_docs = hr.retrieve("제9조가 뭐야?", dept="RETAIL", as_of=date(2024, 1, 1))
    ib_docs = hr.retrieve("제9조가 뭐야?", dept="IB", as_of=date(2024, 1, 1))

    assert "review:art9" not in {d.entity.id for d in retail_docs}
    assert "review:art9" in {d.entity.id for d in ib_docs}


def test_no_article_citation_in_query_skips_title_matching(retriever):
    """조문 번호 언급이 없는 일반 질의는 citation_match 경로를 아예 안
    타야 한다(불필요한 전체 title 스캔 방지)."""
    hr, _, _ = retriever
    docs = hr.retrieve("노인 보호 기준", dept="RETAIL", as_of=date(2024, 1, 1))
    assert all(d.reason != "citation_match" for d in docs)


class _SpyEmbedder(HashEmbedder):
    """embed_query()가 실제로 호출되는지 감시하되, 임베딩 결과 자체는
    HashEmbedder와 동일해야 vector_store에 미리 심어둔 벡터와 비교가
    맞는다(순수 결정론적 해시라 새 인스턴스여도 같은 텍스트는 같은 벡터)."""

    def __init__(self):
        super().__init__()
        self.embed_query_calls: list[str] = []
        self.embed_one_calls: list[str] = []

    def embed_query(self, text):
        self.embed_query_calls.append(text)
        return super().embed_query(text)

    def embed_one(self, text):
        self.embed_one_calls.append(text)
        return super().embed_one(text)


def test_retrieve_embeds_the_query_via_embed_query_not_embed_one():
    """Voyage/Gemini는 질의와 문서를 다르게 임베딩해야 하는 비대칭
    임베딩 API라, retriever가 질의를 embed_one()(문서용)이 아니라
    embed_query()(질의 전용)으로 넣어야 한다 -- 실사용에서 이걸 놓쳐서
    "업무위탁과 관련된 조항" 같은 순수 의미 검색이 제목에 정확히
    "업무위탁"이 들어간 조문조차 못 찾는 문제가 있었다."""
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    _seed(graph_store, vector_store)
    spy = _SpyEmbedder()
    hr = HybridRetriever(spy, vector_store, graph_store)

    hr.retrieve("노인 보호 기준", dept="RETAIL", as_of=date(2024, 1, 1))

    assert spy.embed_query_calls == ["노인 보호 기준"]


# ---------------------------------------------------------------------------
# 권위 위계 / BM25 어휘 채널 / 리랭커 / 소스 타입 필터
# ---------------------------------------------------------------------------


def _authority_fixture():
    """같은 주제를 다루는 법령·사내규정·검토서를 한 벌 심는다. 벡터 유사도는
    검토서가 가장 높도록(질의어를 그대로 포함) 만들어서, 관련성만으로 정렬하면
    검토서가 1등이 되는 상황을 의도적으로 구성한다."""
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    entities = [
        Entity("law:1", EntityType.LAW, "금융지주회사법 제47조", "자회사등 사이의 업무위탁"),
        Entity("regulation:1", EntityType.REGULATION, "업무위탁 운영지침", "업무위탁 내부 절차"),
        Entity("review:1", EntityType.REVIEW, "업무위탁 검토서", "업무위탁 관련 내부 검토의견"),
        Entity("faq:1", EntityType.FAQ, "업무위탁 FAQ", "업무위탁 자주 묻는 질문"),
    ]
    for entity in entities:
        graph_store.add_entity(entity)
        vector_store.upsert([VectorRecord(entity.id, EMB.embed_one(entity.title + entity.body), entity.body)])
    return graph_store, vector_store


def test_results_are_ordered_by_normative_authority_not_relevance_alone():
    """검색 관련성이 아무리 높아도 검토서가 법령보다 먼저 제시되면 안 된다 --
    준법 답변에서 내부 의견과 강행규범의 층위가 뒤섞이는 것은 랭킹 품질
    문제가 아니라 컴플라이언스 리스크다."""
    graph_store, vector_store = _authority_fixture()
    hr = HybridRetriever(EMB, vector_store, graph_store)

    docs = hr.retrieve("업무위탁", dept="RETAIL", as_of=date(2024, 1, 1), top_k=10)

    types = [d.entity.type for d in docs]
    assert types.index(EntityType.LAW) < types.index(EntityType.REGULATION)
    assert types.index(EntityType.REGULATION) < types.index(EntityType.REVIEW)
    assert types.index(EntityType.REVIEW) < types.index(EntityType.FAQ)


def test_entity_types_filter_restricts_results_to_requested_sources():
    graph_store, vector_store = _authority_fixture()
    hr = HybridRetriever(EMB, vector_store, graph_store)

    docs = hr.retrieve(
        "업무위탁", dept="RETAIL", as_of=date(2024, 1, 1), top_k=10, entity_types=(EntityType.LAW,)
    )

    assert {d.entity.id for d in docs} == {"law:1"}


def test_entity_types_filter_also_applies_to_citation_match_path():
    """조문 인용 직접 매칭도 소스 필터를 따라야 한다 -- 우회 경로가 되면
    "법령만 보여줘"가 지켜지지 않는다."""
    graph_store, vector_store = _authority_fixture()
    graph_store.add_entity(
        Entity("review:art47", EntityType.REVIEW, "제47조 관련 검토서", "검토의견 본문")
    )
    hr = HybridRetriever(EMB, vector_store, graph_store)

    docs = hr.retrieve(
        "제47조가 뭐야?", dept="RETAIL", as_of=date(2024, 1, 1), entity_types=(EntityType.LAW,)
    )

    assert "review:art47" not in {d.entity.id for d in docs}


def test_lexical_channel_surfaces_document_vector_search_would_miss():
    """정형 용어 정확 일치는 BM25의 몫이다 -- 벡터는 질의와 무관한 텍스트로
    임베딩해 두어, 어휘 채널이 없으면 절대 안 걸리는 상황을 만든다."""
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    entity = Entity("law:1", EntityType.LAW, "이해상충 방지", "이해상충 방지체계에 관한 조항")
    graph_store.add_entity(entity)
    vector_store.upsert([VectorRecord(entity.id, EMB.embed_one("완전히 무관한 다른 주제"), entity.body)])

    lexical_index = LexicalIndex()
    lexical_index.index(entity.id, f"{entity.title}\n{entity.body}")
    hr = HybridRetriever(EMB, vector_store, graph_store, lexical_index=lexical_index)

    docs = hr.retrieve("이해상충", dept="RETAIL", as_of=date(2024, 1, 1), top_k=3)

    matched = next((d for d in docs if d.entity.id == "law:1"), None)
    assert matched is not None
    assert matched.reason == "lexical_match"


def test_lexical_hits_still_pass_rbac():
    """어휘 검색도 다른 경로와 동일하게 RBAC를 통과해야 한다 -- 새 채널이
    권한 우회로가 되면 안 된다."""
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    entity = Entity("review:1", EntityType.REVIEW, "이해상충 IB 검토서", "IB 한정 내용", allowed_depts=("IB",))
    graph_store.add_entity(entity)
    vector_store.upsert([VectorRecord(entity.id, EMB.embed_one("무관"), entity.body, allowed_depts=("IB",))])

    lexical_index = LexicalIndex()
    lexical_index.index(entity.id, f"{entity.title}\n{entity.body}")
    hr = HybridRetriever(EMB, vector_store, graph_store, lexical_index=lexical_index)

    retail_docs = hr.retrieve("이해상충", dept="RETAIL", as_of=date(2024, 1, 1))
    ib_docs = hr.retrieve("이해상충", dept="IB", as_of=date(2024, 1, 1))

    assert "review:1" not in {d.entity.id for d in retail_docs}
    assert "review:1" in {d.entity.id for d in ib_docs}


class _SpyReranker(Reranker):
    """리랭커가 실제로 후보 풀을 받아 순서를 결정하는지 확인하기 위해,
    입력 순서를 뒤집어 돌려준다."""

    def __init__(self):
        self.calls: list[tuple[str, list[str], int]] = []

    def rerank(self, query, documents, top_n):
        self.calls.append((query, list(documents), top_n))
        indices = list(range(len(documents)))[::-1][:top_n]
        return [RerankedItem(idx, float(len(indices) - rank)) for rank, idx in enumerate(indices)]


def test_reranker_decides_final_order_of_recall_candidates():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    for i in (1, 2):
        entity = Entity(f"law:{i}", EntityType.LAW, f"업무위탁 조항 {i}", f"업무위탁 본문 {i}")
        graph_store.add_entity(entity)
        vector_store.upsert([VectorRecord(entity.id, EMB.embed_one(entity.title + entity.body), entity.body)])

    spy = _SpyReranker()
    hr = HybridRetriever(EMB, vector_store, graph_store, reranker=spy)
    baseline = HybridRetriever(EMB, vector_store, graph_store)

    reranked_ids = [d.entity.id for d in hr.retrieve("업무위탁", dept="RETAIL", as_of=date(2024, 1, 1), top_k=2)]
    baseline_ids = [d.entity.id for d in baseline.retrieve("업무위탁", dept="RETAIL", as_of=date(2024, 1, 1), top_k=2)]

    assert spy.calls, "리랭커가 호출되지 않았다"
    assert reranked_ids == baseline_ids[::-1]


def test_reranker_receives_title_and_body_of_candidates():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    entity = Entity("law:1", EntityType.LAW, "업무위탁 조항", "업무위탁 본문")
    graph_store.add_entity(entity)
    vector_store.upsert([VectorRecord(entity.id, EMB.embed_one(entity.title + entity.body), entity.body)])

    spy = _SpyReranker()
    HybridRetriever(EMB, vector_store, graph_store, reranker=spy).retrieve(
        "업무위탁", dept="RETAIL", as_of=date(2024, 1, 1)
    )

    _query, documents, _top_n = spy.calls[0]
    assert documents == ["업무위탁 조항\n업무위탁 본문"]


def test_citation_match_is_never_dropped_by_the_reranker():
    """사용자가 조문 번호를 특정해 물었으면 그 조문은 리랭커가 어떤 점수를
    주든 답변 근거에 남아야 한다 -- 리랭킹 대상 풀에서 아예 제외된다."""

    class DropEverythingReranker(Reranker):
        def rerank(self, query, documents, top_n):
            return []

    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    entity = Entity("law:art47", EntityType.LAW, "금융지주회사법 제47조(업무위탁)", "본문")
    graph_store.add_entity(entity)
    vector_store.upsert([VectorRecord(entity.id, EMB.embed_one("무관한 텍스트"), entity.body)])

    hr = HybridRetriever(EMB, vector_store, graph_store, reranker=DropEverythingReranker())

    docs = hr.retrieve("제47조가 뭐야?", dept="RETAIL", as_of=date(2024, 1, 1))

    assert [d.entity.id for d in docs] == ["law:art47"]
    assert docs[0].reason == "citation_match"


# ---------------------------------------------------------------------------
# 회수(recall) 폭 -- 채널 내 순위가 top_k보다 낮은 문서도 융합 후보에 들도록
# top_k 자체가 아니라 top_k * 5를 각 채널 회수 폭으로 써야 한다.
# ---------------------------------------------------------------------------


class _SpyLexicalIndex(LexicalIndex):
    """실제 검색은 그대로 위임하되, 호출받은 top_k 인자를 기록한다."""

    def __init__(self):
        super().__init__()
        self.called_top_k: list[int] = []

    def search(self, query, top_k=10, entity_types=None):
        self.called_top_k.append(top_k)
        return super().search(query, top_k=top_k, entity_types=entity_types)


class _SpyVectorStore(InMemoryVectorStore):
    def __init__(self):
        super().__init__()
        self.called_top_k: list[int] = []

    def search(self, query_vector, top_k=10, dept=None, as_of=None, entity_types=None):
        self.called_top_k.append(top_k)
        return super().search(query_vector, top_k=top_k, dept=dept, as_of=as_of, entity_types=entity_types)


def test_recall_width_is_wider_than_final_top_k():
    """top_k=6으로 검색해도 각 채널(BM25/벡터)에는 top_k*5=30을 요청해야
    한다 -- 그래야 채널 안에서 6등 밖인 문서도 융합 후보 풀에는 남아,
    RRF로 합산했을 때 최종 상위권으로 올라올 기회를 가진다. 실사용에서
    이 폭이 top_k와 동일했을 때, 법령이 채널 내 순위가 낮다는 이유만으로
    (다른 소스와 경쟁해 지기도 전에) 후보 풀에서 원천 배제되는 문제가
    확인됐다."""
    graph_store = NetworkXGraphStore()
    lexical_spy = _SpyLexicalIndex()
    vector_spy = _SpyVectorStore()
    entity = Entity("law:1", EntityType.LAW, "테스트 법령", "테스트 본문")
    graph_store.add_entity(entity)
    lexical_spy.index(entity.id, entity.title + "\n" + entity.body)
    vector_spy.upsert([VectorRecord(entity.id, EMB.embed_one(entity.title + entity.body), entity.body)])

    hr = HybridRetriever(EMB, vector_spy, graph_store, lexical_index=lexical_spy)
    hr.retrieve("테스트", dept="RETAIL", as_of=date(2024, 1, 1), top_k=6)

    assert lexical_spy.called_top_k == [30]
    assert vector_spy.called_top_k == [30]


def test_recall_width_scales_with_requested_top_k():
    graph_store = NetworkXGraphStore()
    lexical_spy = _SpyLexicalIndex()
    vector_spy = _SpyVectorStore()

    hr = HybridRetriever(EMB, vector_spy, graph_store, lexical_index=lexical_spy)
    hr.retrieve("테스트", dept="RETAIL", as_of=date(2024, 1, 1), top_k=2)

    assert lexical_spy.called_top_k == [10]
    assert vector_spy.called_top_k == [10]
