"""python -m app.scripts.cleanup_expired --apply --report reports/cleanup.json

실행 전 DB 백업을 보관한다. 기본은 후보 조회이며 --apply에서만 삭제한다.
"""
import argparse
import json
import hashlib
from pathlib import Path
from datetime import datetime
from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.scholarship import Scholarship
from app.services.expired_cleanup import KST, remove_expired


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", required=True)
    parser.add_argument("--verified-deadlines", help="원본을 대조하여 확정한 마감일 JSON 배열")
    args = parser.parse_args()
    verified = []
    if args.verified_deadlines:
        verified_path = Path(args.verified_deadlines)
        verified = json.loads(verified_path.read_text(encoding="utf-8"))
        for item in verified:
            proof = verified_path.parent / item["proof_file"]
            if hashlib.sha256(proof.read_bytes()).hexdigest() != item["proof_sha256"]:
                raise ValueError("verified expiry proof checksum mismatch")
    with SessionLocal() as db:
        if args.apply:
            report = remove_expired(db, verified_deadlines=verified)
            report["verified_deadline_evidence"] = verified
        else:
            rows = db.scalars(select(Scholarship).where(Scholarship.deadline_at < datetime.now(KST))).all()
            report = {"candidates": [{"id": r.id, "title": r.title, "source_url": r.source_url} for r in rows]}
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"deleted": len(report.get("deleted", [])), "retained": len(report.get("retained_writing_history", [])), "detached_applications": report.get("detached_applications", 0), "report": str(path)}))


if __name__ == "__main__":
    main()
