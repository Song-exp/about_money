"""Read-only review differences. A diff is never evidence of eligibility."""
from sqlalchemy import select, or_, and_

from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence
from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.models.card_publication import CardProgramPublication

KEYED = {'/rules':'rule_key', '/limit_groups':'group_key', '/spend_tiers':'tier_key'}
MISSING = object()


def payload_changes(before, after, limit=100):
    changes = []
    truncated = False
    def record(path, old, new):
        nonlocal truncated
        if len(changes) >= limit:
            truncated = True
            return
        changes.append({'path':path or '/', 'kind':'added' if old is MISSING else 'removed' if new is MISSING else 'changed',
                        'before':None if old is MISSING else old,'after':None if new is MISSING else new})
    def walk(old,new,path):
        if old is MISSING or new is MISSING:
            record(path,old,new)
        elif isinstance(old,dict) and isinstance(new,dict):
            for key in sorted(old.keys() | new.keys()):
                part = str(key).replace('~','~0').replace('/','~1')
                walk(old.get(key,MISSING),new.get(key,MISSING),path+'/'+part)
        elif isinstance(old,list) and isinstance(new,list) and path in KEYED:
            key = KEYED[path]
            def index(rows):
                if not all(isinstance(row,dict) and isinstance(row.get(key),str) for row in rows): return None
                result = {row[key]:row for row in rows}
                return result if len(result)==len(rows) else None
            left,right = index(old),index(new)
            if left is None or right is None:
                if old != new: record(path,old,new)
                return
            if list(left) != list(right): record(path+'/$order',list(left),list(right))
            for identity in sorted(left.keys() | right.keys()):
                part = identity.replace('~','~0').replace('/','~1')
                walk(left.get(identity,MISSING),right.get(identity,MISSING),path+'/'+part)
        elif type(old) is not type(new) or old != new:
            record(path,old,new)
    walk(before,after,'')
    return {'fields':changes,'truncated':truncated,'limit':limit}


def program_changes(db, version):
    """Prefer the approved pointer; otherwise compare a preceding extraction.

    Immutable snapshot IDs do not indicate the latest observed source. In
    particular, a reverted source can refer to an older snapshot and version.
    """
    publication = db.get(CardProgramPublication,version.program_key)
    baseline = db.get(CardBenefitVersion,publication.version_id) if publication else None
    kind = 'publication_pointer' if baseline else 'previous_extraction'
    if baseline and (baseline.source_id != version.source_id or baseline.program_key != version.program_key):
        return {'status':'baseline_identity_mismatch','review_required':True}
    if baseline is None:
        baseline = db.scalar(select(CardBenefitVersion).where(
            CardBenefitVersion.program_key==version.program_key,CardBenefitVersion.source_id==version.source_id,
            or_(CardBenefitVersion.created_at < version.created_at,
                and_(CardBenefitVersion.created_at==version.created_at,CardBenefitVersion.id < version.id)))
            .order_by(CardBenefitVersion.created_at.desc(),CardBenefitVersion.id.desc()).limit(1))
    observed = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id==version.source_id,
        CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
    common = {'review_required':True,'target_version_id':version.id,
              'latest_observed_snapshot_id':observed.snapshot_id if observed else None,
              'target_matches_latest_observation':observed.snapshot_id==version.snapshot_id if observed else None}
    if baseline is None:
        return {**common,'status':'no_baseline','baseline':None}
    before = db.get(CardSourceSnapshot,baseline.snapshot_id)
    after = db.get(CardSourceSnapshot,version.snapshot_id)
    if not before or not after:
        return {**common,'status':'snapshot_missing'}
    def evidence(row):
        return {item.field_path:item.quote for item in db.scalars(select(CardBenefitFieldEvidence).where(
            CardBenefitFieldEvidence.version_id==row.id))}
    return {**common,'status':'same_version' if baseline.id==version.id else 'compared',
            'baseline':{'kind':kind,'version_id':baseline.id,'payload_hash':baseline.payload_hash,
                        'pointer_status':publication.status if publication else None},
            'target_payload_hash':version.payload_hash,
            'payload':payload_changes(baseline.payload,version.payload),
            'evidence_quotes':payload_changes(evidence(baseline),evidence(version)),
            'source':{'raw_changed':before.raw_sha256!=after.raw_sha256,
                      'text_changed':before.semantic_sha256!=after.semantic_sha256,
                      'before_snapshot_id':before.id,'after_snapshot_id':after.id},
            'extractor_changed':baseline.extractor_version!=version.extractor_version,
            'note':'원문·추출 조건·근거의 차이만 표시합니다. 변경 없음도 재승인을 대신하지 않습니다.'}
