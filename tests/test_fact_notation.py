import pytest

from app.services.fact_validation import grounded_facts, validate_fact


@pytest.mark.parametrize('key,value,evidence', [
    ('gpa_min', 3.5, '전체 학점 평균이 3.5/4.5 이상'),
    ('gpa_min', 3.0, '직전학기\x00평균평점\x00B0(3.0/4.5\x00또는\x003.0/4.3)\x00이상'),
    ('gpa_min', 2.0, '평 균 평 점 2.0 이상'),
    ('grade_level', ['3', '4'], '4학기 이상 이수한 3~4학년 中'),
    ('grade_level', ['3', '4'], '4 학 기 이상 이수한 3~4 학 년 중'),
])
def test_explicit_source_notation(key, value, evidence):
    assert validate_fact('eligibility', key, value, evidence)


@pytest.mark.parametrize('key,value,evidence', [
    ('gpa_min', 3.5, '전체 학점 평균이 3.0/4.5 이상'),
    ('gpa_min', 3.0, '평점 B0(3.0/4.5 또는 3.5/4.3)이상'),
    ('gpa_min', 3.0, '평점 3.0/4.5 이상 또는 봉사 50시간'),
    ('gpa_min', 3.0, '평점 3.0/4.5 이상, 신입생 제외'),
    ('gpa_min', 3.0, '평점 3.0/5.0 이상'),
    ('gpa_min', 3.0, '평점 3.0/4.5 미만'),
    ('gpa_min', 3.5, '평점 3 .5/4.5 이상'),
    ('grade_level', ['3', '4'], '3학년 이상'),
    ('grade_level', ['3', '4'], '3~4학년 또는 신입생'),
    ('grade_level', ['3', '4'], '3~4학년, 4학년 제외'),
    ('grade_level', ['3', '4'], '4~3학년'),
    ('grade_level', ['3', '4'], '3~4학년 및 2학년'),
    ('grade_level', ['3학년', '4학년'], '3~4학년'),
    ('income_bracket', ['0~3구간'], '학자금 지원 0~3구간'),
    ('income_bracket', [0, 1, 2, 3], '학자금 지원 3구간 이하'),
])
def test_ambiguous_or_wrong_values_remain_rejected(key, value, evidence):
    assert not validate_fact('eligibility', key, value, evidence)


@pytest.mark.parametrize('amount,evidence,expected', [
    (1500000, '❍ 지원내용 : 1인 150만원 생활비', True),
    (1500000, '지 원 내 용 : 1인 150만원 생활비', True),
    (300000, '장 학 금 액 : 300,000 원', True),
    (1500000, '지원내용 : 150만원 또는 100만원', False),
    (1500000, '생활비 지출 150만원', False),
    (1500000, '지원내용 : 15 0만원', False),
])
def test_explicit_payment_notation(amount, evidence, expected):
    assert validate_fact('benefit', 'amount_krw', amount, evidence) is expected


def test_interpretation_keeps_raw_evidence_and_requires_source_membership():
    evidence = '평 균 평점\x003.5/4.5 이상'
    data = {'eligibility': {'gpa_min': 3.5}, 'fact_evidence': {'eligibility.gpa_min': evidence}}
    groups, facts, issues = grounded_facts(data, evidence, {'sha256': 'source'})
    assert groups['eligibility'] == {'gpa_min': 3.5}
    assert facts[0]['evidence'] == evidence and not issues
    assert grounded_facts(data, evidence.replace('3.5', '3.0'), {})[0]['eligibility'] == {}


def test_description_cannot_change_words_during_normalization():
    assert not validate_fact('eligibility', 'target_desc', '재학생', '재 학 생')
    assert not validate_fact('benefit', 'amount_desc', '지원내용', '지 원 내 용')
