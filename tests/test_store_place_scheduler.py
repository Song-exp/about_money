from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent, MapJobLease
from app.models.store import Store
from app.services import store_place_scheduler as job
from app.services import store_directory_revalidation as lookup
from test_store_directory_revalidation import change, acknowledge, document, run


@pytest.fixture
def scheduled(db, change, monkeypatch):
    monkeypatch.setattr(settings, 'store_collection_enabled', True)
    monkeypatch.setattr(settings, 'store_place_revalidation_enabled', True)
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'test', 'provider': 'outback', 'query': '청량리'}]))
    calls = []
    def perform(candidate, client):
        calls.append(candidate)
        return candidate['name'], [document(change)], 'a' * 64
    monkeypatch.setattr(lookup, 'lookup_place', perform)
    return change, calls


def test_only_reviewed_changes_run_once_per_day_without_publication(db, scheduled):
    change, calls = scheduled
    assert job.run_due(db)['results'] == []
    acknowledge(db, change)
    first = job.run_due(db)
    assert first['results'][0]['status'] == 'matched' and len(calls) == 1
    assert job.run_due(db)['results'] == [] and len(calls) == 1
    assert db.scalar(select(func.count()).select_from(Store)) == 0
    assert job.job_status(db)['status'] == 'complete'
    assert job.job_status(db)['stale'] is False


def test_manual_check_defers_schedule_and_applied_change_is_excluded(db, scheduled):
    change, calls = scheduled
    acknowledge(db, change)
    run(db, change)
    assert job.run_due(db)['due'] == 0 and len(calls) == 1
    db.add(MapReviewEvent(kind='store', item_id=1, action='store_directory_applied',
        at=datetime.now(timezone.utc), payload={'change_id': change.id, 'official_id': '279'}))
    db.commit()
    assert job.targets(db) == []


def test_missing_key_is_monitored_without_lookup_or_failure_flood(db, scheduled, monkeypatch):
    change, calls = scheduled
    acknowledge(db, change)
    monkeypatch.setattr(settings, 'kakao_rest_api_key', '')
    for _ in range(2):
        assert job.run_due(db)['status'] == 'blocked'
    status = job.job_status(db)
    assert status['error'] == 'kakao_rest_api_key_required' and not calls
    assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(MapReviewEvent.action == lookup.ACTION)) == 0
    assert db.scalar(select(func.count()).select_from(MapSourceState).where(MapSourceState.source_key == job.KEY)) == 1


def test_transient_retry_backoff_and_semantic_mismatch_delay(db, scheduled, monkeypatch):
    change, calls = scheduled
    acknowledge(db, change)
    def timeout(candidate, client):
        raise httpx.ReadTimeout('private')
    monkeypatch.setattr(lookup, 'lookup_place', timeout)
    job.run_due(db)
    state = db.get(MapSourceState, f'place-recheck:{change.id}:279')
    assert state.failures == 1 and state.error == 'place_lookup_failed'
    assert 290 <= (job.aware(state.next_retry_at) - datetime.now(timezone.utc)).total_seconds() <= 300
    state.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    job.run_due(db)
    assert state.failures == 2
    assert 590 <= (job.aware(state.next_retry_at) - datetime.now(timezone.utc)).total_seconds() <= 600
    state.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    monkeypatch.setattr(lookup, 'lookup_place', lambda candidate, client: ('query', [], 'a' * 64))
    job.run_due(db)
    assert state.error == 'no_unique_exact_place'
    assert (job.aware(state.next_retry_at) - datetime.now(timezone.utc)).total_seconds() > 86000


def test_busy_disabled_stale_heartbeat_and_auth(client, db, scheduled, monkeypatch):
    assert job.job_status(db)['stale']
    db.add(MapJobLease(key=job.KEY, owner='another', expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    db.commit()
    assert job.run_due(db)['status'] == 'busy'
    monkeypatch.setattr(settings, 'store_place_revalidation_enabled', False)
    assert job.run_due(db)['status'] == 'disabled'
    url = '/admin/map/store-directories/place-revalidation'
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'X-Admin-Token': 'test-admin-token'}).json()['enabled'] is False


def test_handoff_runs_even_when_directory_collection_fails(monkeypatch):
    from app.services import scheduler, store_directory_collection
    calls = []
    def failed(*args, **kwargs):
        raise ValueError('synthetic')
    monkeypatch.setattr(store_directory_collection, 'collect_store_directories', failed)
    monkeypatch.setattr(job, 'run_due', lambda db: calls.append('place') or {'status': 'complete'})
    scheduler._store_collection_job()
    assert calls == ['place']


def test_lost_job_lease_cannot_write_retry_or_overwrite_successor(db, scheduled, monkeypatch):
    change, _ = scheduled
    acknowledge(db, change)
    def stolen(candidate, client):
        db.get(MapJobLease, job.KEY).owner = 'successor'
        db.commit()
        return candidate['name'], [document(change)], 'a' * 64
    monkeypatch.setattr(lookup, 'lookup_place', stolen)
    with pytest.raises(RuntimeError, match='lease_lost'):
        job.run_due(db)
    assert db.get(MapJobLease, job.KEY).owner == 'successor'
    assert db.get(MapSourceState, f'place-recheck:{change.id}:279') is None


def test_expired_source_is_visible_as_blocked_without_lookup(db, scheduled):
    change, calls = scheduled
    acknowledge(db, change)
    db.get(MapSourceState, change.payload['source_key']).last_success_at = datetime.now(timezone.utc) - timedelta(days=8)
    db.commit()
    report = job.run_due(db)
    assert report['blocked_targets'][0]['reason'] == 'store_directory_observation_not_recent'
    assert not calls


def test_batch_limit_defers_remaining_targets_without_starvation(db, scheduled, monkeypatch):
    from copy import deepcopy
    from app.services.store_directory_review import identity_hash
    change, calls = scheduled
    payload = deepcopy(change.payload)
    row = {**payload['changes'][0]['after'], 'official_id': '280'}
    payload['changes'].append({'official_id': '280', 'kind': 'added', 'before': None, 'after': row})
    state = db.get(MapSourceState, payload['source_key'])
    state.records = [*state.records, row]
    payload['after_identity_hash'] = identity_hash(state.records)
    change.payload = payload
    db.commit()
    acknowledge(db, change)
    monkeypatch.setattr(settings, 'store_place_revalidation_batch_size', 1)
    first = job.run_due(db)
    assert first['due'] == 2 and first['deferred'] == 1 and len(calls) == 1
    second = job.run_due(db)
    assert second['due'] == 1 and second['deferred'] == 0 and len(calls) == 2
