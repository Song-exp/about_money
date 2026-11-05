"""공고 버전 접근과 스키마 생성·되돌리기 검증."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect

from app.services.scholarship_history import record_revision
from tests.conftest import make_scholarship


def test_revision_admin_endpoint_keeps_old_values(client, db):
    scholarship = make_scholarship(title="기존 제목", benefit={"amount_krw": 100000})
    db.add(scholarship)
    record_revision(db, scholarship)
    scholarship.title = "수정 제목"
    record_revision(db, scholarship)
    db.commit()
    url = f"/admin/crawl/scholarships/{scholarship.id}/revisions"
    assert client.get(url).status_code in (401, 403)
    response = client.get(url, headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert [row["snapshot"]["title"] for row in response.json()] == ["수정 제목", "기존 제목"]


def test_revision_migration_roundtrip():
    migration = importlib.import_module("migrations.versions.b42fa7d19208_add_scholarship_revisions")
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("scholarships", metadata, Column("id", Integer, primary_key=True))
    with engine.begin() as connection:
        metadata.create_all(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "scholarship_revisions" in inspect(connection).get_table_names()
            migration.downgrade()
            assert inspect(connection).get_table_names() == ["scholarships"]
    engine.dispose()
