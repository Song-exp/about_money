"""공식 첨부의 형식 판별·압축 내부 원본 보존. OCR 대상은 성공으로 숨기지 않는다."""
import hashlib
import io
import os
import zipfile

from app.core.config import settings
from app.utils.file_parser import MAX_UNCOMPRESSED_BYTES, UnsupportedFileType
from app.services.document_parser import PARSER_VERSION, parse_document
from app.services import ocr


def parse_asset(content: bytes, filename: str, url: str, content_type: str = "") -> list[dict]:
    item = {"url": url, "filename": filename, "kind": "attachment", "content": content}
    ext = os.path.splitext(filename)[1].lower()
    if content.startswith(b"%PDF"):
        filename += ".pdf" if ext != ".pdf" else ""
    elif content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        import olefile
        with olefile.OleFileIO(io.BytesIO(content)) as ole:
            detected = ".xls" if ole.exists("Workbook") or ole.exists("Book") else ".hwp" if ole.exists("FileHeader") else None
        if not detected:
            item.update(status="needs_review", error="unsupported_ole_document")
            return [item]
        filename += detected if ext != detected else ""
    elif content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or content_type.startswith("image/"):
        blocks, issue = ocr.image_blocks(content)
        item.update(kind="image", blocks=blocks, method="ocr", extracted_text="\n".join(b["text"] for b in blocks),
                    parser_version=PARSER_VERSION, status="needs_review" if issue else "parsed")
        if issue:
            item.update(error="ocr_required", ocr_error=issue)
        return [item]
    elif content.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = [e for e in archive.infolist() if not e.is_dir()]
                if "word/document.xml" in archive.namelist():
                    filename += ".docx" if ext != ".docx" else ""
                elif "xl/workbook.xml" in archive.namelist():
                    filename += ".xlsx" if ext != ".xlsx" else ""
                elif any(e.filename.startswith("Contents/section") for e in entries):
                    filename += ".hwpx" if ext != ".hwpx" else ""
                else:
                    if len(entries) > settings.crawl_max_archive_members or sum(e.file_size for e in entries) > MAX_UNCOMPRESSED_BYTES:
                        raise ValueError("archive_limits_exceeded")
                    item.update(kind="archive", status="parsed", member_count=len(entries))
                    if not entries:
                        item.update(status="needs_review", error="empty_archive")
                    results = [item]
                    for entry in entries:
                        if entry.file_size > settings.crawl_max_document_mb * 1024 * 1024:
                            results.append({"url": url, "filename": entry.filename, "kind": "archive_member",
                                            "status": "needs_review", "error": "member_size_exceeded"})
                            continue
                        member = archive.read(entry)
                        native_zip = False
                        if member.startswith(b"PK"):
                            try:
                                with zipfile.ZipFile(io.BytesIO(member)) as nested:
                                    names = nested.namelist()
                                    native_zip = "word/document.xml" in names or "xl/workbook.xml" in names or any(n.startswith("Contents/section") for n in names)
                            except zipfile.BadZipFile:
                                pass
                        if member.startswith(b"PK") and not native_zip:
                            child = {"url": url, "filename": entry.filename, "content": member,
                                     "status": "needs_review", "error": "nested_archive_requires_review"}
                        else:
                            child = parse_asset(member, entry.filename, url)[0]
                        child.update(kind="archive_member", parent_sha256=hashlib.sha256(content).hexdigest(), member_path=entry.filename)
                        results.append(child)
                    return results
        except Exception as exc:
            item.update(status="needs_review", error=type(exc).__name__)
            return [item]
    elif content_type.split(";")[0].strip() == "text/plain" and not ext:
        filename += ".txt"
    item["detected_filename"] = filename
    try:
        parsed = parse_document(content, filename)
        item.update(parsed)
        text = parsed["extracted_text"]
        if not text.strip():
            item.update(status="needs_review", error="no_text_extracted")
        else:
            item.update(status="needs_review" if parsed["issues"] else "parsed", extracted_text=text)
            if parsed["issues"]:
                item["error"] = parsed["issues"][0]["error"]
    except UnsupportedFileType:
        item.update(status="needs_review", error="UnsupportedFileType")
    except Exception as exc:
        item.update(status="needs_review", error=type(exc).__name__)
    return [item]
