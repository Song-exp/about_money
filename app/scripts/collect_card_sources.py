"""Register audited candidates and explicitly collect due official documents."""
import argparse
import json
from pathlib import Path
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models.card_collection import COLLECTION_MODELS, CardCollectionSource, CardCollectionAttempt
from app.models.card_benefit_version import BENEFIT_VERSION_MODELS
from app.services.card_collection import bootstrap_candidates, collect_due, utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--register-candidates', action='store_true')
    parser.add_argument('--register-discovery', action='store_true')
    parser.add_argument('--register-documents', action='store_true')
    parser.add_argument('--reparse-documents', action='store_true')
    parser.add_argument('--document-review-output', type=Path)
    parser.add_argument('--collect-due', action='store_true')
    parser.add_argument('--source-id', type=int, action='append', help='Only collect due sources with these IDs; does not bypass retry time')
    parser.add_argument('--extract-stored', action='store_true')
    parser.add_argument('--review-output', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.source_id and not args.collect_due:
        parser.error('--source-id requires --collect-due')
    if args.review_output and not args.extract_stored:
        parser.error('--review-output requires --extract-stored')
    if args.document_review_output and not args.reparse_documents:
        parser.error('--document-review-output requires --reparse-documents')
    with SessionLocal() as db:
        result = {}
        if args.register_candidates:
            result['registration'] = bootstrap_candidates(db)
            db.commit()
        if args.register_discovery:
            from app.services.card_discovery import bootstrap_discovery
            result['discovery_registration'] = bootstrap_discovery(db)
            db.commit()
        if args.register_documents:
            from app.services.card_collection import bootstrap_supporting_documents
            result['supporting_documents'] = bootstrap_supporting_documents(db)
            db.commit()
        if args.reparse_documents:
            from app.models.card_collection import CardSourceSnapshot
            from app.models.card_benefit_version import CardBenefitVersion
            from app.services.card_collection import ARCHIVE_ROOT
            from app.services.card_document_crosscheck import DAY_LEAFLET_URL, DEEP_OIL_DISCLOSURE_URL, prepare_document_text, document_checks
            from app.services.toss_kpass_document import KPASS_URL
            from app.services.card_linked_documents import DOCUMENT_URLS
            reparsed=[]
            for source in db.scalars(select(CardCollectionSource).where(CardCollectionSource.canonical_url.in_(
                    [DAY_LEAFLET_URL, DEEP_OIL_DISCLOSURE_URL,KPASS_URL,*DOCUMENT_URLS]))):
                observed = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id,
                    CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
                snapshot = db.get(CardSourceSnapshot, observed.snapshot_id) if observed else None
                if snapshot:
                    derived=prepare_document_text(db, source, snapshot, (ARCHIVE_ROOT / snapshot.raw_object_ref).read_bytes())
                    reparsed.append({'source_id':source.id,'snapshot_id':snapshot.id,'text_id':derived.id,
                        'parser':derived.parser_version,'characters':len(derived.text)})
                    db.commit()
            result['document_texts']=reparsed
            checks = [{'version_id': version.id, **document_checks(db, version)} for version in db.scalars(
                select(CardBenefitVersion).where(CardBenefitVersion.program_key.in_(['hana_day_offline','shinhan_deep_oil_living'])))]
            result['document_checks'] = [{'version_id': row['version_id'], 'status': row['status'],
                'conflicts': row.get('conflicts', [])} for row in checks]
            if args.document_review_output:
                args.document_review_output.parent.mkdir(parents=True, exist_ok=True)
                args.document_review_output.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
        if args.collect_due:
            result['collection'] = collect_due(db,only_source_ids=args.source_id)
        if args.extract_stored:
            from app.services.card_benefit_extraction import extract_stored_sources
            result['extraction'] = extract_stored_sources(db)
            db.commit()
            if args.review_output:
                from app.services.card_benefit_extraction import review_pack
                pack = review_pack(db, [row['version_id'] for row in result['extraction'] if row.get('version_id')])
                args.review_output.parent.mkdir(parents=True, exist_ok=True)
                args.review_output.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding='utf-8')
                result['review_output'] = str(args.review_output)
        result['counts'] = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                            for model in (*COLLECTION_MODELS, *BENEFIT_VERSION_MODELS)}
        from app.models.card_document_text import CardDocumentText
        result['counts']['card_document_texts'] = db.scalar(select(func.count()).select_from(CardDocumentText))
        result['sources'] = [{'url': row.canonical_url, 'error': row.last_error,
            'next_check_at': utc(row.next_check_at).isoformat() if row.next_check_at else None,
            'http_status': db.scalar(select(CardCollectionAttempt.http_status)
                .where(CardCollectionAttempt.source_id == row.id).order_by(CardCollectionAttempt.id.desc()).limit(1))}
            for row in db.scalars(select(CardCollectionSource).order_by(CardCollectionSource.id))]
        result['scope'] = 'source_archive_and_pending_extraction_no_benefit_approval'
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
