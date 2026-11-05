"""On-demand operational alerts from existing records, without state changes."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.card_collection import CardCollectionSource, CardCollectionAttempt, CardCollectionRun
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitExtraction
from app.models.card_publication import CardProgramPublication
from app.services.card_benefit_parsers import PARSERS
from app.services.card_publication import publication_status, program_review_status
from app.services.map_verification import aware


def card_operations(db, *, now=None, limit=100, offset=0):
    now = aware(now or datetime.now(timezone.utc))
    alerts = []
    def add(key, priority, kind, reasons, **details):
        alerts.append({'key':key,'priority':priority,'kind':kind,'reasons':reasons,**details})
    sources = list(db.scalars(select(CardCollectionSource).order_by(CardCollectionSource.id)))
    active = {s.id:s for s in sources if s.enabled}
    for source in active.values():
        reasons = []
        priority = 2
        if source.last_error:
            reasons.append('source_blocked' if source.last_error=='source_disallows_fetch' else 'collection_failed')
            if source.failure_count >= 3 and source.last_error!='source_disallows_fetch':
                reasons.append('repeated_collection_failure')
                priority = 1
        due = max((aware(t) for t in (source.next_check_at,source.next_retry_at) if t),default=None)
        if due is not None and due < now-timedelta(minutes=15):
            reasons.append('collection_overdue')
            priority = min(priority,1)
        if not source.last_attempt_at:
            reasons.append('never_collected')
        if reasons:
            add(f'source:{source.id}',priority,'source',reasons,source_id=source.id,
                source_url=source.canonical_url,error_code=source.last_error,
                error_category=('environment' if source.last_error in {'network_access_denied','proxy_connection_failed'} else
                    'configuration' if source.last_error=='robots_redirect_requires_configuration' else
                    'policy' if source.last_error=='source_disallows_fetch' else 'source'),
                failure_count=source.failure_count,next_attempt_at=due.isoformat() if due else None)

    # Each program appears once. Observation recency is independent of immutable
    # snapshot IDs and extraction timestamps, including source reversion.
    candidates = {}
    for source in active.values():
        parser = PARSERS.get(source.canonical_url)
        if not parser:
            continue
        observed = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id==source.id,
            CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
        versions = list(db.scalars(select(CardBenefitVersion).where(CardBenefitVersion.source_id==source.id,
            CardBenefitVersion.extractor_version==parser[0]).order_by(CardBenefitVersion.created_at.desc(),CardBenefitVersion.id.desc())))
        version = next((v for v in versions if observed and v.snapshot_id==observed.snapshot_id),versions[0] if versions else None)
        extraction = db.scalar(select(CardBenefitExtraction).where(CardBenefitExtraction.source_id==source.id,
            CardBenefitExtraction.snapshot_id==observed.snapshot_id,CardBenefitExtraction.extractor_version==parser[0])
            .order_by(CardBenefitExtraction.id.desc()).limit(1)) if observed else None
        if observed and (not extraction or extraction.status=='failed'):
            add(f'extraction:{source.id}',1,'extraction',
                ['extraction_failed' if extraction else 'extraction_missing'],source_id=source.id,
                source_url=source.canonical_url,version_id=version.id if version else None,
                snapshot_id=observed.snapshot_id)
        if version:
            candidates[version.program_key]=version

    publications = list(db.scalars(select(CardProgramPublication)))
    published = {p.program_key:p for p in publications}
    for key in sorted(set(candidates) | set(published)):
        publication = published.get(key)
        version = candidates.get(key) or (db.get(CardBenefitVersion,publication.version_id) if publication else None)
        status = publication_status(db,publication,now) if publication else 'not_published'
        reasons, priority = [], 2
        if publication and publication.status=='approved':
            if status!='approved':
                priority=0
                reasons.append('published_program_unavailable')
            elif aware(publication.recheck_at) <= now+timedelta(hours=2):
                priority=1
                reasons.append('review_due_soon')
        readiness = program_review_status(db,version,now=now) if version else None
        if readiness:
            if readiness['supporting_document'].get('required') and readiness['supporting_document']['status']=='conflict':
                reasons.append('supporting_document_conflict')
                priority=min(priority,1)
            if publication and version.id!=publication.version_id:
                reasons.append('candidate_differs_from_publication')
                priority=min(priority,1)
            if not publication or version.id!=publication.version_id:
                reasons.append('pending_review' if readiness['can_approve'] else 'pending_policy_resolution')
        if reasons:
            add(f'program:{key}',priority,'program',reasons,program_key=key,
                version_id=version.id if version else None,published_version_id=publication.version_id if publication else None,
                public_status=status,approval_blocker=readiness.get('blocker') if readiness else 'version_missing',
                recheck_at=aware(publication.recheck_at).isoformat() if publication else None)

    latest_run = db.scalar(select(CardCollectionRun).order_by(CardCollectionRun.started_at.desc(),CardCollectionRun.id.desc()).limit(1))
    if latest_run and (latest_run.status in {'failed','degraded'} or
            latest_run.status=='running' and aware(latest_run.started_at) < now-timedelta(minutes=75)):
        add('pipeline:latest_run',1,'pipeline',
            ['collection_run_stalled' if latest_run.status=='running' else 'collection_run_'+latest_run.status],
            run_id=latest_run.id)
    alerts.sort(key=lambda row:(row['priority'],row['key']))
    counts = {f'p{level}':sum(row['priority']==level for row in alerts) for level in range(3)}
    return {'scope':'admin_read_only_operational_alerts','checked_at':now.isoformat(),
            'notification_channel':'admin_dashboard_only','total':len(alerts),'counts':counts,
            'alerts':alerts[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(alerts) else None,
            'sources':{'enabled':len(active),'disabled':len(sources)-len(active)},
            'latest_run':{'id':latest_run.id,'status':latest_run.status,
                          'started_at':aware(latest_run.started_at).isoformat()} if latest_run else None,
            'review_policy':'P0 게시 기록의 현재 이용 불가, P1 재검토·문서 충돌·반복 실패, P2 신규 검토·일반 수집 확인'}
