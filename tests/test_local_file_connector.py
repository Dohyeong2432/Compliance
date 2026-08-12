import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from ontology.schema import ALL_DEPARTMENTS, EntityType
from pipeline.connectors.faq import LocalFileFaqConnector
from pipeline.connectors.local_file import (
    LocalFileConnector,
    LocalFileRegulationConnector,
    _external_id_from_filename,
    _parse_latest_effective_date,
    _parse_title,
)
from pipeline.connectors.review import LocalFileReviewConnector


def test_parse_title_takes_first_nonblank_line():
    assert _parse_title("\n\n  임직원 매매지침  \n소관부서 준법지원실") == "임직원 매매지침"


def test_parse_title_empty_text_returns_placeholder():
    assert _parse_title("   \n\n  ") == "제목 없음"


def test_parse_title_skips_decorative_border_lines():
    text = "  -----------------------------\n  윤리강령행동지침 국문본\n  -----------------------------"
    assert _parse_title(text) == "윤리강령행동지침 국문본"


def test_parse_latest_effective_date_picks_most_recent_revision():
    text = "전문제정 2011.1.10\n개정 2017.09.25\n개정 2022.10.26\n개정 2018.09.07"
    assert _parse_latest_effective_date(text) == date(2022, 10, 26)


def test_parse_latest_effective_date_none_when_absent():
    assert _parse_latest_effective_date("아무 날짜도 없는 본문") is None


def test_external_id_prefers_leading_number():
    path = Path("67. 임직원 금융투자상품 매매지침(2022.10.26).docx")
    assert _external_id_from_filename(path) == "67"


def test_external_id_slugifies_when_no_leading_number():
    assert _external_id_from_filename(Path("no number here.docx")) == "no-number-here"


def test_external_id_slugifies_korean_filename_without_leading_number():
    path = Path("겸직 보수배분 기준 (수정).docx")
    assert _external_id_from_filename(path) == "겸직-보수배분-기준-수정"


@pytest.fixture
def require_pandoc():
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")


def test_fetch_converts_docx_and_skips_unparsable_file(tmp_path, require_pandoc):
    md = tmp_path / "source.md"
    md.write_text("제목입니다\n\n개정 2023.01.15\n\n본문 내용입니다.", encoding="utf-8")
    good_docx = tmp_path / "1. 테스트 규정(2023.01.15).docx"
    subprocess.run(["pandoc", str(md), "-o", str(good_docx)], check=True)

    # A .docx extension that isn't actually a valid docx container -- mirrors
    # the DRM-wrapped file discovered in real seed data (real .docx files
    # are zip archives; this one just isn't).
    (tmp_path / "2. 깨진 파일.docx").write_bytes(b"not a real docx container")

    connector = LocalFileRegulationConnector(str(tmp_path))
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].external_id == "1"
    assert docs[0].effective_date == date(2023, 1, 15)
    assert "제목입니다" in docs[0].title

    assert len(connector.errors) == 1
    assert connector.errors[0][0].name == "2. 깨진 파일.docx"


def test_fetch_ignores_unsupported_extensions(tmp_path):
    (tmp_path / "notes.txt").write_text("무시되어야 함", encoding="utf-8")
    connector = LocalFileRegulationConnector(str(tmp_path))
    assert connector.fetch() == []
    assert connector.errors == []


def test_fetch_empty_directory_returns_empty_list(tmp_path):
    connector = LocalFileRegulationConnector(str(tmp_path))
    assert connector.fetch() == []


def test_fetch_nonexistent_directory_returns_empty_list_without_error(tmp_path):
    connector = LocalFileRegulationConnector(str(tmp_path / "does-not-exist"))
    assert connector.fetch() == []
    assert connector.errors == []


def test_root_level_file_uses_connector_default_allowed_depts(tmp_path, require_pandoc):
    md = tmp_path / "source.md"
    md.write_text("공개 검토서\n\n본문 내용", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(tmp_path / "1. 공개 검토서.docx")], check=True)

    connector = LocalFileConnector(str(tmp_path), EntityType.REVIEW, allowed_depts=("ALL",))
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].allowed_depts == (ALL_DEPARTMENTS,)
    assert docs[0].entity_type == EntityType.REVIEW


def test_subfolder_name_becomes_allowed_depts(tmp_path, require_pandoc):
    ib_dir = tmp_path / "IB"
    ib_dir.mkdir()
    md = tmp_path / "source.md"
    md.write_text("IB 전용 검토서\n\n본문 내용", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(ib_dir / "1. IB 검토서.docx")], check=True)

    connector = LocalFileReviewConnector(str(tmp_path))  # default allowed_depts=ALL
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].allowed_depts == ("IB",)
    assert docs[0].entity_type == EntityType.REVIEW


def test_local_file_faq_connector_reads_docx(tmp_path, require_pandoc):
    md = tmp_path / "source.md"
    md.write_text("고령투자자 기준이 뭔가요?\n\n65세 이상을 말합니다.", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(tmp_path / "1. FAQ.docx")], check=True)

    connector = LocalFileFaqConnector(str(tmp_path))
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].entity_type == EntityType.FAQ
    assert "고령투자자" in docs[0].title
