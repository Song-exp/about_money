"""Capture the official general-Nori reference without approving another product."""
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup
from app.models.map_source import MapSourceState
from app.services.map_sync import fetch_source, USER_AGENT


def main():
    reference = json.loads(Path('data/map_kb_nori_reference.json').read_text(encoding='utf-8'))
    url = reference['source_url']
    state = MapSourceState(source_key='reference:kb_nori', source_url=url, source_kind='reference')
    with httpx.Client(timeout=25, follow_redirects=False, headers={'User-Agent':USER_AGENT}) as client:
        content, _ = fetch_source(client, {'url':url}, state, force=True)
    soup = BeautifulSoup(content, 'html.parser')
    headings = [tag.get_text(' ', strip=True) for tag in soup.find_all(['h1', 'h2'])]
    root = Path('reports/map-improvement-20261013-shared-caps')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'kb-nori-reference.html').write_bytes(content)
    metadata = {'source_url': url, 'captured_at':datetime.now(timezone.utc).isoformat(),
                'sha256':hashlib.sha256(content).hexdigest(), 'product':reference['product'],
                'product_heading_present':any(reference['product'].replace(' ','') == heading.replace(' ','') for heading in headings),
                'automatically_approved_benefits':0, 'scope':'general_nori_reference_only'}
    (root / 'source.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__': main()
