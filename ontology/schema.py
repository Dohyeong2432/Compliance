"""Ontology for the group compliance knowledge graph.

Six source types come straight from the master plan's knowledge pipeline
(law / interpretation / case / regulation / internal review / FAQ). The
relation set is what the hybrid retriever actually walks: SUPERSEDES for
time-awareness, VIOLATES / INTERPRETS / CITES for cross-source grounding,
SCOPED_TO for RBAC, and the remaining relations for topical 1-hop expansion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class EntityType(str, Enum):
    LAW = "law"                    # 법령 (자본시장법 등)
    INTERPRETATION = "interpretation"  # 유권해석 (금융위/금감원 질의회신)
    CASE = "case"                  # 제재사례
    REGULATION = "regulation"      # 사내 규정/지침
    REVIEW = "review"              # 내부 검토서 (부서 한정 접근)
    FAQ = "faq"                    # 준법감시부 FAQ


class RelationType(str, Enum):
    SUPERSEDES = "supersedes"          # 신법 -> 구법 (시계열 체인)
    AMENDS = "amends"                  # 개정 -> 원문 (부분 개정)
    INTERPRETS = "interprets"          # 유권해석 -> 법령/규정
    VIOLATES = "violates"              # 제재사례 -> 위반한 법령/규정
    CITES = "cites"                    # 문서 -> 인용한 다른 문서
    APPLIES_TO = "applies_to"          # 법령/규정 -> 적용 대상 상품/업무
    SCOPED_TO = "scoped_to"            # 문서 -> 접근 가능 부서 (RBAC)
    RELATED_TO = "related_to"          # 일반 토픽 연관 (1-hop 확장용)
    ANSWERED_BY = "answered_by"        # FAQ -> 근거 문서


# Department codes used for RBAC. "ALL" means firm-wide visibility.
ALL_DEPARTMENTS = "ALL"


# 규범적 권위 위계. 검색 정확도와는 별개의 축이다 -- 아무리 질의와 유사해도
# 내부 검토서(누군가의 의견)와 법령(강행규범)이 같은 무게로 인용되면 답변
# 자체가 위험해진다. HybridRetriever는 관련성으로 "무엇을" 가져올지 고르고,
# 이 위계로 "어떤 순서로" 제시할지 정한다(agent/tools.py가 이 라벨을 문서
# 블록에 같이 실어 LLM이 층위를 알고 답하게 한다).
#
# 서열 근거: 구속력 있는 규범(1-2)이 먼저, 그 규범에 대한 외부 해석·선례
# (3-4)가 다음, 내부 참고자료(5-6)가 마지막. 회사 정책에 따라 조정 가능한
# 값이며, 여기 한 곳만 고치면 검색·표시 전 경로에 반영된다.
AUTHORITY_RANK: dict[EntityType, int] = {
    EntityType.LAW: 1,
    EntityType.REGULATION: 2,
    EntityType.INTERPRETATION: 3,
    EntityType.CASE: 4,
    EntityType.REVIEW: 5,
    EntityType.FAQ: 6,
}

# LLM에게 그대로 노출되는 위계 설명. "구속력 없음"처럼 답변 태도가 달라져야
# 하는 정보를 문서 블록 안에 명시해, 프롬프트의 일반 지침만으로 층위를
# 지키기를 기대하지 않는다.
AUTHORITY_LABEL: dict[EntityType, str] = {
    EntityType.LAW: "법령 (강행규범)",
    EntityType.REGULATION: "사내규정 (내부 강행규범)",
    EntityType.INTERPRETATION: "유권해석 (감독당국 공식 해석)",
    EntityType.CASE: "제재사례 (집행 선례)",
    EntityType.REVIEW: "내부 검토서 (참고 의견, 구속력 없음)",
    EntityType.FAQ: "FAQ (내부 정리자료, 구속력 없음)",
}

_LOWEST_AUTHORITY = max(AUTHORITY_RANK.values()) + 1


def authority_rank(entity_type: EntityType) -> int:
    """낮을수록 권위가 높다. 모르는 타입은 항상 맨 뒤로 보낸다."""
    return AUTHORITY_RANK.get(entity_type, _LOWEST_AUTHORITY)


def entity_type_from_id(entity_id: str) -> EntityType | None:
    """entity_id 접두사에서 타입을 복원한다 ("law:009374-47-0@2023-09-14" -> LAW).

    RawDocument.entity_id가 항상 "{type.value}:{external_id}"로 만들어지므로
    (pipeline/connectors/base.py) id만으로 타입을 알 수 있다. 벡터 스토어처럼
    Entity 전체를 들고 있지 않은 계층에서 타입 필터를 걸 때, 저장 스키마를
    바꾸고 기존 색인을 마이그레이션하는 대신 이 함수를 쓴다.
    """
    prefix, sep, _ = entity_id.partition(":")
    if not sep:
        return None
    try:
        return EntityType(prefix)
    except ValueError:
        return None


@dataclass
class Entity:
    id: str
    type: EntityType
    title: str
    body: str
    effective_date: date | None = None     # 시행일 (법령/규정)
    superseded_date: date | None = None    # 폐지/개정일 (None이면 현행)
    allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)  # RBAC: 조회 가능 부서
    source: str = ""                        # 원문 출처 (예: 국가법령정보센터)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_effective_at(self, at: date | datetime) -> bool:
        """Whether this version was the one in force at the given moment."""
        if isinstance(at, datetime):
            at = at.date()
        if self.effective_date is not None and at < self.effective_date:
            return False
        if self.superseded_date is not None and at >= self.superseded_date:
            return False
        return True

    def is_visible_to(self, dept: str) -> bool:
        if ALL_DEPARTMENTS in self.allowed_depts:
            return True
        return dept in self.allowed_depts


@dataclass
class Relation:
    source_id: str
    type: RelationType
    target_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
