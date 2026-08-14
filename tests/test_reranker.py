import pytest

from knowledge.reranker import NoOpReranker, RerankedItem, VoyageReranker


def test_noop_reranker_preserves_input_order():
    """리랭커를 끈 배포에서 검색 순서가 바뀌면 안 된다 -- NoOp은 자르기만 한다."""
    reranker = NoOpReranker()
    items = reranker.rerank("질의", ["a", "b", "c"], top_n=3)
    assert [item.index for item in items] == [0, 1, 2]


def test_noop_reranker_truncates_to_top_n():
    items = NoOpReranker().rerank("질의", ["a", "b", "c", "d"], top_n=2)
    assert [item.index for item in items] == [0, 1]


def test_noop_reranker_scores_descend_with_position():
    """점수 자체는 보정된 관련성이 아니지만, 상위일수록 커야 retriever의
    2차 정렬(권위 → 점수)에서 순서가 뒤집히지 않는다."""
    items = NoOpReranker().rerank("질의", ["a", "b", "c"], top_n=3)
    scores = [item.score for item in items]
    assert scores == sorted(scores, reverse=True)


def test_noop_reranker_handles_empty_documents():
    assert NoOpReranker().rerank("질의", [], top_n=5) == []


def test_voyage_reranker_requires_api_key(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        VoyageReranker(api_key=None)


def test_voyage_reranker_parses_dict_response_sorted_by_relevance():
    response = {
        "results": [
            {"index": 2, "relevance_score": 0.11},
            {"index": 0, "relevance_score": 0.93},
            {"index": 1, "relevance_score": 0.55},
        ]
    }

    items = VoyageReranker._parse_response(response)

    assert [item.index for item in items] == [0, 1, 2]
    assert items[0].score == 0.93


def test_voyage_reranker_parses_sdk_style_response_objects():
    from types import SimpleNamespace

    response = SimpleNamespace(
        results=[
            SimpleNamespace(index=1, relevance_score=0.2),
            SimpleNamespace(index=0, relevance_score=0.8),
        ]
    )

    items = VoyageReranker._parse_response(response)

    assert items == [RerankedItem(0, 0.8), RerankedItem(1, 0.2)]


def test_voyage_reranker_raises_on_malformed_response():
    with pytest.raises(ValueError):
        VoyageReranker._parse_response({})


def test_voyage_reranker_sends_query_and_documents_without_network():
    class FakeClient:
        def __init__(self):
            self.calls: list[dict] = []

        def rerank(self, **kwargs):
            self.calls.append(kwargs)
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    reranker = VoyageReranker.__new__(VoyageReranker)
    reranker.model = "rerank-2.5"
    reranker._client = FakeClient()

    items = reranker.rerank("업무위탁", ["문서1", "문서2", "문서3"], top_n=2)

    assert items == [RerankedItem(0, 0.9)]
    call = reranker._client.calls[0]
    assert call["query"] == "업무위탁"
    assert call["documents"] == ["문서1", "문서2", "문서3"]
    assert call["top_k"] == 2  # top_n을 문서 수 이하로 클램프해 전달


def test_voyage_reranker_clamps_top_n_to_document_count():
    class FakeClient:
        def __init__(self):
            self.calls: list[dict] = []

        def rerank(self, **kwargs):
            self.calls.append(kwargs)
            return {"results": []}

    reranker = VoyageReranker.__new__(VoyageReranker)
    reranker.model = "rerank-2.5"
    reranker._client = FakeClient()

    reranker.rerank("질의", ["문서1"], top_n=10)

    assert reranker._client.calls[0]["top_k"] == 1


def test_voyage_reranker_skips_api_call_for_empty_documents():
    class ExplodingClient:
        def rerank(self, **kwargs):
            raise AssertionError("빈 후보에는 API를 호출하면 안 된다")

    reranker = VoyageReranker.__new__(VoyageReranker)
    reranker.model = "rerank-2.5"
    reranker._client = ExplodingClient()

    assert reranker.rerank("질의", [], top_n=5) == []
