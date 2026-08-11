"""Seed dataset covering the master plan's core test scenarios:

  - 시계열 인지: 자본시장법 적합성원칙 조항의 구법/신법(고령투자자 보호 강화) 및
    SUPERSEDES 관계.
  - 차이니즈월(RBAC): IB 전용 검토서(allowed_depts=("IB",))는 다른 부서 세션에는
    보이지 않아야 함.
  - PII 마스킹: 검토서 원문에 포함된 고객 연락처/계좌번호가 색인 전 마스킹되어야 함.
  - 그래프 교차검증: 법령-해석-제재사례-FAQ 간 관계로 1-hop 확장 및 인용검증 테스트.

build_seed_documents()가 반환하는 RawDocument는 각 커넥터의 dev-mode
(documents=... 생성자 인자)로 주입되어, 실 API 연동 없이도 ingest 파이프라인 전체를
end-to-end로 검증할 수 있게 한다.
"""

from __future__ import annotations

from datetime import date

from ontology.schema import EntityType, RelationType
from pipeline.connectors.base import RawDocument
from pipeline.connectors.case import CaseConnector
from pipeline.connectors.faq import FaqConnector
from pipeline.connectors.interpretation import InterpretationConnector
from pipeline.connectors.law import LawConnector
from pipeline.connectors.regulation import RegulationConnector
from pipeline.connectors.review import ReviewConnector
from pipeline.ingest import IngestPipeline

LAW_OLD_ID = "law:capital-markets-act-suitability-v1"
LAW_NEW_ID = "law:capital-markets-act-suitability-v2"
REGULATION_ID = "regulation:elderly-investor-protection-guideline"
INTERPRETATION_ID = "interpretation:fsc-2023-elderly-wrap-account"
CASE_ID = "case:2022-unsuitable-recommendation-sanction"
REVIEW_ID = "review:ib-2024-wrap-product-launch"
FAQ_ID = "faq:elderly-investor-criteria"


def build_seed_documents() -> dict[str, list[RawDocument]]:
    law_docs = [
        RawDocument(
            external_id="capital-markets-act-suitability-v1",
            entity_type=EntityType.LAW,
            title="자본시장법 제46조(적합성 원칙) - 구법",
            body=(
                "금융투자업자는 일반투자자의 투자목적, 재산상황 및 투자경험 등에 비추어 "
                "적합하지 아니하다고 인정되는 투자권유를 하여서는 아니 된다."
            ),
            effective_date=date(2020, 1, 1),
            superseded_date=date(2023, 7, 1),
            source="국가법령정보센터",
        ),
        RawDocument(
            external_id="capital-markets-act-suitability-v2",
            entity_type=EntityType.LAW,
            title="자본시장법 제46조(적합성 원칙) - 신법(고령투자자 보호 강화)",
            body=(
                "금융투자업자는 일반투자자의 투자목적, 재산상황 및 투자경험 등에 비추어 "
                "적합하지 아니하다고 인정되는 투자권유를 하여서는 아니 되며, 65세 이상 "
                "고령투자자에 대해서는 강화된 설명의무 및 가족·지인 등 조력자 참여 절차를 "
                "추가로 거쳐야 한다."
            ),
            effective_date=date(2023, 7, 1),
            source="국가법령정보센터",
            relations=[(RelationType.SUPERSEDES, LAW_OLD_ID)],
        ),
    ]

    regulation_docs = [
        RawDocument(
            external_id="elderly-investor-protection-guideline",
            entity_type=EntityType.REGULATION,
            title="고령투자자 보호 지침",
            body=(
                "본 지침은 만 65세 이상 고객에 대한 금융상품 권유 시 강화된 절차를 정한다. "
                "랩어카운트, 파생결합증권 등 고난도 상품 권유 시 사전 녹취 및 조력자 입회를 "
                "원칙으로 한다."
            ),
            effective_date=date(2023, 7, 15),
            source="사내 규정관리시스템",
            relations=[(RelationType.CITES, LAW_NEW_ID)],
        ),
    ]

    interpretation_docs = [
        RawDocument(
            external_id="fsc-2023-elderly-wrap-account",
            entity_type=EntityType.INTERPRETATION,
            title="금융위 유권해석: 고령투자자 랩어카운트 판매 시 유의사항",
            body=(
                "고령투자자에게 일임형 랩어카운트를 권유하는 경우에도 개별 투자자산의 "
                "위험도에 대한 설명의무는 면제되지 않으며, 강화된 적합성 원칙이 그대로 적용된다."
            ),
            effective_date=date(2023, 9, 1),
            source="금융위원회 법령해석 회신",
            relations=[(RelationType.INTERPRETS, LAW_NEW_ID)],
        ),
    ]

    case_docs = [
        RawDocument(
            external_id="2022-unsuitable-recommendation-sanction",
            entity_type=EntityType.CASE,
            title="제재사례: 고령투자자 부적합 상품 권유 (2022)",
            body=(
                "A증권은 78세 고객에게 원금손실 위험이 큰 파생결합증권을 적합성 원칙을 "
                "충분히 검토하지 않고 권유하여 과태료 및 임직원 제재를 받았다."
            ),
            effective_date=date(2022, 11, 1),
            source="금융감독원 제재정보공개",
            relations=[(RelationType.VIOLATES, LAW_OLD_ID)],
        ),
    ]

    review_docs = [
        RawDocument(
            external_id="ib-2024-wrap-product-launch",
            entity_type=EntityType.REVIEW,
            title="[IB 전용] OO 고령자 특화 랩상품 신규 출시 검토서",
            body=(
                "본 검토서는 IB사업부 신규 랩상품 출시안에 대한 내부 준법성 검토 결과이다. "
                "테스트 고객 김철수(연락처 010-9876-5432, 계좌 111-222-333444)를 대상으로 "
                "파일럿을 진행하였다. 고령투자자 보호 지침에 따른 조력자 입회 절차를 상품 "
                "가입 프로세스에 반영할 것을 권고한다."
            ),
            effective_date=date(2024, 3, 1),
            source="사내 EDMS",
            allowed_depts=("IB",),
            relations=[(RelationType.CITES, REGULATION_ID)],
        ),
    ]

    faq_docs = [
        RawDocument(
            external_id="elderly-investor-criteria",
            entity_type=EntityType.FAQ,
            title="FAQ: 고령투자자 기준이 어떻게 되나요?",
            body="만 65세 이상 고객이 고령투자자 보호 지침의 적용 대상입니다.",
            source="준법감시부 FAQ",
            relations=[(RelationType.ANSWERED_BY, REGULATION_ID)],
        ),
    ]

    return {
        "law": law_docs,
        "regulation": regulation_docs,
        "interpretation": interpretation_docs,
        "case": case_docs,
        "review": review_docs,
        "faq": faq_docs,
    }


def seed_all(pipeline: IngestPipeline) -> int:
    documents = build_seed_documents()
    connectors = [
        LawConnector(documents=documents["law"]),
        RegulationConnector(documents=documents["regulation"]),
        InterpretationConnector(documents=documents["interpretation"]),
        CaseConnector(documents=documents["case"]),
        ReviewConnector(documents=documents["review"]),
        FaqConnector(documents=documents["faq"]),
    ]
    total = 0
    for connector in connectors:
        total += pipeline.ingest_connector(connector)
    return total
