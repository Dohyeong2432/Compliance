"""Common shape every source connector normalizes into before ingest.py
maps it onto the ontology.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ontology.schema import ALL_DEPARTMENTS, EntityType, RelationType


@dataclass
class RawDocument:
    external_id: str
    entity_type: EntityType
    title: str
    body: str
    effective_date: date | None = None
    superseded_date: date | None = None
    source: str = ""
    allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)
    relations: list[tuple[RelationType, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def entity_id(self) -> str:
        return f"{self.entity_type.value}:{self.external_id}"


class SourceConnector(ABC):
    entity_type: EntityType

    @abstractmethod
    def fetch(self) -> list[RawDocument]:
        """Return all currently available documents from this source."""
        ...
