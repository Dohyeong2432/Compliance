from datetime import date

import pytest

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
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
