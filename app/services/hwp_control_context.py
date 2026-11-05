"""Independent original control/paragraph context, not automatic input mapping."""
import hashlib
import io
import struct

import olefile
from app.services.hwp_source_inventory import record_inventory, paragraph_text, inflate, WIDE_CONTROLS, LIMIT

VERSION = "independent-hwp-control-context-v2"


def field_spans(payload):
    if len(payload) % 2:
        raise ValueError("invalid_source_utf16_length")
    cursor, stack, spans, issues = 0, [], [], []
    while cursor < len(payload):
        code = int.from_bytes(payload[cursor:cursor + 2], "little")
        if code in WIDE_CONTROLS:
            if cursor + 16 > len(payload):
                raise ValueError("truncated_source_text_control")
            if int.from_bytes(payload[cursor + 14:cursor + 16], "little") != code:
                issues.append("inline_control_boundary_mismatch")
            if code == 3:
                stack.append(cursor)
            elif code == 4:
                if not stack:
                    issues.append("field_end_without_start")
                else:
                    start = stack.pop()
                    spans.append({"start_byte": start, "end_byte": cursor + 16,
                                  "visible_text": paragraph_text(payload[start + 16:cursor]), "nested": bool(stack)})
            cursor += 16
        else:
            cursor += 2
    if stack:
        issues.append("field_start_without_end")
    return {"spans": sorted(spans, key=lambda s: s["start_byte"]), "issues": list(dict.fromkeys(issues))}


def observed_click_command(payload):
    # Bounded observed profile in this original. Unknown profiles stay unresolved;
    # this is not a complete HWP control-header implementation.
    if len(payload) < 11 or payload[:4] != b"klc%" or payload[8] != 9:
        return None
    length = int.from_bytes(payload[9:11], "little")
    end = 11 + length * 2
    if end > len(payload):
        return None
    try:
        command = payload[11:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not command.startswith("Clickhere:set:"):
        return None
    return {"text": command, "byte_start": 11, "byte_end": end,
            "trailing_bytes": len(payload) - end, "trailing_sha256": hashlib.sha256(payload[end:]).hexdigest(),
            "profile": "observed_clickhere_utf16_length_profile_not_full_control_spec"}


def section_context(data, section):
    records = record_inventory(data)
    contexts = []
    for control in records:
        if control["tag"] != 71 or control["control_id"] == " lbt":
            continue
        index = control["record"]
        header = next((r for r in reversed(records[:index]) if r["tag"] == 66 and r["level"] < control["level"]), None)
        paragraph = None
        if header:
            following = records[header["record"] + 1:]
            boundary = next((r["record"] for r in following if r["level"] < header["level"] or r["tag"] == 66 and r["level"] == header["level"]), len(records))
            texts = [r for r in records[header["record"] + 1:boundary] if r["tag"] == 67 and r["level"] == header["level"] + 1]
            if index < boundary and len(texts) == 1:
                paragraph = texts[0]
        proof = {k: v for k, v in control.items() if k != "payload"}
        proof["location"] = f"{section}/record:{index}"
        command = observed_click_command(control["payload"])
        context = None
        if paragraph:
            spans = field_spans(paragraph["payload"])
            siblings = [r["record"] for r in records[paragraph["record"] + 1:boundary]
                        if r["tag"] == 71 and r["control_id"] == "klc%" and r["level"] == control["level"]]
            context = {"record": paragraph["record"], "header_record": header["record"],
                "location": f"{section}/record:{paragraph['record']}", "text": paragraph_text(paragraph["payload"]),
                "payload_sha256": paragraph["payload_sha256"], "field_spans": spans["spans"], "issues": spans["issues"],
                "click_control_records": siblings, "field_count_matches": len(spans["spans"]) == len(siblings),
                "mapping_by_order_authorized": False}
        contexts.append({"source": proof, "observed_command": command, "paragraph_context": context,
            "status": "requires_original_review", "automatic_inheritance": False, "target_field_ids": [],
            "visual_placement_verified": False, "control_semantics_verified": False})
    return {"section": section, "controls": contexts, "unresolved": len(contexts), "whole_form_complete": False}


def control_context(content):
    if len(content) > LIMIT:
        raise ValueError("source_container_size_limit")
    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise ValueError("invalid_hwp_source_header")
        props = struct.unpack_from("<I", header, 36)[0]
        if props & 6:
            raise ValueError("encrypted_or_distribution_source")
        sections = []
        total, decoded_total = 0, 0
        for entry in sorted(ole.listdir()):
            if len(entry) != 2 or entry[0] != "BodyText" or not entry[1].startswith("Section") or not entry[1][7:].isdigit():
                continue
            if ole.get_size(entry) > LIMIT:
                raise ValueError("source_stream_size_limit")
            data = ole.openstream(entry).read()
            total += len(data)
            if total > LIMIT:
                raise ValueError("source_container_size_limit")
            decoded, accounting = inflate(data, bool(props & 1))
            decoded_total += len(decoded)
            if decoded_total > LIMIT:
                raise ValueError("source_decoded_body_size_limit")
            sections.append({**section_context(decoded, "/".join(entry)), "compression_accounting": accounting})
    if not sections:
        raise ValueError("source_body_sections_missing")
    return {"version": VERSION, "source_sha256": hashlib.sha256(content).hexdigest(), "sections": sections,
            "whole_form_complete": False, "visual_review_complete": False,
            "scope": "original_control_commands_and_paragraph_context_not_field_mapping_or_rendering"}
