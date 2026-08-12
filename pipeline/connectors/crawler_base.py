"""Base for connectors backed by a caller-supplied Python crawler function.

Sites for LAW/INTERPRETATION/CASE differ wildly in shape, and two of the
three (금융위/금감원 질의회신, 금감원 제재정보공개) have no stable public API to
hard-code against from here. So the actual crawling (HTTP calls, HTML/XML
parsing, pagination) is deliberately kept out of this codebase and left to
a callback the caller injects -- `fetch_items`, a zero-argument callable
returning `list[dict]`. This module only owns the second half: mapping
that generic dict shape onto RawDocument/Relation.

Expected minimum shape per item:
    {
        "id": str,                                 # external_id
        "title": str,
        "body": str,
        "effective_date": "YYYY-MM-DD" | date | None,      # optional
        "superseded_date": "YYYY-MM-DD" | date | None,     # optional
        "source_url": str,                                  # optional
        "allowed_depts": ["ALL"] | ["IB", ...],             # optional
        "metadata": {...},                                  # optional
        "relations": [["cites", "regulation:69"], ...],     # optional, generic
    }

Relation targets must be fully-qualified entity ids ("<entity_type>:<external_id>",
e.g. "law:capital-markets-act-46") since a relation commonly points at a
different source type (an INTERPRETATION interprets a LAW, a CASE violates a
REGULATION) and a bare id would be ambiguous about which type it names.

Per-entity-type convenience relation keys (e.g. LAW's "supersedes") are
handled by each subclass's _convenience_relations() hook, on top of the
generic "relations" list above.

documents=[...] (dev/test mode) bypasses fetch_items entirely and returns a
fixed RawDocument list as-is -- this is what seed_data/seed.py and existing
tests use, and stays working unchanged.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from ontology.schema import ALL_DEPARTMENTS, RelationType
from pipeline.connectors.base import RawDocument, SourceConnector


def _coerce_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


class CrawledSourceConnector(SourceConnector):
    def __init__(
        self,
        documents: list[RawDocument] | None = None,
        fetch_items: Callable[[], list[dict[str, Any]]] | None = None,
    ):
        self._documents = documents
        self.fetch_items = fetch_items
        self.errors: list[tuple[dict, str]] = []

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        if self.fetch_items is None:
            raise NotImplementedError(
                f"{type(self).__name__}: 'documents'(dev/test용 고정 목록) 또는 "
                "'fetch_items'(실 크롤러 콜백, () -> list[dict])를 생성자에 지정하세요."
            )

        self.errors = []
        results: list[RawDocument] = []
        for item in self.fetch_items():
            try:
                results.append(self._to_document(item))
            except (KeyError, ValueError) as exc:
                self.errors.append((item, str(exc)))
        return results

    def _to_document(self, item: dict[str, Any]) -> RawDocument:
        allowed_depts = tuple(item.get("allowed_depts") or (ALL_DEPARTMENTS,))
        relations: list[tuple[RelationType, str]] = [
            (RelationType(rel_type), target_id) for rel_type, target_id in item.get("relations", [])
        ]
        relations.extend(self._convenience_relations(item))

        return RawDocument(
            external_id=str(item["id"]),
            entity_type=self.entity_type,
            title=str(item["title"]),
            body=str(item["body"]),
            effective_date=_coerce_date(item.get("effective_date")),
            superseded_date=_coerce_date(item.get("superseded_date")),
            source=item.get("source_url") or f"{self.entity_type.value} 크롤러",
            allowed_depts=allowed_depts,
            relations=relations,
            metadata=item.get("metadata") or {},
        )

    def _convenience_relations(self, item: dict[str, Any]) -> list[tuple[RelationType, str]]:
        """Subclasses map their entity-type-specific shorthand key(s) here."""
        return []
