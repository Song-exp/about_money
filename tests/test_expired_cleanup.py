import uuid
from datetime import datetime, timedelta
from sqlalchemy import select
from app.models.application import ApplicationDocument, ApplicationSource, UserApplication
from app.models.scholarship import Scholarship, PostingStatus
from app.models.writing_session import WritingSession
from app.models.user import User
from app.services.expired_cleanup import remove_expired, KST
from app.services.scholarship_history import record_revision
from tests.conftest import make_scholarship


def test_delete_expired_only_and_preserve_application_documents(db):
    now = datetime.now(KST)
    past = make_scholarship(title="expired", deadline_at=now - timedelta(days=1))
    future = make_scholarship(title="future", deadline_at=now + timedelta(days=1))
    unknown = make_scholarship(title="unknown", deadline_at=None)
    db.add_all([past, future, unknown]); db.flush()
    application = UserApplication(user_id=uuid.uuid4(), scholarship_id=past.id, scholarship_name="original name", source=ApplicationSource.GENERATED)
    application.documents.append(ApplicationDocument(content_text="user content"))
    db.add(application); db.commit()
    report = remove_expired(db, now)
    db.expire_all()
    assert len(report["deleted"]) == 1 and report["detached_applications"] == 1
    assert db.get(Scholarship, past.id) is None
    assert {r.title for r in db.scalars(select(Scholarship))} == {"future", "unknown"}
    db.refresh(application)
    assert application.scholarship_id is None and application.scholarship_name == "original name"
    assert application.documents[0].content_text == "user content"
    assert remove_expired(db, now)["deleted"] == []


def test_writing_session_references_are_retained_closed(db):
    now = datetime.now(KST)
    row = make_scholarship(title="referenced", deadline_at=now - timedelta(days=1))
    user = User(id=uuid.uuid4())
    db.add_all([row, user]); db.flush()
    revision = record_revision(db, row)
    db.add(WritingSession(user_id=user.id, scholarship_id=row.id, revision_id=revision.id, form_fingerprint="a" * 64))
    db.commit()
    report = remove_expired(db, now)
    assert report["deleted"] == [] and len(report["retained_writing_history"]) == 1
    db.refresh(row)
    assert row.status == PostingStatus.CLOSED


def test_verified_expiry_requires_current_url_and_expired_aware_time(db):
    import pytest
    row = make_scholarship(title="missing date", deadline_at=None)
    db.add(row); db.commit()
    now = datetime.now(KST)
    entry = {"id": row.id, "source_url": row.source_url, "deadline_at": (now - timedelta(days=1)).isoformat()}
    with pytest.raises(ValueError):
        remove_expired(db, now, [{**entry, "source_url": "wrong"}])
    with pytest.raises(ValueError):
        remove_expired(db, now, [{**entry, "deadline_at": (now + timedelta(days=1)).isoformat()}])
    report = remove_expired(db, now, [entry])
    assert len(report["deleted"]) == 1
