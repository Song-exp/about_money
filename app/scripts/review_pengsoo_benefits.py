"""Explicit source review of four known Pengsoo services; approvals use the API guard."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
import httpx
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.card import Card, CardBenefit
from app.models.map_source import MapSourceState
from app.models.map_verification import MapEvidence
from app.services.map_sync import fetch_source, capture_official, USER_AGENT
from app.services.map_verification import normalize_location_text, today_kst
from app.scripts.collect_map_official_sources import persist_sources

URL = 'https://card.kbcard.com/CRD/DVIEW/HCAMCXPRICAC0076?cooperationcode=01900&mainCC=a'
TIERS = [{'min_spend':200000,'cap':10000}, {'min_spend':300000,'cap':20000},
         {'min_spend':500000,'cap':30000}, {'min_spend':1000000,'cap':50000}]
SERVICES = {'스타벅스':(20,10000,4000), '아웃백':(20,30000,10000), 'VIPS':(20,30000,10000), 'GS25':(5,10000,1000)}


def main():
    if engine.dialect.name != 'sqlite' or settings.is_production:
        raise RuntimeError('Local reviewed-data application only')
    root = Path('reports/map-improvement-20261013-pengsoo')
    root.mkdir(parents=True, exist_ok=True)
    backup = Path('_backup/map-improvement-20261013-pengsoo')
    backup.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(engine.url.database) as original, sqlite3.connect(backup / f'before-review-{datetime.now():%H%M%S}.db') as copied:
        original.backup(copied)
    with httpx.Client(timeout=25, follow_redirects=False, headers={'User-Agent':USER_AGENT}) as client:
        raw, _ = fetch_source(client, {'url':URL}, MapSourceState(source_key='review:pengsoo', source_url=URL, source_kind='official_evidence'), force=True)
    soup = BeautifulSoup(raw, 'html.parser')
    if not any('펭수노리체크카드' in normalize_location_text(h.get_text()) for h in soup.find_all(['h1','h2'])):
        raise RuntimeError('product_heading_mismatch')
    (root / 'official-detail.html').write_bytes(raw)
    for tag in soup(['script','style','nav','footer']): tag.decompose()
    normalized = normalize_location_text(soup.get_text(' ',strip=True))
    drafts, sources = [], []
    with SessionLocal() as db:
        items = db.scalars(select(CardBenefit).join(Card).where(Card.card_key=='kb_pengsoo_nori_check',
            CardBenefit.brand_key.in_(SERVICES), CardBenefit.is_active.is_(True))).all()
        items = [item for item in items if not item.valid_to or item.valid_to >= today_kst()]
        if len(items) != 4: raise RuntimeError('unexpected_target_inventory')
        for item in items:
            rate, minimum, transaction_cap = SERVICES[item.brand_key]
            value_quote = '아웃백, VIPS 20% 환급할인' if item.brand_key in ('아웃백','VIPS') else f'{item.brand_key} {rate}% 환급할인'
            quotes = {'product':'펭수노리체크카드', 'value':value_quote,
                'minimum':'3만원~5만원' if minimum==30000 else '1만원~2만원',
                'prior_spend':'전월 이용실적 20만원 이상', 'scope':value_quote,
                'period':'펭수노리체크카드 서비스', 'transaction_cap':f'최대 할인금액 {transaction_cap:,}원',
                'monthly_cap':'월간 통합할인한도', 'grace':'60일까지는 전월 이용실적이 없는 경우에도'}
            for name, quote in quotes.items():
                if normalize_location_text(quote) not in normalized:
                    raise RuntimeError(f'source_claim_missing:{item.brand_key}:{name}')
            conditions = ['전월 동일계열 합산 인정 실적 20만원 이상', '월 통합 한도는 동일계열 전체에서 공유',
                '최초 발급·사용등록 60일 실적 유예 적용 여부 확인', '상품권·선불카드 구입/충전 제외',
                'KB 대상 업종 등록 및 PG 전용 가맹점 여부 확인', '호텔·백화점·대형마트·철도역사 입점 등 제외 여부 확인',
                '실적 인정 금액과 잔여 한도는 카드사 안내로 확인']
            item.min_payment_krw, item.min_payment_raw = minimum, f'{minimum:,}원 이상'
            item.cap_text = f'건별 최대 {transaction_cap:,}원 · 실적 구간별 계열 월 통합 한도'
            item.period_text, item.conditions = '카드 기본 서비스 · 원문 재검증 주기 7일', conditions
            item.source_url, item.source_as_of = URL, today_kst()
            source = {'target_kind':'card', 'item_id':item.id, 'url':URL}
            capture_official(db, source, raw)
            db.flush()
            evidence = db.get(MapEvidence, ('card',item.id))
            rules = {'type':'percent','value':rate,'min_payment':minimum,'per_transaction_cap':transaction_cap,
                'prior_spend_required':200000, 'monthly_cap_tiers':TIERS, 'monthly_cap_group':'kb_nori',
                'prior_spend_group':'kb_nori', 'grace_monthly_cap':10000, 'payment_paths':['card'],
                'scope_confirmed':True, 'excluded_items':['상품권','선불카드 구입·충전'],
                'other_conditions':['KB 대상 업종 및 PG 전용 가맹점 제외 확인',
                                    '호텔·백화점·대형마트·철도역사 등 제외 점포가 아님',
                                    '실적 인정 금액과 실제 잔여 한도는 카드사 기준으로 입력']}
            drafts.append({'id':item.id, 'brand':item.brand_key, 'body':{'expected_hash':evidence.content_hash,
                'action':'approved','reviewer':'official_source_structured_review',
                'reason':'정확한 펭수 상품 상세 원문의 할인·최소 금액·실적·한도·제외·유예를 대조. 기본 서비스 안내를 7일마다 재검증하며 사용자 조건 충족은 별도 판정.',
                'indefinite':True,'recheck_days':7,'rules':rules,'evidence_quotes':quotes}})
            sources.append({'kind':'card','item_id':item.id,'url':URL})
        db.commit()
    persist_sources(sources)
    # Uses the same administrative authentication and validation as the review UI.
    from app.main import app
    api = TestClient(app)
    results = []
    for draft in drafts:
        response = api.post(f"/admin/map/review/card/{draft['id']}", json=draft['body'],
                            headers={'X-Admin-Token':settings.admin_token})
        results.append({'id':draft['id'],'brand':draft['brand'],'http_status':response.status_code,
                        'status':response.json().get('status')})
        if response.status_code != 200:
            (root / 'results.json').write_text(json.dumps({'results':results,'error':response.json()},ensure_ascii=False,indent=2),encoding='utf-8')
            raise RuntimeError('review_guard_rejected_approval')
    (root / 'reviews.json').write_text(json.dumps(drafts,ensure_ascii=False,indent=2),encoding='utf-8')
    (root / 'results.json').write_text(json.dumps({'source_url':URL,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'results':results},ensure_ascii=False))


if __name__ == '__main__': main()
