"""Independent byte/record inventory for review, never a semantic accuracy gold.

This reader deliberately does not import the production HWP layout/text parser.
It enumerates controls and lists even when they cannot be classified as cells.
"""
import hashlib
import io
import re
import struct
import zlib
from collections import Counter

import olefile

LIMIT = 32 * 1024 * 1024
WIDE_CONTROLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})


def record_inventory(data):
    if len(data) > LIMIT:
        raise ValueError("source_record_size_limit")
    position, result, ancestors = 0, [], []
    while position < len(data):
        start = position
        if len(data) - position < 4:
            raise ValueError("truncated_source_record_header")
        header = int.from_bytes(data[position:position + 4], "little")
        position += 4
        tag, level, size = header & 0x3ff, (header >> 10) & 0x3ff, header >> 20
        if size == 0xfff:
            if len(data) - position < 4:
                raise ValueError("truncated_source_extended_size")
            size = int.from_bytes(data[position:position + 4], "little")
            position += 4
        if size > len(data) - position:
            raise ValueError("truncated_source_record_payload")
        payload = data[position:position + size]
        position += size
        while ancestors and ancestors[-1]["level"] >= level:
            ancestors.pop()
        item = {"record": len(result), "offset": start, "end_offset": position, "tag": tag, "level": level, "size": size,
                "payload_sha256": hashlib.sha256(payload).hexdigest(), "control_parent": ancestors[-1]["record"] if ancestors else None,
                "table_parent": next((a["record"] for a in reversed(ancestors) if a["control_id"] == " lbt"), None)}
        if tag == 71:
            item["control_id"] = payload[:4].decode("ascii", errors="backslashreplace")
            ancestors.append(item)
        result.append({**item, "payload": payload})
    return result


def paragraph_text(data):
    if len(data) % 2:
        raise ValueError("invalid_source_utf16_length")
    cursor, pieces = 0, []
    while cursor < len(data):
        code = int.from_bytes(data[cursor:cursor + 2], "little")
        if code >= 32:
            pieces.append(data[cursor:cursor + 2])
            cursor += 2
        elif code in WIDE_CONTROLS:
            if cursor + 16 > len(data):
                raise ValueError("truncated_source_text_control")
            if code == 9:
                pieces.append(b"\t\x00")
            cursor += 16
        else:
            pieces.append(b"\n\x00" if code in (10, 13) else b" \x00")
            cursor += 2
    return b"".join(pieces).decode("utf-16-le").strip()


def inflate(data, compressed):
    if len(data) > LIMIT:
        raise ValueError("source_stream_size_limit")
    if not compressed:
        return data, {"compressed": False, "unused_tail_bytes": 0}
    decoder = zlib.decompressobj(-15)
    result = decoder.decompress(data, LIMIT + 1)
    if len(result) > LIMIT or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("source_decompression_incomplete_or_limit")
    # HWP streams can have bytes after the deflate end. Preserve their accounting
    # instead of silently losing them or inventing a semantic interpretation.
    return result, {"compressed": True, "unused_tail_bytes": len(decoder.unused_data),
                    "unused_tail_sha256": hashlib.sha256(decoder.unused_data).hexdigest(),
                    "tail_review_status": "pending" if decoder.unused_data else "none"}


def section_inventory(data, section):
    raw = record_inventory(data)
    dimensions = {r["table_parent"]: struct.unpack_from("<HH", r["payload"], 4) for r in raw
                  if r["tag"] == 77 and r["table_parent"] is not None and len(r["payload"]) >= 8}
    texts, lists, controls = [], [], []
    for record in raw:
        proof = {k: v for k, v in record.items() if k != "payload"}
        proof["location"] = f"{section}/record:{record['record']}"
        payload = record["payload"]
        if record["tag"] == 67:
            text = paragraph_text(payload)
            if text:
                texts.append({**proof, "text": text})
        if record["tag"] == 71:
            controls.append({**proof, "visual_review_status": "pending"})
        if record["tag"] == 72:
            candidates = []
            rows, columns = dimensions.get(record["table_parent"], (0, 0))
            for offset in (8, 6):
                if len(payload) < offset + 26:
                    continue
                col, row, width, height = struct.unpack_from("<HHHH", payload, offset)
                flags = struct.unpack_from("<I", payload, offset - 4)[0]
                if width and height and col + width <= columns and row + height <= rows and not flags & ~0x7f:
                    candidates.append({"header_offset": offset, "row": row, "column": col, "row_span": height, "column_span": width})
            unique = {(c["row"], c["column"], c["row_span"], c["column_span"]) for c in candidates}
            kind = "cell_geometry_candidate" if len(unique) == 1 else "ambiguous_table_list" if candidates else "table_list_without_proven_geometry" if record["table_parent"] is not None else "non_table_list"
            children = []
            for child in raw[record["record"] + 1:]:
                if child["level"] < record["level"] or child["tag"] == 72 and child["level"] == record["level"]:
                    break
                children.append(child)
            counts = [int.from_bytes(c["payload"][:4], "little") & 0x7fffffff for c in children if c["tag"] == 66 and len(c["payload"]) >= 4]
            content = [c["record"] for c in children if c["tag"] == 67 and paragraph_text(c["payload"])]
            controls_present = any(c["level"] > record["level"] and (c["tag"] == 71 or c["tag"] > 72) for c in children)
            lists.append({**proof, "kind": kind, "geometry_candidates": candidates, "semantic_review_status": "pending",
                          "content_records": content, "paragraph_char_counts": counts, "has_control": controls_present,
                          "blank_candidate": kind == "cell_geometry_candidate" and not content and not controls_present and bool(counts) and all(n == 1 for n in counts)})
    return {"section": section, "stream_sha256": hashlib.sha256(data).hexdigest(), "record_count": len(raw),
            "records": [{k: v for k, v in r.items() if k != "payload"} for r in raw],
            "tag_counts": dict(Counter(str(r["tag"]) for r in raw)), "texts": texts, "list_records": lists, "controls": controls}


def source_inventory(content):
    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise ValueError("invalid_hwp_source_header")
        props = int.from_bytes(header[36:40], "little")
        if props & 6:
            raise ValueError("encrypted_or_distribution_source")
        sections, streams, total = [], [], 0
        for entry in sorted(ole.listdir()):
            if ole.get_size(entry) > LIMIT:
                raise ValueError("source_stream_size_limit")
            data = ole.openstream(entry).read()
            total += len(data)
            if total > LIMIT:
                raise ValueError("source_container_size_limit")
            path = "/".join(entry)
            streams.append({"stream": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "semantic_review_status": "pending"})
            if len(entry) == 2 and entry[0] == "BodyText" and re.fullmatch(r"Section\d+", entry[1]):
                decoded, accounting = inflate(data, bool(props & 1))
                sections.append({**section_inventory(decoded, path), "compression_accounting": accounting})
        if not sections:
            raise ValueError("source_body_sections_missing")
        return {"version": "independent-hwp-source-inventory-v1", "source_sha256": hashlib.sha256(content).hexdigest(),
                "streams": streams, "sections": sections, "complete_atomic_gold": False, "whole_form_complete": False,
                "visual_review_complete": False, "scope": "original_streams_records_text_controls_lists_not_semantic_or_visual_completeness"}
