"""Resolve Deep Oil's public leaflet afresh; expiring download URLs are not IDs."""
import json
import re
from datetime import datetime
from urllib.parse import urlencode
from urllib.robotparser import RobotFileParser

from app.core.config import settings

ROOT = 'https://www.shinhancard.com'
DEEP_OIL_DISCLOSURE_URL = ROOT + '/mob/MOBFM12051N/MOBFM12051R01.shc'
SEARCH_URL = ROOT + '/mob/MOBFM12051N/MOBFM12051R01C.ajax'
DOWNLOAD_URL = ROOT + '/mob/MOBFM12051N/MOBFM12051R03.shc'
USER_AGENT = 'PayPickMapSync/1.0'


def select_deep_oil_document(data):
    if not isinstance(data, dict) or data.get('mbw_result') != 'S' or not isinstance(data.get('mbw_json'), dict):
        raise ValueError('disclosure_search_failed')
    result = data['mbw_json']
    rows = result.get('crdPdPbnList')
    if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
            or not isinstance(result.get('data'), dict) or result['data'].get('nxtQyKey') != ''):
        raise ValueError('disclosure_search_incomplete')
    matches = [row for row in rows if row.get('CRD_PD_GUI_NM') == 'Deep Oil']
    if len(matches) != 1:
        raise ValueError('disclosure_product_ambiguous')
    row = matches[0]
    if (not re.fullmatch(r'\d{5}', row.get('CRD_PD_GUI_N', ''))
            or not re.fullmatch(r'[A-Fa-f0-9]{64}', row.get('CRD_PD_GUI_FIL_NM', ''))):
        raise ValueError('disclosure_document_identity_invalid')
    try:
        datetime.strptime(row['CRD_PD_GUI_BUL_D'], '%Y.%m.%d')
    except (KeyError, TypeError, ValueError):
        raise ValueError('disclosure_document_date_invalid') from None
    return row


def fetch_deep_oil_leaflet(client):
    """Same public search as the disclosure UI, in one non-login HTTP session.

    Check robots for each route, bound all responses, disallow redirects, and
    reject HTML error pages even when they have HTTP status 200.
    """
    robots = client.get(ROOT + '/robots.txt', follow_redirects=False)
    policy = RobotFileParser()
    if robots.status_code == 404:
        policy.parse(['User-agent: *', 'Allow: /'])
    else:
        if robots.is_redirect:
            raise ValueError('source_redirect_requires_configuration')
        robots.raise_for_status()
        if len(robots.content) > settings.map_source_max_bytes:
            raise ValueError('source_too_large')
        policy.parse(robots.text.splitlines())

    def read(method, url, **kwargs):
        if not policy.can_fetch(USER_AGENT, url):
            raise ValueError('source_disallows_fetch')
        with client.stream(method, url, follow_redirects=False, **kwargs) as response:
            if response.is_redirect:
                raise ValueError('source_redirect_requires_configuration')
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > settings.map_source_max_bytes:
                    raise ValueError('source_too_large')
            return bytes(body), response.headers

    read('GET', DEEP_OIL_DISCLOSURE_URL)
    search, _ = read('POST', SEARCH_URL, data={'mbw_json': json.dumps({
        'crdPdGuiNm': 'Deep Oil', 'crdTcd': '0', 'nxtQyKey': ''}, ensure_ascii=False)})
    try:
        data = json.loads(search)
        row = select_deep_oil_document(data)
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
        raise ValueError('disclosure_search_invalid') from None
    url = DOWNLOAD_URL + '?' + urlencode({'pbnNm': 'Deep Oil', 'aFilenm': row['CRD_PD_GUI_FIL_NM']})
    body, _ = read('GET', url)
    if not body.startswith(b'%PDF'):
        if b'scwi.err.hpp.custm.crdpdpbn.err001' in body:
            raise ValueError('disclosure_download_expired')
        raise ValueError('disclosure_download_not_pdf')
    # Deliberately do not persist expiring URLs, session cookies or conditional
    # headers from a temporary download. The archive stores the PDF by digest.
    return body, {}
