"""계약검토 선례(과거 계약 조항 검토 사례) 연동.

/contract-review가 만든 검토 결과를 자동으로 이 소스에 넣지 않는다 --
검증 안 된 자동 판단이 그대로 "선례"로 굳어지면 같은 오류가 반복
재생산될 위험이 있다. 대신 사규/검토서/FAQ와 동일한 방식으로, 준법감시부가
검토·승인을 마친 사례 문서만 로컬 스테이징 디렉터리(docx/doc/pdf)에
올리면 LocalFilePrecedentConnector가 그걸 색인한다 -- 큐레이션은 "승인된
사례만 이 폴더에 올린다"는 사람의 행위 자체로 이루어지며, 별도 승인
워크플로우는 만들지 않는다.

부서 한정 사례는 반드시 <directory>/<부서코드>/ 하위 폴더에 올려야
allowed_depts가 그 부서로 제한된다(REVIEW와 동일한 규칙, 사례에 계약
상대방·금액 등 민감정보가 섞일 수 있다).

반드시 pipeline.masking.mask_pii를 거친 뒤 색인해야 함 -- 이 커넥터가
반환하는 RawDocument.body는 아직 마스킹되지 않은 원문이며, 마스킹은
ingest.py의 공통 단계에서 PRECEDENT 타입에 대해 일괄 적용된다(REVIEW와
동일한 이유: 상대방 연락처/계좌 등 노출 위험).

PrecedentConnector(documents=[...])는 실 연동/파일 없이 고정 목록을
주입하는 dev/test 전용 경로로 남겨둔다.
"""

from __future__ import annotations

from ontology.schema import ALL_DEPARTMENTS, EntityType
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.connectors.local_file import LocalFileConnector


class PrecedentConnector(SourceConnector):
    entity_type = EntityType.PRECEDENT

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "PrecedentConnector 실 연동 미구현: documents로 고정 목록을 주입하거나(dev/test), "
            "실 파일 기반 색인은 LocalFilePrecedentConnector를 사용하세요."
        )


class LocalFilePrecedentConnector(LocalFileConnector):
    """data/raw/precedent/ 등에 올려둔 계약검토 선례 원문(docx/doc/pdf)을 색인.

    조문 분리(split_into_articles) 없이 파일 하나를 문서 하나로 색인한다 --
    사례 문서는 사규처럼 "제N조" 구조가 아니라 사례 서술문이라 REVIEW/FAQ와
    같은 whole-file 색인이 맞다."""

    def __init__(self, directory: str, allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)):
        super().__init__(directory, EntityType.PRECEDENT, allowed_depts)
