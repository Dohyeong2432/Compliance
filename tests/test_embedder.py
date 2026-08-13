import math

import pytest

from knowledge.embedder import GeminiEmbedder, HashEmbedder, VoyageEmbedder


def test_hash_embedder_is_deterministic():
    embedder = HashEmbedder(dimension=64)
    v1 = embedder.embed_one("고령투자자 보호 기준")
    v2 = embedder.embed_one("고령투자자 보호 기준")
    assert v1 == v2


def test_hash_embedder_different_text_different_vector():
    embedder = HashEmbedder(dimension=64)
    v1 = embedder.embed_one("고령투자자 보호 기준")
    v2 = embedder.embed_one("전산실 출입 통제 규정")
    assert v1 != v2


def test_hash_embedder_dimension_and_normalization():
    embedder = HashEmbedder(dimension=128)
    vec = embedder.embed_one("아무 텍스트")
    assert len(vec) == 128
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-9


def test_hash_embedder_empty_string_does_not_crash():
    embedder = HashEmbedder(dimension=32)
    vec = embedder.embed_one("")
    assert len(vec) == 32


def test_voyage_embedder_requires_api_key(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        VoyageEmbedder(api_key=None)


def test_voyage_embedder_request_and_response_parsing_without_network(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key-for-test")

    embedder = VoyageEmbedder.__new__(VoyageEmbedder)
    embedder.api_key = "fake-key-for-test"
    embedder.model = "voyage-3"
    embedder.dimension = 4
    embedder.input_type = "document"

    request = embedder._build_request(["a", "b"])
    assert request == {"texts": ["a", "b"], "model": "voyage-3", "input_type": "document"}

    parsed = embedder._parse_response({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    assert parsed == [[0.1, 0.2], [0.3, 0.4]]

    with pytest.raises(ValueError):
        embedder._parse_response({})


def test_gemini_embedder_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        GeminiEmbedder(api_key=None)


def test_gemini_embedder_request_and_response_parsing_without_network(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    embedder = GeminiEmbedder.__new__(GeminiEmbedder)
    embedder.api_key = "fake-key-for-test"
    embedder.model = "gemini-embedding-001"
    embedder.dimension = 4
    embedder.task_type = "RETRIEVAL_DOCUMENT"

    request = embedder._build_request(["a", "b"])
    assert request == {
        "model": "gemini-embedding-001",
        "contents": ["a", "b"],
        "config": {"task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": 4},
    }

    # dict-shaped response (as used in tests / a plain JSON reply)
    parsed = embedder._parse_response({"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3, 0.4]}]})
    assert parsed == [[0.1, 0.2], [0.3, 0.4]]

    with pytest.raises(ValueError):
        embedder._parse_response({})


def test_gemini_embedder_parses_sdk_style_response_objects(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    embedder = GeminiEmbedder.__new__(GeminiEmbedder)

    response = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.5, 0.6])])
    parsed = embedder._parse_response(response)
    assert parsed == [[0.5, 0.6]]


def test_gemini_embedder_splits_large_input_into_batches():
    """대량 문서를 한 번에 embed()에 넘겨도 batch_size 단위로 쪼개서 여러 번
    호출해야 한다 -- 한 번의 요청이 너무 크면(대량의 사규 문서 등) 무료
    등급 할당량을 하나의 요청만으로 다 써버릴 수 있다."""

    class FakeModels:
        def __init__(self):
            self.calls: list[list[str]] = []

        def embed_content(self, model, contents, config):
            self.calls.append(list(contents))
            return {"embeddings": [{"values": [float(len(t))]} for t in contents]}

    class FakeClient:
        def __init__(self):
            self.models = FakeModels()

    embedder = GeminiEmbedder.__new__(GeminiEmbedder)
    embedder.model = "gemini-embedding-001"
    embedder.dimension = 4
    embedder.task_type = "RETRIEVAL_DOCUMENT"
    embedder.batch_size = 2
    embedder.rate_limit_max_retries = 3
    embedder.rate_limit_backoff_seconds = 0.0
    embedder._client = FakeClient()

    texts = ["a", "bb", "ccc", "dddd", "e"]
    vectors = embedder.embed(texts)

    assert embedder._client.models.calls == [["a", "bb"], ["ccc", "dddd"], ["e"]]
    assert vectors == [[1.0], [2.0], [3.0], [4.0], [1.0]]


def _make_gemini_embedder(client, max_retries=3, backoff_seconds=0.0):
    embedder = GeminiEmbedder.__new__(GeminiEmbedder)
    embedder.model = "gemini-embedding-001"
    embedder.dimension = 4
    embedder.task_type = "RETRIEVAL_DOCUMENT"
    embedder.batch_size = 10
    embedder.rate_limit_max_retries = max_retries
    embedder.rate_limit_backoff_seconds = backoff_seconds
    embedder._client = client
    return embedder


def test_gemini_embedder_backs_off_and_retries_on_429_then_succeeds(monkeypatch):
    """실사용 재현: 조문 단위로 잘게 쪼개 임베딩하다 보니(법령 하나에도
    조문+부칙이 수십 건) 배치를 쉬지 않고 연달아 쏘면 분당 쿼터를 금방
    다 써서 429 RESOURCE_EXHAUSTED가 났다. SDK 자체 재시도(tenacity)는
    분당 한도가 풀리기엔 너무 짧게 기다려서 그대로 실패했다 -- 그래서
    이 클래스가 한 번 더, 훨씬 길게(기본 60초) 기다렸다가 같은 배치를
    재시도해야 한다."""
    from google.genai.errors import ClientError

    class FlakyModels:
        def __init__(self):
            self.calls = 0

        def embed_content(self, model, contents, config):
            self.calls += 1
            if self.calls == 1:
                raise ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})
            return {"embeddings": [{"values": [0.1, 0.2]} for _ in contents]}

    class FakeClient:
        def __init__(self):
            self.models = FlakyModels()

    client = FakeClient()
    embedder = _make_gemini_embedder(client)

    sleep_calls = []
    monkeypatch.setattr("knowledge.embedder.time.sleep", lambda s: sleep_calls.append(s))

    vectors = embedder.embed(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert client.models.calls == 2  # 첫 시도 실패 + 재시도 성공
    assert sleep_calls == [embedder.rate_limit_backoff_seconds]


def test_gemini_embedder_gives_up_after_max_retries_on_persistent_429(monkeypatch):
    from google.genai.errors import ClientError

    class AlwaysRateLimitedModels:
        def __init__(self):
            self.calls = 0

        def embed_content(self, model, contents, config):
            self.calls += 1
            raise ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})

    class FakeClient:
        def __init__(self):
            self.models = AlwaysRateLimitedModels()

    client = FakeClient()
    embedder = _make_gemini_embedder(client, max_retries=3)
    monkeypatch.setattr("knowledge.embedder.time.sleep", lambda s: None)

    with pytest.raises(ClientError):
        embedder.embed(["a"])

    assert client.models.calls == 3  # max_retries만큼만 시도하고 포기


def test_gemini_embedder_does_not_retry_non_rate_limit_errors(monkeypatch):
    """429가 아닌 에러(인증 실패, 잘못된 요청 등)는 재시도해도 어차피 또
    실패할 뿐이니 곧바로 전파해야 한다 -- 쓸데없이 60초씩 기다리며 매달리면
    안 된다."""
    from google.genai.errors import ClientError

    class AuthFailingModels:
        def __init__(self):
            self.calls = 0

        def embed_content(self, model, contents, config):
            self.calls += 1
            raise ClientError(401, {"error": {"message": "invalid API key"}})

    class FakeClient:
        def __init__(self):
            self.models = AuthFailingModels()

    client = FakeClient()
    embedder = _make_gemini_embedder(client)
    sleep_calls = []
    monkeypatch.setattr("knowledge.embedder.time.sleep", lambda s: sleep_calls.append(s))

    with pytest.raises(ClientError):
        embedder.embed(["a"])

    assert client.models.calls == 1  # 재시도 없이 바로 전파
    assert sleep_calls == []
