"""Local filesystem document connector, generalized across entity types.

Reads staged documents from a directory (docx/doc/pdf) and converts them
into RawDocument objects. Originally built for sagyu (REGULATION) as a
stand-in for a real EDMS connection (see pipeline/connectors/regulation.py's
TODO); REVIEW and FAQ documents are staged the same way (see
pipeline/connectors/review.py / faq.py for their thin subclasses), since
none of the three has a real system connection yet and all three arrive as
docx/doc/pdf files in practice.

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

The directory is walked recursively. A file directly under the root
directory gets the connector's default allowed_depts (ALL unless
overridden); a file nested one level down gets that subfolder's name as
its allowed_depts, e.g. <directory>/IB/문서.docx -> allowed_depts=("IB",).
This matters most for REVIEW documents, which are the Chinese-wall-gated
source in this system's ontology -- staging an IB-only review under an
"IB" subfolder is what actually restricts it to IB sessions, so put
department-restricted files in a subfolder rather than the directory root.
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
    # encoding="utf-8" is required, not just nice-to-have: pandoc/catdoc/
    # pdftotext all emit UTF-8 regardless of the host OS, but
    # subprocess.run(..., text=True) without an explicit encoding decodes
    # using the platform's preferred encoding -- cp949 on Korean Windows --
    # which raises UnicodeDecodeError (silently, in a background reader
    # thread) on any non-ASCII output. That leaves result.stdout as None
    # rather than raising where this function could catch it, so this isn't
    # optional on Windows.
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            result = subprocess.run(
                ["pandoc", "-t", "plain", str(path)],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
        elif suffix == ".doc":
            result = subprocess.run(
                ["catdoc", "-s", "cp949", "-d", "utf-8", str(path)],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
        elif suffix == ".pdf":
            result = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
        else:
            raise UnparsableDocumentError(f"Unsupported file type: {suffix}")
    except subprocess.CalledProcessError as exc:
        raise UnparsableDocumentError(
            f"{path.name}: {suffix} parser failed (exit {exc.returncode}): "
            f"{exc.stderr.strip() if exc.stderr else 'no stderr'}"
        ) from exc
    except OSError as exc:
        # e.g. the parser binary itself isn't installed/on PATH (catdoc is
        # the common case -- .doc support is optional, see crawlers/README
        # for the LibreOffice-conversion alternative). This must stay a
        # per-file skip like CalledProcessError above, not a hard failure --
        # one file needing a tool that isn't installed shouldn't block
        # ingesting every other file in the batch.
        raise UnparsableDocumentError(f"{path.name}: {suffix} parser ('{exc.filename}') not available: {exc}") from exc
    if not result.stdout.strip():
        raise UnparsableDocumentError(f"{path.name}: parser produced no text (possibly encrypted/corrupted)")
    return result.stdout


_DECORATIVE_LINE = re.compile(r"^[\s\-=_*~+#]+$")


def _parse_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        # pandoc renders table/box borders as bare punctuation lines
        # (e.g. "-----"); skip those to find the real first heading.
        if stripped and not _DECORATIVE_LINE.match(stripped):
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


class LocalFileConnector(SourceConnector):
    """entity_type을 매개변수로 받는 제네릭 로컬 파일 커넥터.

    REGULATION 전용이던 원래 구현을 REVIEW/FAQ 등 "문서 기반이지만 실 시스템
    연동은 아직 없는" 소스 전반에 재사용할 수 있도록 일반화한 것. 편의를 위해
    pipeline/connectors/{local_file,review,faq}.py에 엔티티 타입별 서브클래스가
    있다.
    """

    def __init__(
        self,
        directory: str,
        entity_type: EntityType,
        allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,),
    ):
        self.directory = Path(directory)
        self.entity_type = entity_type
        self.allowed_depts = allowed_depts
        self.errors: list[tuple[Path, str]] = []

    def fetch(self) -> list[RawDocument]:
        self.errors = []
        documents: list[RawDocument] = []

        if not self.directory.exists():
            return documents

        for path in sorted(self.directory.rglob("*")):
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
                    source=f"로컬 스테이징 원본 ({path.relative_to(self.directory)})",
                    allowed_depts=self._allowed_depts_for(path),
                )
            )

        return documents

    def _allowed_depts_for(self, path: Path) -> tuple[str, ...]:
        rel_parts = path.relative_to(self.directory).parts
        if len(rel_parts) > 1:
            # <directory>/<부서코드>/파일 형태면 그 하위 폴더명을 부서코드로 사용.
            return (rel_parts[0],)
        return self.allowed_depts


class LocalFileRegulationConnector(LocalFileConnector):
    """REGULATION 전용 편의 서브클래스 (하위 호환 유지)."""

    def __init__(self, directory: str, allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)):
        super().__init__(directory, EntityType.REGULATION, allowed_depts)
