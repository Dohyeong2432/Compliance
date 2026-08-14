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

# 조문 헤딩은 실제 사규 파일에서 줄 맨 앞의 "제N조(제목)" 형태로 나타난다.
# 괄호 제목을 필수로 요구하는 이유: catdoc/pdftotext는 고정 폭으로 줄을
# 바꾸는데, "...법\n제47조제4항에 따른..."처럼 문장 중간의 타 법령 인용이
# 우연히 줄 맨 앞에 놓이는 경우가 실제 사규 파일에서 다수 확인됐다(제목
# 없는 "제47조"). 반면 진짜 조문 헤딩은 예외 없이 "제N조(제목)"로 제목이
# 붙어 있으므로, 괄호를 필수 조건으로 두면 이런 줄바꿈 오탐을 걸러낸다.
# "제N조의M" 가지번호도 지원.
_ARTICLE_HEADING = re.compile(
    r"^제\s*(?P<no>\d+)\s*조(?:\s*의\s*(?P<sub>\d+))?\s*\((?P<heading>[^)]{0,60})\)",
    re.MULTILINE,
)
# 부칙은 항상 "제1조(시행일)"부터 번호를 다시 매겨 시작하므로("64. 업무위탁운용지침"
# 실물 파일에서 확인됨), 조문 헤딩 매칭 대상에서 통째로 제외하고 마지막 조문 뒤에
# 그대로 덧붙인다.
_ADDENDUM_HEADING = re.compile(r"^부\s*칙", re.MULTILINE)


def split_into_articles(text: str) -> list[tuple[str, str]]:
    """본문을 (조문 라벨, 조문 본문) 목록으로 분리한다. 조문 헤딩이 하나도
    없으면(정관 별표, 조직도 첨부 등 조문 구조가 아닌 문서) 빈 리스트를
    반환하며, 호출부는 파일 전체를 문서 하나로 색인하는 기존 방식으로
    폴백해야 한다. 재현율이 100%일 필요는 없다 -- 놓친 조문은 파일 전체
    단위 색인이었던 이전 상태와 다를 바 없이 여전히 검색은 되고, 세밀한
    citation 매칭만 못 받을 뿐이다."""
    addendum_match = _ADDENDUM_HEADING.search(text)
    body = text[: addendum_match.start()] if addendum_match else text
    addendum = text[addendum_match.start():].strip() if addendum_match else ""

    matches = list(_ARTICLE_HEADING.finditer(body))
    if not matches:
        return []

    articles = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        jo = f"제{m.group('no')}조" + (f"의{m.group('sub')}" if m.group("sub") else "")
        heading = (m.group("heading") or "").strip()
        label = f"{jo}({heading})" if heading else jo
        articles.append((label, body[m.start():end].strip()))

    if addendum:
        last_label, last_body = articles[-1]
        articles[-1] = (last_label, f"{last_body}\n\n{addendum}")

    return articles


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
