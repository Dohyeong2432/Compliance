from pipeline.masking import mask_pii


def test_masks_email():
    assert "user@example.com" not in mask_pii("연락처: user@example.com")
    assert "[이메일 마스킹]" in mask_pii("연락처: user@example.com")


def test_masks_resident_registration_number():
    masked = mask_pii("고객 주민번호 900101-1234567 확인")
    assert "900101-1234567" not in masked
    assert "[주민번호 마스킹]" in masked


def test_masks_phone_number():
    masked = mask_pii("연락처 010-1234-5678 입니다")
    assert "010-1234-5678" not in masked
    assert "[전화번호 마스킹]" in masked


def test_masks_account_number():
    masked = mask_pii("계좌 123-456-789012 로 송금")
    assert "123-456-789012" not in masked
    assert "[계좌번호 마스킹]" in masked


def test_leaves_unrelated_text_untouched():
    text = "본 검토서는 신규 상품 출시안에 대한 준법성 검토 결과이다."
    assert mask_pii(text) == text


def test_masks_multiple_pii_types_in_one_document():
    text = "고객 홍길동, 이메일 hong@test.com, 연락처 010-1111-2222, 계좌 111-222-333444"
    masked = mask_pii(text)
    assert "hong@test.com" not in masked
    assert "010-1111-2222" not in masked
    assert "111-222-333444" not in masked
