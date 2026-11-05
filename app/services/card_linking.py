"""Encrypted connection identifiers and conservative card catalog matching."""
import base64
import hashlib
import json
import re
from difflib import SequenceMatcher
from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings

# CODEF official card overview: https://developer.codef.io/products/card/overview
ISSUERS = {
    '0301': ('KB국민카드', ['KB', '국민']), '0302': ('현대카드', ['현대']),
    '0303': ('삼성카드', ['삼성']), '0304': ('NH농협카드', ['NH', '농협']),
    '0305': ('BC카드', ['BC', '비씨']), '0306': ('신한카드', ['신한']),
    '0307': ('씨티카드', ['씨티', 'CITI']), '0309': ('우리카드', ['우리']),
    '0311': ('롯데카드', ['롯데']), '0313': ('하나카드', ['하나']),
    '0315': ('전북카드', ['전북']), '0316': ('광주카드', ['광주']),
    '0320': ('수협카드', ['수협']), '0321': ('제주카드', ['제주']),
}
CERT_ONLY = {'0316', '0320', '0321'}


def cipher():
    key = hashlib.sha256(('tmi-codef-v1:' + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def unseal(value, ttl=None):
    try:
        return json.loads(cipher().decrypt(value.encode(), ttl=ttl))
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise ValueError('invalid_or_expired_connection') from None


def normalize(value):
    return re.sub(r'[^a-z0-9가-힣]', '', value.casefold())


def belongs(card, organization):
    issuer = normalize(card.issuer or '')
    return any(normalize(alias) in issuer for alias in ISSUERS[organization][1])


def match_cards(rows, catalog, organization):
    available = [c for c in catalog if belongs(c, organization)]
    result = []
    for index, row in enumerate(rows):
        name = str(row.get('name') or '')[:200]
        key = normalize(name.split('|')[0])
        exact = [c for c in available if key and normalize(c.card_name) == key]
        ranked = sorted(available, key=lambda c: SequenceMatcher(None, key, normalize(c.card_name)).ratio(), reverse=True)
        candidates = exact or [c for c in ranked[:5] if key and SequenceMatcher(None, key, normalize(c.card_name)).ratio() >= .45]
        result.append({'index': index, 'name': name or '카드명 미제공', 'type': str(row.get('type') or '')[:100],
            'dormant': bool(row.get('dormant')), 'state': str(row.get('state') or '')[:100],
            'match': 'exact' if len(exact) == 1 else 'needs_confirmation',
            'candidates': [{'id': c.id, 'name': c.card_name, 'issuer': c.issuer} for c in candidates]})
    return result
