"""HWP 5 기록 계층에서 표 셀을 복원한다. 미리보기는 전체 본문이 아니다."""
import io
import hashlib
import re
import struct
import zlib

from app.utils.file_parser import MAX_UNCOMPRESSED_BYTES, _hwp_para_text
from app.services import ocr


def embedded_blocks(ole, compressed, remaining):
    blocks, issues = [], []
    for entry in sorted(e for e in ole.listdir() if e[0] == "BinData"):
        location = "/".join(entry)
        try:
            if ole.get_size(entry) > remaining:
                raise ValueError("hwp_size_limit")
            data = ole.openstream(entry).read()
            signatures = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")
            if not data.startswith(signatures) and compressed:
                decoder = zlib.decompressobj(-15)
                data = decoder.decompress(data, remaining + 1)
                if decoder.unconsumed_tail or not decoder.eof:
                    raise ValueError("hwp_decompression_incomplete_or_limit")
            remaining -= len(data)
            if remaining < 0:
                raise ValueError("hwp_size_limit")
            if not data.startswith(signatures):
                issues.append({"location": location, "error": "hwp_unsupported_embedded_object"})
                continue
            image, issue = ocr.image_blocks(data)
            for block in image:
                block.update(location=f"{location}/{block['location']}", embedded_object=location)
                block.pop("page", None)  # Internal raster page is not an HWP document page.
            blocks.extend(image)
            issues.append({"location": location, "error": "hwp_image_placement_requires_review",
                           "ocr_blocks": len(image)})
            if issue:
                issues.append({"location": location, "error": issue})
        except Exception as exc:
            issues.append({"location": location, "error": str(exc)})
    return blocks, issues


def records(data):
    position = 0
    while position < len(data):
        if position + 4 > len(data):
            raise ValueError("truncated_hwp_record")
        header = struct.unpack_from("<I", data, position)[0]
        position += 4
        tag, level, size = header & 1023, (header >> 10) & 1023, header >> 20
        if size == 4095:
            if position + 4 > len(data):
                raise ValueError("truncated_hwp_record")
            size = struct.unpack_from("<I", data, position)[0]
            position += 4
        if position + size > len(data):
            raise ValueError("truncated_hwp_record")
        yield tag, level, data[position:position + size]
        position += size


def section_blocks(data, section):
    blocks, tables, structures = [], [], []
    for index, (tag, level, payload) in enumerate(records(data)):
        while tables and level <= tables[-1]["level"]:
            tables.pop()
        if tag == 71 and payload[:4] == b" lbt":
            structure = {"table": f"{section}/table:{index}", "method": "native", "cells": [], "issues": []}
            structures.append(structure)
            tables.append({"level": level, "location": structure["table"], "cell": None, "structure": structure})
        elif tag == 77 and tables and len(payload) >= 8:
            tables[-1]["rows"], tables[-1]["columns"] = struct.unpack_from("<HH", payload, 4)
            tables[-1]["structure"].update(rows=tables[-1]["rows"], columns=tables[-1]["columns"])
        elif tag == 72 and tables and tables[-1].get("rows"):
            table = tables[-1]
            table["cell"] = None
            # Modern HWP uses an 8-byte list header; older revisions use 6.
            candidates, bounded = [], []
            for offset in (8, 6):
                if len(payload) < offset + 26:
                    continue
                col, row, cs, rs = struct.unpack_from("<HHHH", payload, offset)
                if not (cs and rs and col + cs <= table["columns"] and row + rs <= table["rows"]):
                    continue
                bounded.append((offset, col, row, cs, rs))
                # LIST_HEADER alignment/flow flags use bits 0..6. A shifted
                # coordinate sequence can fit the grid but is not a valid header.
                properties = struct.unpack_from("<I", payload, offset - 4)[0]
                if properties & ~0x7f:
                    continue
                candidates.append((offset, col, row, cs, rs))
            proven = len({c[1:] for c in candidates}) == 1
            if not proven:
                table["structure"]["issues"].append("hwp_cell_geometry_ambiguous_or_invalid")
            # Retain the previous text reading for uncertain geometry; it cannot
            # authorize empty-cell recovery because the structure remains flagged.
            selected = candidates if proven else bounded
            if selected:
                offset, col, row, cs, rs = selected[0]
                location = f"{table['location']}/row:{row + 1}/cell:{col + 1}"
                metadata = {"table": table["location"], "row": row, "column": col,
                            "row_span": rs, "column_span": cs, "cell_location": location,
                            "record": index, "tag": tag, "level": level, "header_offset": offset,
                            "geometry_proven": proven,
                            "payload_sha256": hashlib.sha256(payload).hexdigest(),
                            "text_locations": [], "paragraph_char_counts": [], "has_control": False}
                table["structure"]["cells"].append(metadata)
                table["cell"] = {"table": table["location"], "row": row, "column": col,
                                 "row_span": rs, "column_span": cs, "cell_level": level, "metadata": metadata}
        elif tag == 66 and tables and tables[-1].get("cell") and len(payload) >= 4:
            tables[-1]["cell"]["metadata"]["paragraph_char_counts"].append(struct.unpack_from("<I", payload)[0] & 0x7fffffff)
        elif tag == 67:
            text = _hwp_para_text(payload).strip()
            if not text:
                continue
            cell = tables[-1].get("cell") if tables else None
            if cell and level > cell["cell_level"]:
                location = f"{cell['table']}/row:{cell['row'] + 1}/cell:{cell['column'] + 1}"
                cell["metadata"]["text_locations"].append(f"{location}/record:{index}")
                blocks.append({**{k: v for k, v in cell.items() if k not in ("cell_level", "metadata")},
                               "text": text, "location": f"{location}/record:{index}", "cell_location": location,
                               "kind": "cell", "method": "native"})
            else:
                blocks.append({"text": text, "location": f"{section}/record:{index}",
                               "kind": "paragraph", "method": "native"})
        # A textless cell containing an object is not a proven blank input.
        if tag == 71 or tag > 72:
            for table in tables:
                if table.get("cell") and level > table["cell"]["cell_level"]:
                    table["cell"]["metadata"]["has_control"] = True
    for structure in structures:
        for cell in structure["cells"]:
            cell["blank"] = (not cell["text_locations"] and not cell["has_control"] and
                             bool(cell["paragraph_char_counts"]) and all(n == 1 for n in cell["paragraph_char_counts"]))
        first = next((b for b in blocks if b.get("table") == structure["table"]), None)
        if first:
            first["table_structure"] = structure
    return blocks


def hwp_blocks(content):
    import olefile
    blocks, issues = [], [{"location": "document", "error": "hwp_layout_requires_validation"}]
    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        header = ole.openstream("FileHeader").read()
        props = struct.unpack_from("<I", header, 36)[0]
        if props & (2 | 4):
            return [], issues + [{"location": "document", "error": "hwp_encrypted_or_distribution_document"}]
        sections = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"
                           and re.fullmatch(r"Section\d+", e[1])), key=lambda e: int(e[1][7:]))
        total = 0
        for entry in sections:
            try:
                data = ole.openstream(entry).read()
                if props & 1:
                    decoder = zlib.decompressobj(-15)
                    data = decoder.decompress(data, MAX_UNCOMPRESSED_BYTES - total + 1)
                    if decoder.unconsumed_tail or not decoder.eof:
                        raise ValueError("hwp_decompression_incomplete_or_limit")
                total += len(data)
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("hwp_size_limit")
                blocks.extend(section_blocks(data, "/".join(entry)))
            except Exception as exc:
                issues.append({"location": "/".join(entry), "error": str(exc)})
        if any(e[0] == "BinData" for e in ole.listdir()):
            issues.append({"location": "BinData", "error": "hwp_embedded_objects_require_review"})
            image, image_issues = embedded_blocks(ole, bool(props & 1), MAX_UNCOMPRESSED_BYTES - total)
            blocks.extend(image)
            issues.extend(image_issues)
        if not blocks and ole.exists("PrvText"):
            text = ole.openstream("PrvText").read().decode("utf-16-le", errors="replace").strip()
            if text:
                blocks.append({"text": text, "location": "PrvText", "kind": "preview", "method": "native"})
                issues.append({"location": "PrvText", "error": "hwp_preview_only"})
    return blocks, issues
