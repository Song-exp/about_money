"""Code-owned consultation scopes; a policy with '장학' in its title remains a policy."""
from typing import Literal

from sqlalchemy import or_

from app.models.scholarship import Category, Scholarship

Consultation = Literal['scholarship', 'youth_policy']
POLICY_PLATFORM = 'onjungchoungnyeon'


def scope_clause(consultation):
    if consultation == 'youth_policy':
        return or_(Scholarship.category == Category.GOV_BENEFIT, Scholarship.source_platform == POLICY_PLATFORM)
    return (Scholarship.category == Category.SCHOLARSHIP) & (Scholarship.source_platform != POLICY_PLATFORM)


def in_scope(row, consultation):
    policy = row.category == Category.GOV_BENEFIT or row.source_platform == POLICY_PLATFORM
    return policy if consultation == 'youth_policy' else not policy and row.category == Category.SCHOLARSHIP


def policy_period(row):
    """Missing dates do not establish continuous recruitment."""
    period = (row.eligibility or {}).get('application_period') or {}
    return {'kind': period.get('kind', 'unknown'), 'start_at': period.get('start_at'),
            'end_at': period.get('end_at'), 'checked_at': row.crawled_at.isoformat() if row.crawled_at else None,
            'label': '상시 신청 안내 · 현재 접수 확인 필요' if period.get('kind') == 'continuous' else
                     '신청 기간 명시 · 원문 확인' if period.get('kind') == 'fixed' else '신청 기간 확인 필요'}
