from agent.contract_checklist import checklist_for_label


def test_damages_clause_gets_damages_checklist():
    checklist = checklist_for_label("제15조(손해배상)")
    assert "민법 제398조" in checklist


def test_termination_clause_gets_termination_checklist():
    checklist = checklist_for_label("제10조(계약해지)")
    assert "해지 통지 기간" in checklist


def test_jurisdiction_clause_gets_jurisdiction_checklist():
    checklist = checklist_for_label("제20조(관할법원)")
    assert "약관법 제14조" in checklist


def test_confidentiality_clause_gets_confidentiality_checklist():
    checklist = checklist_for_label("제8조(비밀유지)")
    assert "비밀정보의 범위" in checklist


def test_liability_clause_gets_liability_checklist():
    checklist = checklist_for_label("제12조(면책)")
    assert "불가항력" in checklist


def test_outsourcing_clause_gets_outsourcing_checklist():
    checklist = checklist_for_label("제5조(업무의 재위탁)")
    assert "금융지주회사법 제47조" in checklist


def test_unmatched_clause_type_falls_back_to_default_checklist():
    checklist = checklist_for_label("제1조(정의)")
    assert "권리·의무가 대등하게" in checklist


def test_whole_document_fallback_label_gets_default_checklist():
    """"제N조(제목)" 구조가 없는 계약서는 split_into_articles가 빈 리스트를
    반환해 "전체 본문"으로 폴백되는데(agent/contract_review.py), 이 라벨은
    괄호 제목이 없으므로 범용 체크리스트로 떨어져야 한다."""
    checklist = checklist_for_label("전체 본문")
    assert "권리·의무가 대등하게" in checklist


def test_keyword_match_works_even_with_extra_words_in_heading():
    """"업무의 재위탁"처럼 키워드가 제목 전체와 정확히 일치하지 않고
    일부로만 포함돼도 매칭돼야 한다."""
    assert checklist_for_label("제5조(재위탁 제한)") == checklist_for_label("제5조(수탁)")
