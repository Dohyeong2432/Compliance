"""내부 검토서(Chinese-wall 대상 문서) 연동.

실 EDMS 연동 전까지는 사규와 동일하게 로컬 스테이징 디렉터리(docx/doc/pdf)로
검토서 원문을 받는다 -- LocalFileReviewConnector가 그 경로다. 부서 한정 검토서는
반드시 <directory>/<부서코드>/ 하위 폴더에 올려야 allowed_depts가 그 부서로
제한된다(예: data/raw/review/IB/문서.docx -> allowed_depts=("IB",)). 디렉터리
바로 아래에 놓인 파일은 생성자에 전달한 allowed_depts(기본 ALL)를 그대로 쓰므로,
전사 공개가 아닌 검토서를 실수로 루트에 두지 않도록 주의할 것.

반드시 pipeline.masking.mask_pii를 거친 뒤 색인해야 함 — 이 커넥터가 반환하는
RawDocument.body는 아직 마스킹되지 않은 원문이며, 마스킹은 ingest.py의 공통
단계에서 REVIEW 타입에 대해 일괄 적용된다(커넥터가 무엇이든 항상 적용됨).

ReviewConnector(documents=[...])는 실 연동/파일 없이 고정 목록을 주입하는
dev/test 전용 경로로 남겨둔다 — seed_data/seed.py가 이 경로를 쓴다.
"""

from __future__ import annotations

from ontology.schema import ALL_DEPARTMENTS, EntityType
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.connectors.local_file import LocalFileConnector


class ReviewConnector(SourceConnector):
    entity_type = EntityType.REVIEW

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "ReviewConnector 실 연동 미구현: documents로 고정 목록을 주입하거나(dev/test), "
            "실 파일 기반 색인은 LocalFileReviewConnector를 사용하세요."
        )


class LocalFileReviewConnector(LocalFileConnector):
    """data/raw/review/ 등에 올려둔 검토서 원문(docx/doc/pdf)을 색인."""

    def __init__(self, directory: str, allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)):
        super().__init__(directory, EntityType.REVIEW, allowed_depts)
