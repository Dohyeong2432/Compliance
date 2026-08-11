"""Append-only JSONL audit log.

Every turn is recorded regardless of outcome: who asked (dept + user_id from
the verified session, never client-supplied), what was searched, which
entity ids were retrieved, and which citations in the final answer were
verified vs. rejected. This is what makes a compliance review of the
agent's own behavior possible after the fact.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.sso import SessionContext
from agent.tools import ToolCallRecord


@dataclass
class AuditRecord:
    timestamp: str
    user_id: str
    dept: str
    user_message: str
    tool_calls: list[dict[str, Any]]
    verified_citations: list[str]
    rejected_citations: list[str]
    answer: str


class AuditLogger:
    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(
        self,
        session: SessionContext,
        user_message: str,
        tool_calls: list[ToolCallRecord],
        verified: list[str],
        rejected: list[str],
        answer: str,
    ) -> AuditRecord:
        record = AuditRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_id=session.user_id,
            dept=session.dept,
            user_message=user_message,
            tool_calls=[asdict(tc) for tc in tool_calls],
            verified_citations=verified,
            rejected_citations=rejected,
            answer=answer,
        )
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        return record
