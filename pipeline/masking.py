"""PII masking applied to internal review documents before indexing.

Review documents (RBAC-scoped to a single department) can contain customer
identifiers that must never end up verbatim in the vector/graph store, since
those stores don't inherit the EDMS's own access controls beyond dept-level
allowed_depts. Masking runs unconditionally for EntityType.REVIEW in
ingest.py — connectors are not trusted to have already done it.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RRN = re.compile(r"\d{6}-\d{7}")               # 주민등록번호
_PHONE = re.compile(r"01[0-9]-\d{3,4}-\d{4}")    # 휴대전화번호
_ACCOUNT = re.compile(r"\d{2,6}-\d{2,6}-\d{2,6}")  # 계좌번호 (일반 하이픈 구분 형식)

# Order matters: more specific patterns (email, RRN, phone) must run before
# the broader account-number pattern, or they'd be partially consumed by it.
_RULES: list[tuple[re.Pattern, str]] = [
    (_EMAIL, "[이메일 마스킹]"),
    (_RRN, "[주민번호 마스킹]"),
    (_PHONE, "[전화번호 마스킹]"),
    (_ACCOUNT, "[계좌번호 마스킹]"),
]


def mask_pii(text: str) -> str:
    masked = text
    for pattern, replacement in _RULES:
        masked = pattern.sub(replacement, masked)
    return masked
