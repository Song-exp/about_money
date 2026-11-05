"""Observe scheduled cycles through the running local API, without forcing jobs."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=360)
    parser.add_argument('--output', default='reports/map-pipeline-20261015/scheduled-cycles.json')
    args = parser.parse_args()
    seen = {}
    deadline = time.monotonic()+args.timeout
    with httpx.Client(timeout=10) as client:
        while time.monotonic() < deadline:
            health = client.get('http://127.0.0.1:8000/stores/source-status')
            health.raise_for_status()
            state = health.json()
            run = state.get('latest_pipeline_run')
            if run and run.get('trigger') == 'scheduled':
                seen[run['run_id']] = run
            if len(seen) >= args.cycles:
                break
            time.sleep(5)
    if len(seen) < args.cycles:
        raise RuntimeError('not_enough_observed_scheduled_cycles')
    runs = sorted(seen.values(), key=lambda r: r['started_at'])
    intervals = [(datetime.fromisoformat(b['started_at'])-datetime.fromisoformat(a['started_at'])).total_seconds()
                 for a, b in zip(runs, runs[1:])]
    assert all(abs(interval-state['poll_interval_seconds']) < 30 for interval in intervals)
    assert all(run['outcome'] in ('success', 'needs_review') for run in runs)
    rows = client_rows()
    approved = {b['id'] for item in rows for b in item['card_benefits'] if b['recommendation_status'] == 'approved'}
    result = {'observed_at':datetime.now(timezone.utc).isoformat(), 'cycles':len(runs),
        'interval_seconds':intervals, 'configured_interval_seconds':state['poll_interval_seconds'],
        'runs':runs, 'verified_map_stores':len(rows), 'approved_map_benefit_ids':sorted(approved)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('cycles','interval_seconds','verified_map_stores','approved_map_benefit_ids')}))


def client_rows():
    response = httpx.get('http://127.0.0.1:8000/stores/nearby', params={
        'lat':37.5938,'lng':127.0564,'radius_m':3000,'verified_locations_only':'true','limit':200},timeout=10)
    response.raise_for_status()
    return response.json()


if __name__ == '__main__':
    main()
