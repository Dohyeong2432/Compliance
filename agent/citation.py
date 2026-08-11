"""Citation verification.

The system prompt instructs the LLM to mark every factual claim with
[[CITE:entity_id]] using ids that came back from search_knowledge. This
module is what actually enforces that the marker is honest: an id is only
turned into a numbered footnote if it (a) was retrieved during this turn
and (b) still resolves in the graph store. Anything else is left visibly
flagged in the answer rather than silently dropped (which would let an
unsupported claim read as plain, confident prose) or silently trusted
(which would defeat the whole point).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from knowledge.graph_store import GraphStore

CITATION_PATTERN = re.compile(r"\[\[CITE:([A-Za-z0-9_:.\-]+)\]\]")
UNVERIFIED_MARKER = "[출처 미확인]"


@dataclass
class CitationResult:
    text: str
    verified_ids: list[str] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)

    @property
    def has_rejected(self) -> bool:
        return len(self.rejected_ids) > 0


class CitationGuard:
    def __init__(self, graph_store: GraphStore):
        self.graph_store = graph_store

    def apply(self, text: str, retrieved_ids: set[str]) -> CitationResult:
        verified: list[str] = []
        rejected: list[str] = []
        footnotes: dict[str, int] = {}

        def replace(match: re.Match) -> str:
            entity_id = match.group(1)
            if entity_id in retrieved_ids and self.graph_store.has_entity(entity_id):
                if entity_id not in footnotes:
                    footnotes[entity_id] = len(footnotes) + 1
                    verified.append(entity_id)
                return f"[{footnotes[entity_id]}]"
            rejected.append(entity_id)
            return UNVERIFIED_MARKER

        rewritten = CITATION_PATTERN.sub(replace, text)

        if footnotes:
            lines = [rewritten, "", "---", "**참고 문서**"]
            for entity_id, n in sorted(footnotes.items(), key=lambda kv: kv[1]):
                entity = self.graph_store.get_entity(entity_id)
                title = entity.title if entity else entity_id
                lines.append(f"[{n}] {title} ({entity_id})")
            rewritten = "\n".join(lines)

        return CitationResult(text=rewritten, verified_ids=verified, rejected_ids=rejected)
