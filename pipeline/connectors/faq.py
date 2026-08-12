"""준법감시부 FAQ 연동.

실 사내 위키/포털 연동 전까지는 사규와 동일하게 로컬 스테이징 디렉터리
(docx/doc/pdf, 한 파일에 Q&A 하나)로 FAQ 원문을 받는다 --
LocalFileFaqConnector가 그 경로다. 파일의 첫 줄이 질문 제목으로, 나머지가
본문(답변)으로 추출된다. 근거 법령/규정과의 ANSWERED_BY 관계는 자동
추출되지 않으므로, 필요하면 fetch()가 반환한 RawDocument.relations를
직접 채운 뒤 ingest_documents()에 넘기세요 (data/raw/regulation/README.md와
동일한 패턴).

FaqConnector(documents=[...])는 실 연동/파일 없이 고정 목록을 주입하는
dev/test 전용 경로로 남겨둔다 — seed_data/seed.py가 이 경로를 쓴다.
"""

from __future__ import annotations

from ontology.schema import ALL_DEPARTMENTS, EntityType
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.connectors.local_file import LocalFileConnector


class FaqConnector(SourceConnector):
    entity_type = EntityType.FAQ

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "FaqConnector 실 연동 미구현: documents로 고정 목록을 주입하거나(dev/test), "
            "실 파일 기반 색인은 LocalFileFaqConnector를 사용하세요."
        )


class LocalFileFaqConnector(LocalFileConnector):
    """data/raw/faq/ 등에 올려둔 FAQ 원문(docx/doc/pdf)을 색인."""

    def __init__(self, directory: str, allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)):
        super().__init__(directory, EntityType.FAQ, allowed_depts)
