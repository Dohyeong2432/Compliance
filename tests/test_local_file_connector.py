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
    split_into_articles,
)
from pipeline.connectors.precedent import LocalFilePrecedentConnector
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


def test_fetch_skips_file_when_parser_binary_is_missing(tmp_path, require_pandoc, monkeypatch):
    """catdoc(.doc)/pdftotext(.pdf) 등 파서 프로그램 자체가 PATH에 없으면
    subprocess.run이 FileNotFoundError(OSError)를 던진다 -- 이것도
    CalledProcessError처럼 그 파일만 건너뛰고 나머지는 계속 처리해야 한다."""
    md = tmp_path / "source.md"
    md.write_text("정상 문서 제목\n\n본문 내용입니다.", encoding="utf-8")
    good_docx = tmp_path / "1. 정상 문서.docx"
    subprocess.run(["pandoc", str(md), "-o", str(good_docx)], check=True)

    (tmp_path / "2. 구버전 문서.doc").write_bytes(b"legacy doc content, irrelevant to this test")

    import pipeline.connectors.local_file as local_file_module

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[0] == "catdoc":
            raise FileNotFoundError(2, "지정된 파일을 찾을 수 없습니다", "catdoc")
        return real_run(args, **kwargs)

    monkeypatch.setattr(local_file_module.subprocess, "run", fake_run)

    connector = LocalFileRegulationConnector(str(tmp_path))
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].external_id == "1"
    assert len(connector.errors) == 1
    assert connector.errors[0][0].name == "2. 구버전 문서.doc"
    assert "catdoc" in connector.errors[0][1]


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


# ---------------------------------------------------------------------------
# split_into_articles / REGULATION 조문 단위 분리
# ---------------------------------------------------------------------------


def test_split_into_articles_splits_on_article_headings():
    text = (
        "제1조(목적) 이 지침은 목적을 정한다.\n\n"
        "제2조(정의) 이 지침에서 용어의 정의는 다음과 같다.\n\n"
        "제3조(적용범위) 이 지침은 다음의 경우에 적용한다."
    )
    articles = split_into_articles(text)

    assert [label for label, _ in articles] == ["제1조(목적)", "제2조(정의)", "제3조(적용범위)"]
    assert "목적을 정한다" in articles[0][1]
    assert "제2조" not in articles[0][1]  # 다음 조문 헤딩이 앞 조문 본문에 섞이면 안 됨


def test_split_into_articles_handles_sub_numbered_article():
    text = "제6조(원칙) 본문.\n\n제6조의2(예외) 예외 규정 본문."
    articles = split_into_articles(text)
    assert [label for label, _ in articles] == ["제6조(원칙)", "제6조의2(예외)"]


def test_split_into_articles_ignores_inline_citation_mid_sentence():
    """"법 제47조에 따라"처럼 문장 중간의 인용은 줄 앞이 아니므로 조문
    헤딩으로 오인되면 안 된다."""
    text = "제1조(목적) 이 지침은 금융지주회사법 제47조에 따라 업무 위탁을 정한다."
    articles = split_into_articles(text)
    assert len(articles) == 1
    assert articles[0][0] == "제1조(목적)"


def test_split_into_articles_appends_addendum_to_last_article_without_renumbering():
    """부칙은 항상 제1조부터 번호를 다시 매기므로, 별도 조문으로 분리하면 안
    되고 마지막 조문 뒤에 그대로 붙여야 한다."""
    text = (
        "제1조(목적) 본문1.\n\n"
        "제2조(정의) 본문2.\n\n"
        "부   칙(2010.03.22)\n\n"
        "1. 이 규정은 2010. 3. 22일부터 시행한다.\n\n"
        "부   칙(2022.02.22)\n\n"
        "1. 이 규정은 2022. 3. 1일부터 시행한다."
    )
    articles = split_into_articles(text)

    assert [label for label, _ in articles] == ["제1조(목적)", "제2조(정의)"]
    assert "부   칙(2010.03.22)" in articles[-1][1]
    assert "2022. 3. 1일부터 시행한다" in articles[-1][1]


def test_split_into_articles_returns_empty_when_no_article_headings():
    """정관 별표/조직도 첨부처럼 조문 구조가 아닌 문서는 빈 리스트를 반환해
    호출부가 파일 전체 단위 색인으로 폴백하게 한다."""
    assert split_into_articles("업무분장표\n\n1. 총무팀: 총무 업무\n2. 인사팀: 인사 업무") == []


def test_regulation_connector_splits_docx_into_per_article_documents(tmp_path, require_pandoc):
    md = tmp_path / "source.md"
    md.write_text(
        "업무위탁운용지침\n\n"
        "제정 2010.03.22\n\n"
        "개정 2022.02.22\n\n"
        "제1조(목적) 이 지침은 「금융지주회사법」 제47조에 따라 업무 위탁을 정한다.\n\n"
        "제2조(정의) 이 지침에서 업무위탁이란 다음을 말한다.\n\n"
        "부칙(2022.02.22)\n\n"
        "1. 이 규정은 2022. 3. 1일부터 시행한다.",
        encoding="utf-8",
    )
    subprocess.run(["pandoc", str(md), "-o", str(tmp_path / "64. 업무위탁운용지침.docx")], check=True)

    connector = LocalFileRegulationConnector(str(tmp_path))
    docs = connector.fetch()

    assert [d.external_id for d in docs] == ["64-1", "64-2"]
    assert docs[0].title == "업무위탁운용지침 제1조(목적)"
    assert docs[1].title == "업무위탁운용지침 제2조(정의)"
    assert all(d.entity_type == EntityType.REGULATION for d in docs)
    assert "부칙" in docs[1].body  # 부칙은 마지막 조문에 붙음


def test_regulation_connector_falls_back_to_whole_file_without_article_headings(tmp_path, require_pandoc):
    """test_fetch_converts_docx_and_skips_unparsable_file과 동일한 폴백 계약
    -- 조문 헤딩이 없는 사규 파일(정관 별표 등)은 예전처럼 파일 전체가
    문서 하나가 돼야 한다."""
    md = tmp_path / "source.md"
    md.write_text("업무분장표\n\n1. 총무팀: 총무 업무\n2. 인사팀: 인사 업무", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(tmp_path / "42. 업무분장표.docx")], check=True)

    connector = LocalFileRegulationConnector(str(tmp_path))
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].external_id == "42"
    assert docs[0].title == "업무분장표"


# ---------------------------------------------------------------------------
# LocalFilePrecedentConnector (계약검토 선례)
# ---------------------------------------------------------------------------


def test_precedent_connector_indexes_file_as_single_whole_document(tmp_path, require_pandoc):
    """사례 문서는 사규와 달리 "제N조" 구조가 아니라 서술문이므로, 조문
    분리 없이 파일 하나가 문서 하나가 돼야 한다(REVIEW/FAQ와 동일)."""
    md = tmp_path / "source.md"
    md.write_text(
        "2024 업무위탁계약 검토 사례\n\n"
        "제1조(목적)와 유사한 조항을 검토한 사례입니다. 위탁 범위가 과도하게 넓어 수정 요청함.",
        encoding="utf-8",
    )
    subprocess.run(["pandoc", str(md), "-o", str(tmp_path / "1. 업무위탁 사례.docx")], check=True)

    connector = LocalFilePrecedentConnector(str(tmp_path))
    docs = connector.fetch()

    assert len(docs) == 1
    assert docs[0].entity_type == EntityType.PRECEDENT
    assert docs[0].title == "2024 업무위탁계약 검토 사례"


def test_precedent_connector_subfolder_restricts_allowed_depts(tmp_path, require_pandoc):
    ib_dir = tmp_path / "IB"
    ib_dir.mkdir()
    md = tmp_path / "source.md"
    md.write_text("IB 전용 검토 사례\n\n본문 내용", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(ib_dir / "1. IB 사례.docx")], check=True)

    connector = LocalFilePrecedentConnector(str(tmp_path))
    docs = connector.fetch()

    assert docs[0].allowed_depts == ("IB",)


def test_precedent_connector_root_level_file_is_firm_wide(tmp_path, require_pandoc):
    md = tmp_path / "source.md"
    md.write_text("전사 공통 비밀유지계약 검토 사례\n\n본문 내용", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(tmp_path / "1. 사례.docx")], check=True)

    connector = LocalFilePrecedentConnector(str(tmp_path))
    docs = connector.fetch()

    assert docs[0].allowed_depts == (ALL_DEPARTMENTS,)
