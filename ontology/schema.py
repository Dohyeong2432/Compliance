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
