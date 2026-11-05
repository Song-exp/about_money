import io
import zipfile

from app.core.config import settings
from app.services.source_assets import parse_asset


def archive_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_zip_preserves_parent_and_each_member():
    content = archive_bytes({"양식.txt": "학업 계획을 쓰세요.", "공지.txt": "신청 안내"})
    documents = parse_asset(content, "자료.zip", "https://x.test/file")
    assert len(documents) == 3
    assert documents[0]["kind"] == "archive"
    assert all(d["status"] == "parsed" for d in documents)
    assert documents[1]["parent_sha256"] == documents[2]["parent_sha256"]
    assert documents[1]["content"].decode("utf-8") == "학업 계획을 쓰세요."


def test_archive_image_and_nested_zip_remain_reviewable():
    content = archive_bytes({"공고.png": b"\x89PNG\r\n\x1a\nimage", "다른자료.zip": archive_bytes({"a.txt": "내용"})})
    documents = parse_asset(content, "자료.zip", "https://x.test/file")
    assert documents[1]["error"] == "ocr_required"
    assert documents[2]["error"] == "nested_archive_requires_review"
    assert all(d["content"] for d in documents)


def test_archive_limit_is_not_success(monkeypatch):
    monkeypatch.setattr(settings, "crawl_max_archive_members", 1)
    documents = parse_asset(archive_bytes({"a.txt": "a", "b.txt": "b"}), "자료.zip", "https://x.test/file")
    assert documents[0]["status"] == "needs_review"


def test_extensionless_plain_text_uses_content_type():
    document = parse_asset("지원동기".encode(), "download", "https://x.test/file", "text/plain; charset=utf-8")[0]
    assert document["extracted_text"] == "지원동기"
    assert document["detected_filename"].endswith(".txt")
