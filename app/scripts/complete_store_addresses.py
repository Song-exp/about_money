"""Preview or apply full roads already supported by recent exact-place evidence."""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.store import Store
from app.services.store_address_completion import address_completion


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    backup=None
    if args.apply:
        if settings.is_production or engine.dialect.name!='sqlite':
            raise RuntimeError('local_sqlite_only')
        backup=Path('_backup/map-address-review')/f'before-completion-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
        backup.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(engine.url.database) as source,sqlite3.connect(backup) as target:
            source.backup(target)
    with SessionLocal() as db:
        result=[address_completion(db,store,apply=args.apply) for store in db.scalars(select(Store).order_by(Store.id))]
        if args.apply:
            db.commit()
    summary={'applied':args.apply,'backup':str(backup) if backup else None,
        'ready':sum(r['status']=='ready' for r in result),'completed':sum(r['status']=='address_completed' for r in result),
        'pending':sum(r['status']=='pending' for r in result),'items':result}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k!='items'},ensure_ascii=False))


if __name__=='__main__':
    main()
