"""구역 제목 대신 같은 행의 실제 동의 제목과 선택 셀을 연결한다."""
import re

from app.services.document_layout import compact

MARKERS = r"☐□☑■"


def original_options(text):
    """동의 선택 앞의 설명문을 선택지에 포함하지 않는다."""
    consent = re.findall(rf"(동의하지\s*않음|동의함|동의하지\s*않습니다|동의합니다)\s*[{MARKERS}]", text)
    if consent:
        return [option.strip() for option in consent]
    value = text.strip().strip("[]")
    if value[:1] in MARKERS:
        values = re.split(rf"[{MARKERS}]", value)[1:]
    else:
        values = re.findall(rf"([^{MARKERS}]+)[{MARKERS}]", value)
    # Parentheses may contain a real amount/grade blank. Keep the whole caption;
    # stripping punctuation here used to silently truncate those input controls.
    values = [o.strip() for o in values]
    return values if values and all(o and len(o) < 60 for o in values) else []


def native_consent_controls(blocks):
    controls = []
    for block in blocks:
        if block.get("method") != "native" or not block.get("cell_location") or "동의여부" not in compact(block["text"]):
            continue
        peers = [b for b in blocks if b.get("method") == "native" and b.get("cell_location") == block["cell_location"]]
        title = "\n".join(b["text"] for b in peers)
        if compact(title) not in ("수집·이용동의여부", "수집ㆍ이용동의여부", "수집이용동의여부", "제공·조회동의여부", "동의여부"):
            continue
        choices = [b for b in blocks if b.get("method") == "native" and b.get("table") == block.get("table")
                   and b.get("row") == block.get("row") and
                   b.get("column") == block.get("column", -1) + (block.get("column_span") or 1)
                   and original_options(b["text"]) and "동의" in b["text"]]
        headings = [b for b in blocks if b.get("table") == block.get("table") and b.get("row") == 0
                    and b.get("method") == "native" and
                    ("개인정보" in compact(b["text"]) or "주민등록번호처리" in compact(b["text"]))]
        if len(headings) != 1 or not choices:
            continue
        # A footer grid with row subjects is a repeating template, not this control.
        if any(compact(b["text"]) == "구분" for b in blocks if b.get("table") == block.get("table") and b.get("row") == 0):
            continue
        controls.append({"label": title, "field_type": "checkbox", "label_block_id": peers[0]["block_id"],
                         "option_block_ids": [b["block_id"] for b in choices],
                         "options": [o for b in choices for o in original_options(b["text"])],
                         "_control_locations": {b["location"] for b in [*peers, *choices, *headings]},
                         "_control_ids": {b["block_id"] for b in [*peers, *choices, *headings]},
                         "_heading": headings[0]})
    return controls


def reconcile_control(item, controls):
    """A model's section/answer pointer becomes the proven input-row pointer.

    Never repair an arbitrary different label or unknown pointer. Native recovery
    is also independently emitted so omission does not lose the original control.
    """
    if not isinstance(item, dict):
        return item
    matches = []
    for control in controls:
        if item.get("label_block_id"):
            match = item["label_block_id"] in control["_control_ids"]
        elif item.get("location"):
            match = item["location"] in control["_control_locations"]
        else:
            match = compact(item.get("label", "")) in (compact(control["label"]), compact(control["_heading"]["text"]).split("*")[0])
        if match:
            matches.append(control)
    if len(matches) == 1:
        return {k: v for k, v in matches[0].items() if not k.startswith("_")}
    return item


def consent_context(block, blocks, source):
    from app.services.form_semantics import block_source
    if not block.get("table"):
        return None
    headings = [b for b in blocks if b.get("table") == block["table"] and b.get("row") == 0
                and b.get("method") == "native" and len(b["text"]) < 150]
    for heading in headings:
        text = compact(heading["text"])
        if "주민등록번호처리" in text:
            kind = "identity_processing"
        elif "제공" in text and "수집" not in text:
            kind = "provision_inquiry"
        elif "수집" in text and "이용" in text and "제공" not in text:
            kind = "collection_use"
        else:
            continue
        # Positional scope includes nested data inventory tables but ends at the
        # last native cell of this processing section; never cross to the next one.
        end = max(b["end"] for b in blocks if b.get("table") == block["table"])
        contextual = [b for b in blocks if b.get("method") == "native" and heading["start"] <= b["start"] and b["end"] <= end]
        return {"kind": kind, "label": heading["text"], "source": block_source(heading, source),
                "sources": [block_source(b, source) for b in contextual], "text": "\n".join(b["text"] for b in contextual)}
    return None
