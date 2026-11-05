"""Category and overnight rules remain pending until their engines are supported."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, func

from app.models.card import Card, CardBenefit
from app.models.card_collection import CardCollectionAttempt
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence
from app.models.card_publication import CardProgramPublication
from app.services.card_collection import register_source, capture_document
from app.services.card_benefit_extraction import extract_snapshot
from app.services.card_mr_life import MR_LIFE_URL, parse_mr_life
from app.services.card_publication import program_review_status

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def text():
    return Path('tests/fixtures/mr-life-time-benefits.txt').read_text(encoding='utf-8')


def test_time_limits_keep_category_counts_shared_reward_cap_and_overnight_clock():
    payload = parse_mr_life(text())
    day, night = payload['rules']
    assert day['scope'] == {'type': 'issuer_categories', 'categories': ['편의점']}
    assert '커피전문점' in night['scope']['categories']
    assert all('brands' not in rule['scope'] and rule['rate_bps'] == 1000 for rule in payload['rules'])
    assert day['time_window'] is None
    assert night['time_window'] == {'timezone': 'Asia/Seoul', 'basis': 'approval_time',
        'start_hour': 21, 'end_hour': 9, 'crosses_midnight': True, 'boundary_policy': 'unresolved'}
    assert (day['daily_count_cap'], day['monthly_count_cap']) == (1, 5)
    assert (night['daily_count_cap'], night['monthly_count_cap']) == (1, 10)
    assert all(rule['count_scope'] == 'service_category' and rule['max_eligible_payment_per_transaction_krw'] == 10000
        and rule['max_reward_per_transaction_krw'] == 1000 for rule in payload['rules'])
    assert day['limit_group'] == night['limit_group'] == 'time_shared'
    group = payload['limit_groups'][0]
    assert group['cap_unit'] == 'reward_krw'
    assert group['caps_by_spend_tier_krw'] == {'standard': 10000, 'middle': 20000, 'higher': 30000}
    assert group['includes_unextracted_services'] == ['hospital_pharmacy', 'laundry', 'online_shopping', 'taxi']
    assert group['allocation'] == 'issuer_acquisition_order'
    assert [tier['min_prior_spend_krw'] for tier in payload['spend_tiers']] == [300000, 500000, 1000000]
    assert payload['new_card_grace']['applied_tier'] == 'standard'
    assert payload['valid_from'] is payload['valid_until_exclusive'] is None
    for evidence in payload['evidence']:
        assert text()[evidence['start_offset']:evidence['end_offset']] == evidence['quote']


@pytest.mark.parametrize('old,new', [
    ('상품명: 신한카드 Mr.Life', '상품명: 다른 카드'),
    ('50만원 이상 100만원 미만 2만원', '60만원 이상 100만원 미만 2만원'),
    ('100만원 이상 3만원', '110만원 이상 3만원'),
    ('All Day: 편의점, 병원/약국, 세탁비 10%', 'All Day: 편의점, 병원/약국, 세탁비 20%'),
    ('Night: 오후 9시~오전 9시까지 온라인쇼핑, 택시, 식음료 10%',
     'Night: 오후 9시~오전 9시까지 온라인쇼핑, 택시, 식음료 5%'),
    ('Night: 오후 9시~오전 9시까지', 'Night: 오후 8시~오전 9시까지'),
    ('1회 최대 1천원 할인', '1회 최대 2천원 할인'),
    ('구분 영역별 각각 일 1회/월 5회', '구분 영역별 각각 일 6회/월 5회'),
    ('전월실적 30~50만원 구간', '전월실적 40~50만원 구간'),
    ('Night Time 할인서비스는 승인시간 기준', 'Night Time 할인서비스는 매입시간 기준'),
    ('편의점, 병원/약국, 세탁소, 식음료는 신한카드 가맹점 업종 기준', '새 업종 기준'),
    ('주말 할인 할인마트', '변경된 레이아웃'),
])
def test_missing_or_conflicting_conditions_fail_instead_of_guessing(old, new):
    original = text()
    assert old in original
    with pytest.raises(ValueError):
        parse_mr_life(original.replace(old, new))


def test_saved_version_is_idempotent_and_admin_cannot_publish(client, db, tmp_path):
    source = register_source(db, MR_LIFE_URL, provider_key='shinhan', issuer=('shinhan', '신한카드'), now=NOW)
    raw = ('<html><main>'+text()+'</main></html>').encode()
    snapshot = capture_document(db, source, raw, now=NOW, archive_root=tmp_path)
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id, attempted_at=NOW,
        status='captured', http_status=200))
    extracted = extract_snapshot(db, source, snapshot, now=NOW)
    assert extracted['status'] == 'needs_review' and extracted['created']
    assert not extract_snapshot(db, source, snapshot, now=NOW)['created']
    db.commit()
    version = db.get(CardBenefitVersion, extracted['version_id'])
    assert version.review_state == 'pending' and version.publication_state == 'draft'
    for evidence in db.scalars(select(CardBenefitFieldEvidence)):
        assert snapshot.extracted_text[evidence.start_offset:evidence.end_offset] == evidence.quote
    readiness = program_review_status(db, version, now=NOW)
    assert not readiness['can_approve']
    assert 'issuer_category_mapping_not_supported' in readiness['unresolved_issues']
    body = {'action': 'approved', 'expected_payload_hash': version.payload_hash,
        'expected_snapshot_hash': snapshot.raw_sha256, 'reviewer': 'test-reviewer',
        'reason': '업종 및 시간 조건 미지원 혜택의 승인 차단을 확인합니다.',
        'policy_quote': '', 'acknowledged_issues': version.payload['issues']}
    response = client.post(f'/admin/map/card-programs/{version.id}/review',
        headers={'X-Admin-Token': 'test-admin-token'}, json=body)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Card)) == 0
    assert db.scalar(select(func.count()).select_from(CardBenefit)) == 0
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
