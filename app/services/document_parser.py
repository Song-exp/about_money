"""첨부를 위치가 있는 블록으로 읽는다. 일부 페이지 실패를 전체 성공으로 숨기지 않는다."""
import io
import os
import re
import zipfile
from xml.etree import ElementTree as ET

from app.utils.file_parser import _docx_blocks, _guard_zip_size, extract_text_from_bytes
from app.services import ocr
from app.services.document_layout import finalize_blocks
from app.services.spreadsheet_parser import spreadsheet_blocks
from app.services.spreadsheet_inventory import spreadsheet_inventory
from app.services.hwp_layout import hwp_blocks
from app.services.pdf_reconciliation import reconcile_page

PARSER_VERSION = "blocks-v5"


def parse_document(content: bytes, filename: str) -> dict:
    blocks, issues, reconciliation = [], [], []
    inventory = None
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".docx":
        import docx
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _guard_zip_size(archive)
        document = docx.Document(io.BytesIO(content))
        blocks = _docx_blocks(document)
        for relation, part in document.part.related_parts.items():
            if part.content_type.startswith("image/"):
                image, issue = ocr.image_blocks(part.blob)
                for block in image:
                    block["location"] = f"document/image:{relation}/{block['location']}"
                blocks.extend(image)
                if issue:
                    issues.append({"location": f"document/image:{relation}", "error": issue})
    elif ext == ".hwpx":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _guard_zip_size(archive)
            sections = sorted((n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                              key=lambda n: int(re.search(r"section(\d+)", n).group(1)))
            def walk(node, path, context=None):
                local = node.tag.rsplit("}", 1)[-1]
                context = dict(context or {})
                if local == "tbl":
                    context = {"table": path}
                if local == "tc":
                    address = next((c for c in node if c.tag.rsplit("}", 1)[-1] == "cellAddr"), None)
                    span = next((c for c in node if c.tag.rsplit("}", 1)[-1] == "cellSpan"), None)
                    if address is not None:
                        context.update(row=int(address.get("rowAddr", 0)), column=int(address.get("colAddr", 0)),
                                       row_span=int(span.get("rowSpan", 1)) if span is not None else 1,
                                       column_span=int(span.get("colSpan", 1)) if span is not None else 1,
                                       cell_location=path)
                # 표 안에서는 각 셀의 문단을 독립적으로 처리한다.
                if local == "p" and not any(child.tag.rsplit("}", 1)[-1] == "tbl" for child in node.iter()):
                    text = "".join("".join(t.itertext()) for t in node.iter() if t.tag.rsplit("}", 1)[-1] == "t")
                    if text.strip():
                        blocks.append({**context, "text": text, "location": path, "kind": "paragraph", "method": "native"})
                    return
                counts = {}
                for child in node:
                    tag = child.tag.rsplit("}", 1)[-1]
                    counts[tag] = counts.get(tag, 0) + 1
                    walk(child, f"{path}/{tag}:{counts[tag]}", context)
            for section in sections:
                walk(ET.fromstring(archive.read(section)), section)
            for name in archive.namelist():
                if name.lower().startswith("bindata/") and name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")):
                    image, issue = ocr.image_blocks(archive.read(name))
                    for block in image:
                        block["location"] = f"{name}/{block['location']}"
                    blocks.extend(image)
                    if issue:
                        issues.append({"location": name, "error": issue})
    elif ext in (".xlsx", ".xls"):
        blocks, issues = spreadsheet_blocks(content, ext)
        inventory = spreadsheet_inventory(content, ext)
    elif ext == ".hwp":
        blocks, issues = hwp_blocks(content)
    elif ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        for number, page in enumerate(reader.pages, 1):
            try:
                text = (page.extract_text(extraction_mode="layout") or "") if "/Contents" in page else ""
                has_images = bool(page.images)
                native = None
                if text.strip():
                    native = {"text": text, "location": f"page:{number}", "page": number, "kind": "page", "method": "native"}
                    blocks.append(native)
                if has_images or not text.strip():
                    image, issue = ocr.pdf_page_blocks(content, number)
                    image, decisions = reconcile_page(content, number, native, image)
                    reconciliation.extend(decisions)
                    blocks.extend(image)
                    if any(d["reason"] == "native_ocr_region_disagreement" for d in decisions):
                        issues.append({"location": f"page:{number}", "error": "native_ocr_disagreement_requires_review"})
                    if issue:
                        issues.append({"location": f"page:{number}", "error": issue})
            except Exception as exc:
                issues.append({"location": f"page:{number}", "error": type(exc).__name__})
    else:
        text = extract_text_from_bytes(content, filename)
        blocks = [{"text": text, "location": "document", "kind": "body", "method": "native"}] if text.strip() else []
    if not blocks:
        issues.append({"location": "document", "error": "no_text_extracted"})
    finalize_blocks(blocks)
    return {**({'spreadsheet_inventory': inventory} if inventory is not None else {}),
            "blocks": blocks, "issues": issues, "parser_version": PARSER_VERSION,
            "reconciliation": reconciliation,
            "extracted_text": "\n".join(b["text"] for b in blocks)}
