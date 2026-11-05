"""Exact-match review candidates from the audited public Shinhan lookup schema."""
import json
import re
from app.services.map_verification import normalize_location_text

DIRECTORY_URL = 'https://www.shinhancard.com/mob/MOBFM204N/MOBFM204R02.shc'
LOOKUP_URL = 'https://www.shinhancard.com/mob/MOBFM204N/MOBFM204C01.ajax'
COLUMNS = ('MCT_N', 'MCT_NM', 'MCT_AR', 'MCT_RY_NM')


def parse_shinhan_directory(content):
    if not content or len(content) > 2_000_000:
        raise ValueError('issuer_directory_size_invalid')
    try:
        data = json.loads(content)
        if data['mbw_result'] != 'S':
            raise ValueError('issuer_directory_response_failed')
        body = data['mbw_json']
        columns = body['mchtList']
        if not isinstance(columns, dict) or body['isNext'] not in ('Y','N'):
            raise ValueError('issuer_directory_schema_changed')
        if not all(isinstance(columns[key], list) for key in COLUMNS):
            raise ValueError('issuer_directory_schema_changed')
        count = len(columns['MCT_N'])
        if count > 1000 or any(not isinstance(values,list) or len(values) != count for values in columns.values()):
            raise ValueError('issuer_directory_columns_mismatch')
        if body['isNext'] == 'N' and body.get('nxtQyKey') not in ('', None):
            raise ValueError('issuer_directory_paging_conflict')
        if body['isNext'] == 'Y' and (not isinstance(body.get('nxtQyKey'),str) or not body['nxtQyKey'].strip()):
            raise ValueError('issuer_directory_paging_conflict')
        rows = []
        seen = {}
        for index in range(count):
            values = {key: columns[key][index] for key in COLUMNS}
            if any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values.values()):
                raise ValueError('issuer_directory_record_invalid')
            record = {'merchant_id': values['MCT_N'], 'name': values['MCT_NM'].strip(),
                'address': values['MCT_AR'].strip(), 'issuer_category': values['MCT_RY_NM'].strip(),
                'row_index': index, 'json_pointer': '/mbw_json/mchtList'}
            if record['merchant_id'] in seen:
                previous = seen[record['merchant_id']]
                if any(previous[k] != record[k] for k in ('name','address','issuer_category')):
                    raise ValueError('issuer_directory_conflicting_merchant')
            else:
                seen[record['merchant_id']] = record
                rows.append(record)
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('issuer_directory_schema_changed') from None
    return {'issuer_key': 'shinhan', 'scope': 'general_card_merchants_not_point_merchants',
        'complete': body['isNext'] == 'N', 'records': rows, 'public_approval': False}


def address_identity(value):
    # Neighborhood parentheses can accompany a road address; floors/units cannot
    # be dropped when identifying a card merchant inside the same building.
    value = re.sub(r'\(([가-힣0-9]+동)\)', '', value or '').strip()
    if not re.match(r'^(?:서울특별시|서울시|서울)\s+[가-힣]+구\s+', value):
        return None
    return normalize_location_text(value)


def merchant_match(store, directory):
    result = {'store_id': store.id, 'store_name': f'{store.brand} {store.branch}', 'address': store.address,
        'status': 'pending', 'reason': None, 'candidates': [], 'issuer_classification_confirmed': False,
        'public_approval': False, 'review_required': True}
    if (directory.get('issuer_key') != 'shinhan'
            or directory.get('scope') != 'general_card_merchants_not_point_merchants'):
        result['reason'] = 'issuer_directory_identity_unresolved'
        return result
    if not store.is_active:
        result['reason'] = 'inactive_store'
        return result
    address = address_identity(store.address)
    if not address:
        result['reason'] = 'store_address_not_specific'
        return result
    if not directory['complete']:
        result['reason'] = 'directory_pagination_incomplete'
        return result
    expected = normalize_location_text(result['store_name'])
    exact = [record for record in directory['records'] if normalize_location_text(record['name']) == expected
        and address_identity(record['address']) == address]
    result['candidates'] = exact
    if len(exact) != 1:
        result['reason'] = 'ambiguous_exact_match' if exact else 'no_exact_name_and_address_match'
        return result
    result.update(status='unique_exact_match', reason='issuer_evidence_review_required')
    return result
