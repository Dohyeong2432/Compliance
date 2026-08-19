"""Lexical (BM25) retrieval, complementing dense vector search.

임베딩은 "의미가 비슷한" 문서는 잘 찾지만 정형 용어의 정확 일치에는 약하다.
법률 도메인은 반대로 정형 용어 덩어리다 -- "이해상충", "겸직", "적합성원칙",
법령명 자체처럼 토씨 하나 다르면 다른 개념이 되는 단어들이 답을 가른다.
retriever.py의 조문 인용 직접 매칭("제N조")이 이 문제의 아주 좁은 특수해였다면,
이 모듈은 일반해다.

한국어 형태소 분석기(konlpy/mecab 등)는 설치·운영 부담이 커서 쓰지 않고,
색인 단위를 두 겹으로 잡아 대체한다:
  - 한글 어절 전체 ("이해상충")     -> IDF가 높아 정확 일치에 강하게 반응
  - 그 어절의 문자 바이그램 (이해/해상/상충) -> 어미 변화·부분 일치를 흡수
바이그램만 쓰면 "이해상충"이 "해상보험"에 걸리는 오탐이 생기는데, 어절 전체
term이 함께 있어서 BM25의 IDF 가중이 정답 쪽을 확실히 위로 올린다.

RBAC/시점 필터는 여기서 하지 않는다 -- 색인은 (entity_id, score)만 돌려주고,
부서 권한과 as_of 판정은 HybridRetriever가 그래프의 Entity를 보고 한 곳에서만
수행한다. 권한 판정 지점을 늘리지 않기 위한 의도적인 설계다.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ontology.schema import EntityType, entity_type_from_id

# 한글 어절 / 영숫자 토큰. 그 밖의 문자(구두점, 공백, 한자 등)는 구분자로만 쓴다.
_WORD = re.compile(r"[가-힣]+|[0-9a-zA-Z]+")

# BM25 표준 파라미터. k1은 용어 빈도 포화 지점, b는 문서 길이 정규화 강도.
# 조문 하나(짧음)와 검토서 한 편(김)이 한 색인에 섞이므로 길이 정규화는
# 기본값 그대로 살려 둔다.
_K1 = 1.5
_B = 0.75


@dataclass
class LexicalMatch:
    entity_id: str
    score: float


def tokenize(text: str) -> list[str]:
    """색인·질의 양쪽에서 동일하게 쓰는 토크나이저 (반드시 같아야 한다)."""
    tokens: list[str] = []
    for word in _WORD.findall(text.lower()):
        tokens.append(word)
        if len(word) > 1 and "가" <= word[0] <= "힣":
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


class LexicalIndex:
    """역색인. 기본은 메모리 상주이며, 매 sync 사이클마다 ingest가 다시
    채우므로(pipeline/ingest.py) 그 경우엔 프로세스가 재시작돼도 별도 복구
    절차가 필요 없다.

    persist_path를 주면 얘기가 달라진다 -- IngestSyncer.sync_once()가 사이클
    끝에 save()를 호출해 postings를 디스크에 JSON으로 남기고, 생성 시
    _load()가 그 파일이 있으면 즉시 복원한다. 이게 필요한 이유는 그래프/벡터
    스토어와 달리 이 색인은 영속 백엔드(Chroma/Kuzu)로 바꿔도 자동으로
    같이 영속화되지 않기 때문이다 -- api/main.py에서 SYNC_ON_STARTUP=false로
    시작 시 재색인을 건너뛰면, 이 파일이 없는 한 BM25 채널만 매번 빈 채로
    뜨는 조용한 회귀가 생긴다(그래프/벡터는 영속 백엔드에 이미 데이터가
    있으므로 겉으로는 정상 작동하는 것처럼 보여서 더 위험하다)."""

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._postings: dict[str, dict[str, int]] = defaultdict(dict)  # term -> {entity_id: tf}
        self._doc_terms: dict[str, set[str]] = {}                       # delete용 역참조
        self._doc_len: dict[str, int] = {}
        self.persist_path = Path(persist_path) if persist_path else None
        self._load()

    def _load(self) -> None:
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._postings = defaultdict(dict, {term: dict(tf_by_id) for term, tf_by_id in raw.get("postings", {}).items()})
        self._doc_terms = {doc_id: set(terms) for doc_id, terms in raw.get("doc_terms", {}).items()}
        self._doc_len = dict(raw.get("doc_len", {}))

    def save(self) -> None:
        if self.persist_path is None:
            return
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "postings": dict(self._postings),
            "doc_terms": {doc_id: sorted(terms) for doc_id, terms in self._doc_terms.items()},
            "doc_len": self._doc_len,
        }
        self.persist_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._doc_len)

    def index(self, entity_id: str, text: str) -> None:
        self.delete(entity_id)  # 재색인 시 이전 posting을 남기지 않는다
        tokens = tokenize(text)
        if not tokens:
            return
        freqs: dict[str, int] = defaultdict(int)
        for token in tokens:
            freqs[token] += 1
        for term, tf in freqs.items():
            self._postings[term][entity_id] = tf
        self._doc_terms[entity_id] = set(freqs)
        self._doc_len[entity_id] = len(tokens)

    def delete(self, entity_id: str) -> None:
        for term in self._doc_terms.pop(entity_id, ()):
            postings = self._postings.get(term)
            if postings is None:
                continue
            postings.pop(entity_id, None)
            if not postings:
                del self._postings[term]
        self._doc_len.pop(entity_id, None)

    def search(
        self,
        query: str,
        top_k: int = 10,
        entity_types: tuple[EntityType, ...] | None = None,
    ) -> list[LexicalMatch]:
        if not self._doc_len:
            return []
        query_terms = set(tokenize(query))
        if not query_terms:
            return []

        total_docs = len(self._doc_len)
        avg_len = sum(self._doc_len.values()) / total_docs
        scores: dict[str, float] = defaultdict(float)

        for term in query_terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (total_docs - len(postings) + 0.5) / (len(postings) + 0.5))
            for entity_id, tf in postings.items():
                if entity_types is not None and entity_type_from_id(entity_id) not in entity_types:
                    continue
                norm = _K1 * (1 - _B + _B * self._doc_len[entity_id] / avg_len)
                scores[entity_id] += idf * tf * (_K1 + 1) / (tf + norm)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [LexicalMatch(entity_id, score) for entity_id, score in ranked[:top_k]]
