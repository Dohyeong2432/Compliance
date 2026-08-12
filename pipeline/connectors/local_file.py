"""Local filesystem regulation connector.

Reads staged regulation documents from a directory (docx/doc/pdf) and
converts them into RawDocument objects. This stands in for a real EDMS
connection (see pipeline/connectors/regulation.py's TODO) so documents
uploaded to data/raw/regulation/ can be ingested before that connection
exists.

Shells out to external CLI tools rather than parsing formats in pure
Python, since Korean legacy .doc (CP949-encoded OLE compound files) and
.pdf don't have a reliable pure-Python path:
  - pandoc      -- .docx -> plain text
  - catdoc      -- .doc  -> plain text (CP949 -> UTF-8; legacy Korean Word
                   files are almost never UTF-8)
  - pdftotext   -- .pdf  -> plain text (poppler-utils)

Files that fail to parse (wrong extension, corrupted, or -- as observed
with one seed file -- DRM-encrypted despite a .docx extension) are
skipped, not fatal: fetch() collects them in self.errors instead of
raising, so one bad file doesn't block ingesting the rest of the batch.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

from ontology.schema import ALL_DEPARTMENTS, EntityType
from pipeline.connectors.base import RawDocument, SourceConnector

_REVISION_DATE = re.compile(r"(?:제정|개정)\s*(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})")
_LEADING_NUMBER = re.compile(r"^(\d+)\.")
_SUPPORTED_SUFFIXES = (".docx", ".doc", ".pdf")


class UnparsableDocumentError(RuntimeError):
    pass


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            result = subprocess.run(
                ["pandoc", "-t", "plain", str(path)],
                capture_output=True, text=True, check=True,
            )
        elif suffix == ".doc":
            result = subprocess.run(
                ["catdoc", "-s", "cp949", "-d", "utf-8", str(path)],
                capture_output=True, text=True, check=True,
            )
        elif suffix == ".pdf":
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True, text=True, check=True,
            )
        else:
            raise UnparsableDocumentError(f"Unsupported file type: {suffix}")
    except subprocess.CalledProcessError as exc:
        raise UnparsableDocumentError(
            f"{path.name}: {suffix} parser failed (exit {exc.returncode}): "
            f"{exc.stderr.strip() if exc.stderr else 'no stderr'}"
        ) from exc
    if not result.stdout.strip():
        raise UnparsableDocumentError(f"{path.name}: parser produced no text (possibly encrypted/corrupted)")
    return result.stdout


def _parse_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "제목 없음"


def _parse_latest_effective_date(text: str) -> date | None:
    dates = [date(int(y), int(m), int(d)) for y, m, d in _REVISION_DATE.findall(text)]
    return max(dates) if dates else None


def _external_id_from_filename(path: Path) -> str:
    match = _LEADING_NUMBER.match(path.stem)
    if match:
        return match.group(1)
    # \w is unicode-aware in Python 3, so this keeps Korean characters
    # instead of collapsing filenames with no ASCII letters to "".
    return re.sub(r"[^\w]+", "-", path.stem).strip("-").lower()


class LocalFileRegulationConnector(SourceConnector):
    entity_type = EntityType.REGULATION

    def __init__(self, directory: str, allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)):
        self.directory = Path(directory)
        self.allowed_depts = allowed_depts
        self.errors: list[tuple[Path, str]] = []

    def fetch(self) -> list[RawDocument]:
        self.errors = []
        documents: list[RawDocument] = []

        for path in sorted(self.directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _SUPPORTED_SUFFIXES:
                continue
            try:
                text = _extract_text(path)
            except UnparsableDocumentError as exc:
                self.errors.append((path, str(exc)))
                continue

            documents.append(
                RawDocument(
                    external_id=_external_id_from_filename(path),
                    entity_type=self.entity_type,
                    title=_parse_title(text),
                    body=text.strip(),
                    effective_date=_parse_latest_effective_date(text),
                    source=f"사내 EDMS (로컬 스테이징 원본: {path.name})",
                    allowed_depts=self.allowed_depts,
                )
            )

        return documents
