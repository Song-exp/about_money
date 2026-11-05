"""Read-only audit of local benefit provenance and recommendation eligibility."""
from collections import Counter
from datetime import date
import json
from pathlib import Path
from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.card import CardBenefit
from app.models.store import StoreOffer
from app.models.local_benefit import LocalBenefitMerchant
from app.api.stores import _load_card_benefits, _offer_source_metadata
from app.services.map_sync import configured_sources, source_health


def value(x):
    return getattr(x, 'value', x)


def main():
    with SessionLocal() as db:
        today = date.today()
        cards = db.scalars(select(CardBenefit)).all()
        offers = db.scalars(select(StoreOffer)).all()
        local = db.scalars(select(LocalBenefitMerchant)).all()
        eligible = [x for rows in _load_card_benefits(db, False).values() for x in rows]
        metadata = _offer_source_metadata(db)
        result = {
            'audit_date': today.isoformat(), 'database_mutated': False,
            'card_benefits': {
                'total': len(cards), 'confidence': dict(Counter(value(x.confidence) for x in cards)),
                'source_dates': dict(Counter(str(x.source_as_of) for x in cards)),
                'missing_source_url': sum(not x.source_url for x in cards),
                'expired': sum(bool(x.valid_to and x.valid_to < today) for x in cards),
                'default_map_eligible': len(eligible),
                'eligible_confidence': dict(Counter(value(x.confidence) for x in eligible)),
                'eligible_missing_source_url': sum(not x.source_url for x in eligible),
                'eligible_without_expiry': sum(x.valid_to is None for x in eligible),
                'eligible_source_dates': dict(Counter(str(x.source_as_of) for x in eligible)),
            },
            'simple_pay_offers': {
                'total': len(offers), 'samples': sum(x.is_sample for x in offers),
                'non_sample': sum(not x.is_sample for x in offers),
                'expired': sum(bool(x.valid_to and x.valid_to < today) for x in offers),
                'with_pipeline_provenance': sum(x.id in metadata for x in offers),
            },
            'local_benefits': {
                'total': len(local), 'source_types': dict(Counter(x.source_type for x in local)),
                'confidence': dict(Counter(x.confidence for x in local)),
                'missing_source_url': sum(not x.source_url for x in local),
                'without_expiry': sum(x.valid_to is None for x in local),
            },
            'promotion_feeds_configured': sum(x['kind'] == 'simple_pay_promotion' for x in configured_sources()),
            'source_health': source_health(db),
        }
    root = Path('reports/map-verification-audit-20261012')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
