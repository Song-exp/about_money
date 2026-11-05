"""Cross-check a retained official Mega branch and optionally register it locally."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.scripts.discover_starbucks_store import review_candidate
from app.services.mega_store_identity import mega_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-dir', required=True)
    parser.add_argument('--official-id', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(args.capture_dir)
    candidate = mega_candidate((root / 'positions.json').read_text(encoding='utf-8'),
        (root / 'search.html').read_text(encoding='utf-8'),
        json.loads((root / 'receipt.json').read_text(encoding='utf-8')),
        args.official_id, args.branch, datetime.now(timezone.utc))
    review_candidate(candidate, args.output, args.apply)


if __name__ == '__main__':
    main()
