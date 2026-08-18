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
from pipeline.korean_article_parser import split_into_articles

_REVISION_DATE = re.compile(r"(?:제정|개정)\s*(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})")
_LEADING_NUMBER = re.compile(r"^(\d+)\.")
_SUPPORTED_SUFFIXES = (".docx", ".doc", ".pdf")

# split_into_articles는 pipeline/korean_article_parser.py로 옮겨졌다 (계약서
# 조항 분리에도 같은 로직이 필요해 공유 모듈로 분리) -- 위 import로 재-export해
# 기존 `from pipeline.connectors.local_file import split_into_articles` 경로를
# 그대로 유지한다.


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

            documents.extend(self._documents_from_file(path, text))

        return documents

    def _documents_from_file(self, path: Path, text: str) -> list[RawDocument]:
        """파일 하나를 RawDocument 목록으로 변환한다. 기본 구현은 파일
        전체를 문서 하나로 만든다 -- REVIEW/FAQ는 조문 구조가 없는 보고서/
        Q&A 형식이라 이 기본 동작이 맞다. 조문 단위 분리가 필요한
        REGULATION은 LocalFileRegulationConnector가 오버라이드한다."""
        return [
            RawDocument(
                external_id=_external_id_from_filename(path),
                entity_type=self.entity_type,
                title=_parse_title(text),
                body=text.strip(),
                effective_date=_parse_latest_effective_date(text),
                source=f"로컬 스테이징 원본 ({path.relative_to(self.directory)})",
                allowed_depts=self._allowed_depts_for(path),
            )
        ]

    def _allowed_depts_for(self, path: Path) -> tuple[str, ...]:
        rel_parts = path.relative_to(self.directory).parts
        if len(rel_parts) > 1:
            # <directory>/<부서코드>/파일 형태면 그 하위 폴더명을 부서코드로 사용.
            return (rel_parts[0],)
        return self.allowed_depts


class LocalFileRegulationConnector(LocalFileConnector):
    """REGULATION 전용 -- 조문(제N조) 구조가 감지되면 조문 단위로 쪼개 색인한다.

    title을 "{규정명} 제N조(조제목)" 형태로 만드는 것이 핵심이다 --
    pipeline/citation_extraction.py의 자동 인용관계 추출이 title에서
    "법령명/규정명 제N조" 부분문자열을 찾는 방식으로 동작하는데, 규정 전체가
    파일 하나짜리 entity였을 때는 title에 "제N조"가 없어 이 매칭이 전혀
    걸리지 않았다. 조문 헤딩이 하나도 없는 문서(정관 별표, 조직도 첨부 등)는
    기존처럼 파일 전체를 문서 하나로 색인하는 것으로 폴백한다."""

    def __init__(self, directory: str, allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)):
        super().__init__(directory, EntityType.REGULATION, allowed_depts)

    def _documents_from_file(self, path: Path, text: str) -> list[RawDocument]:
        articles = split_into_articles(text)
        if not articles:
            return super()._documents_from_file(path, text)

        doc_title = _parse_title(text)
        base_id = _external_id_from_filename(path)
        effective_date = _parse_latest_effective_date(text)
        source = f"로컬 스테이징 원본 ({path.relative_to(self.directory)})"
        allowed_depts = self._allowed_depts_for(path)

        return [
            RawDocument(
                external_id=f"{base_id}-{i}",
                entity_type=self.entity_type,
                title=f"{doc_title} {label}",
                body=article_body,
                effective_date=effective_date,
                source=source,
                allowed_depts=allowed_depts,
            )
            for i, (label, article_body) in enumerate(articles, start=1)
        ]
