"""Audited K-pass references and their collection state, without URL guessing."""
import hashlib
from datetime import datetime,timedelta,timezone
from pathlib import Path

from sqlalchemy import select
from app.models.card_collection import CardCollectionSource,CardCollectionAttempt,CardSourceSnapshot
from app.models.card_document_text import CardDocumentText
from app.services.toss_kpass_document import parse_kpass_document,KPASS_URL
from app.services.map_verification import aware

ROOT='https://api.tossbank.com/api-public/disclosure/attachment/document/document-no/'
DOCUMENTS={
    '60303005':{'label':'토스뱅크 체크카드 상품안내서','role':'product_guide','url':ROOT+'60303005'},
    '60303001':{'label':'체크카드 개인회원 약관','role':'membership_terms','url':ROOT+'60303001'},
}
DOCUMENT_URLS={row['url'] for row in DOCUMENTS.values()}


def linked_document_status(db,snapshot,derived,*,archive_root=None,now=None):
    from app.services.card_collection import ARCHIVE_ROOT
    from app.services.card_document_crosscheck import TEXT_PARSER
    now=aware(now or datetime.now(timezone.utc))
    parent_source=db.get(CardCollectionSource,snapshot.source_id)
    if not parent_source or parent_source.canonical_url!=KPASS_URL:
        return {'status':'parent_identity_unresolved','documents':[]}
    root=Path(archive_root or ARCHIVE_ROOT).resolve()
    path=(root/snapshot.raw_object_ref).resolve()
    if not path.is_relative_to(root): return {'status':'archive_unavailable','documents':[]}
    try:
        raw=path.read_bytes()
    except OSError:
        return {'status':'archive_unavailable','documents':[]}
    if (derived.snapshot_id != snapshot.id or hashlib.sha256(raw).hexdigest()!=snapshot.raw_sha256 or derived.raw_sha256!=snapshot.raw_sha256
            or hashlib.sha256(derived.text.encode()).hexdigest()!=derived.text_sha256):
        return {'status':'integrity_failure','documents':[]}
    try:
        parsed=parse_kpass_document(raw)
    except ValueError:
        return {'status':'reference_parse_failed','documents':[]}
    if parsed['text_sha256']!=derived.text_sha256:
        return {'status':'derived_text_mismatch','documents':[]}
    rows=[]
    for reference in parsed['references']:
        item={'label':reference['label'],'document_identifier':reference['target'],
              'reference_pointer':reference['json_pointer'],'parent_snapshot_id':snapshot.id}
        spec=DOCUMENTS.get(reference['target'])
        if not spec or spec['label']!=reference['label']:
            rows.append({**item,'status':'unconfigured_reference'})
            continue
        item.update(source_url=spec['url'],document_role=spec['role'])
        source=db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url==spec['url']))
        if not source:
            rows.append({**item,'status':'source_not_registered'})
            continue
        item.update(source_id=source.id,error_code=source.last_error)
        if (not parent_source.issuer_id or source.issuer_id!=parent_source.issuer_id
                or (source.schedule_policy or {}).get('document_identifier')!=reference['target']):
            rows.append({**item,'status':'document_identity_unresolved'})
            continue
        item['manual_document'] = manual_document_status(db, source, root)
        if not source.enabled or source.last_error:
            rows.append({**item,'status':'collection_blocked' if source.last_error=='source_disallows_fetch' else 'source_unavailable'})
            continue
        observed=db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id==source.id,
            CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
        document=db.get(CardSourceSnapshot,observed.snapshot_id) if observed else None
        if not document:
            rows.append({**item,'status':'document_missing'})
            continue
        if document.source_id!=source.id:
            rows.append({**item,'status':'document_identity_unresolved'})
            continue
        full=db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id==source.id,
            CardCollectionAttempt.snapshot_id==document.id,CardCollectionAttempt.status=='captured',
            CardCollectionAttempt.http_status==200).order_by(CardCollectionAttempt.id.desc()).limit(1))
        text=db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id==document.id,
            CardDocumentText.parser_version==TEXT_PARSER))
        if not full or not now-timedelta(days=7)<=aware(full.attempted_at)<=now:
            status='document_stale'
        elif not text:
            status='reparse_required'
        elif text.raw_sha256!=document.raw_sha256 or hashlib.sha256(text.text.encode()).hexdigest()!=text.text_sha256:
            status='document_text_changed'
        elif not archived_document_intact(root, document):
            status='document_archive_integrity_failure'
        else:
            status='ready_for_crosscheck'
        rows.append({**item,'status':status,'snapshot_id':document.id,'text_id':text.id if text else None,
                     'raw_sha256':document.raw_sha256})
    ready=bool(rows) and all(row['status']=='ready_for_crosscheck' for row in rows)
    return {'status':'ready_for_crosscheck' if ready else 'collection_pending','documents':rows,
            'crosscheck_completed':False,'public_approval':False,
            'required_action':'수집 정책이 허용하는 공식 경로 또는 별도로 제공된 공식 문서를 확보한 뒤 적용 관계를 대조해야 합니다.'}


def archived_document_intact(root, snapshot):
    root = Path(root).resolve()
    path = (root / snapshot.raw_object_ref).resolve()
    try:
        return path.is_relative_to(root) and hashlib.sha256(path.read_bytes()).hexdigest() == snapshot.raw_sha256
    except OSError:
        return False


def manual_document_status(db, source, archive_root):
    """Manual evidence remains separate even while HTTP collection is blocked."""
    from app.models.card_document_import import CardDocumentImport
    from app.services.card_document_crosscheck import TEXT_PARSER
    record = db.scalar(select(CardDocumentImport).join(CardSourceSnapshot,
        CardDocumentImport.snapshot_id == CardSourceSnapshot.id).where(
        CardSourceSnapshot.source_id == source.id).order_by(CardDocumentImport.id.desc()).limit(1))
    if record is None:
        return None
    snapshot = db.get(CardSourceSnapshot, record.snapshot_id)
    text = db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id == snapshot.id,
        CardDocumentText.parser_version == TEXT_PARSER))
    root = Path(archive_root).resolve()
    path = (root / snapshot.raw_object_ref).resolve()
    try:
        intact = (path.is_relative_to(root) and hashlib.sha256(path.read_bytes()).hexdigest() == snapshot.raw_sha256
            and text is not None and text.raw_sha256 == snapshot.raw_sha256
            and hashlib.sha256(text.text.encode()).hexdigest() == text.text_sha256)
    except OSError:
        intact = False
    return {'import_id': record.id, 'snapshot_id': snapshot.id, 'raw_sha256': snapshot.raw_sha256,
            'review_pack_url': f'/admin/map/card-document-imports/{record.id}/review-pack',
            'declared_title': record.declared_title, 'declared_revision': record.declared_revision,
            'obtained_at': aware(record.obtained_at).isoformat(),
            'status': 'manual_identity_review_required' if intact else 'integrity_failure',
            'metadata_verified': False, 'crosscheck_completed': False, 'public_approval': False}
