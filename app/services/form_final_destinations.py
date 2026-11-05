"""Bounded essay cells and original-only identity/date/signature destinations."""
import re
from copy import deepcopy

from app.services.document_layout import compact
from app.services.form_basic_inputs import _cell
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-final-destinations-v2"
ESSAY_TITLES = {
    "1.본인에대한간단한소개와가족돌봄상황을적어주세요.",
    "2.기존지원경험과현재필요한도움",
    "3.장학금을받게된다면어떻게사용할예정인가요?",
    "4.향후진로계획및장래의비전",
}
IDENTITY_ACTION = "주민등록번호는 채팅에 입력하지 말고 본인이 원본의 해당 칸에 직접 작성하세요."
DATE_ACTION = "원본에 표시된 연도와 실제 작성일을 확인하고 월·일을 원본에서 직접 작성하세요. 날짜를 자동으로 채우지 않습니다."
SIGN_ACTION = "원본 신청자 칸을 확인하고 본인이 직접 서명하거나 도장을 찍으세요."


def native_final_destinations(blocks, source):
    bindings = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        if shape.get("method") != "native":
            continue
        texts = [b for b in blocks if b.get("table") == shape["table"]]
        if shape.get("columns") == 19:
            labels = [b for b in texts if (b.get("row"), b.get("column"), compact(b["text"])) == (0, 10, "주민등록번호")]
            prompts = [b for b in texts if (b.get("row"), b.get("column"), b["text"].strip()) == (0, 15, "-")]
            cell = _cell(shape, 0, 15, 4, blank=False)
            header = _cell(shape, 0, 10, 5, blank=False)
            if len(labels) == len(prompts) == 1 and cell and header and not cell.get("blank") and not header.get("blank") and all(b.get("method") == "native" for b in labels + prompts) and labels[0].get("column_span") == 5 and prompts[0].get("column_span") == 4 and cell.get("text_locations") == [prompts[0]["location"]] and header.get("text_locations") == [labels[0]["location"]] and len(cell.get("paragraph_char_counts", [])) == 1 and cell["paragraph_char_counts"][0] > 1:
                label = labels[0]
                field = _field(label, source, cell["cell_location"])
                field["input_cell_source"] = {**(source or {}), **cell}
                field["layout"].update({k: cell[k] for k in ("row", "column", "row_span", "column_span", "cell_location")})
                field["manual_spec"] = {"version": VERSION, "mode": "original_only", "action": IDENTITY_ACTION,
                                        "sources": [block_source(b, source) for b in labels + prompts]}
                field["instructions"] = label["text"] + "\n" + IDENTITY_ACTION
                bindings.append({"label": label, "field": field, "kind": "identity"})
        if shape.get("rows") != 4 or shape.get("columns") != 1 or len(shape.get("cells", [])) != 4 or shape.get("issues"):
            continue
        for label in texts:
            if compact(label["text"]) not in ESSAY_TITLES or label.get("row") not in (0, 2) or label.get("column") != 0 or label.get("method") != "native":
                continue
            header = _cell(shape, label["row"], 0, 1, blank=False)
            cell = _cell(shape, label["row"] + 1, 0, 1)
            peers = [b for b in texts if b.get("cell_location") == label.get("cell_location")]
            if not header or not cell or label.get("row_span") != 1 or label.get("column_span") != 1 or any(b.get("method") != "native" for b in peers) or header.get("text_locations") != [b["location"] for b in peers]:
                continue
            bindings.append({"label": label, "cell": {**(source or {}), **cell}, "kind": "essay"})
    # The literal declaration date immediately precedes this standalone signer line.
    for index, label in enumerate(blocks):
        if label.get("kind") != "paragraph" or compact(label["text"]) != "신청자:(인/서명)*서명또는도장필수":
            continue
        date = blocks[index - 1] if index else {}
        if label.get("method") != "native" or date.get("method") != "native" or date.get("kind") != "paragraph" or not re.fullmatch(r"\d{4}년\s+월\s+일", date.get("text", "").strip()):
            continue
        for block, kind, action in ((date, "date", DATE_ACTION), (label, "signature", SIGN_ACTION)):
            field = _field(block, source, block["location"])
            if kind == "signature":
                field.update(field_type="signature", required=True, required_source=block_source(block, source))
            field["manual_spec"] = {"version": VERSION, "mode": "original_only", "action": action,
                                    "sources": [block_source(date, source), block_source(label, source)]}
            field["original_destination"] = {"version": VERSION, "kind": "paragraph_template", "source": block_source(block, source),
                                             "visual_placement_verified": False}
            field["instructions"] = block["text"] + "\n" + action
            bindings.append({"label": block, "field": field, "kind": kind})
    # The consent footer is inside an outer cell containing nested tables. Bind
    # its paragraph only; this is not a blank cell or a flat input destination.
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        if shape.get("method") != "native" or shape.get("rows") != 1 or shape.get("columns") != 1 or len(shape.get("cells", [])) != 1:
            continue
        cell = shape["cells"][0]
        peers = [b for b in blocks if b.get("cell_location") == cell.get("cell_location")]
        dates = [b for b in peers if re.fullmatch(r"\d{4}년\s+월\s+일", b["text"].strip())]
        attestations = [b for b in peers if compact(b["text"]).startswith("신청자본인및돌봄대상자는본동의서의내용을이해하였으며,")]
        recipients = [b for b in peers if compact(b["text"]) == "우양재단貴中"]
        headings = [b for b in blocks if b.get("method") == "native" and b["start"] < owner["start"] and compact(b["text"]) == "개인[신용]정보수집·이용·제공동의서"]
        if len(dates) != 1 or len(attestations) != 1 or len(recipients) != 1 or len(headings) != 1 or any(b.get("method") != "native" for b in peers):
            continue
        date, attestation, recipient, heading = dates[0], attestations[0], recipients[0], headings[0]
        if not attestation["end"] < date["start"] < recipient["start"] or date["location"] not in cell.get("text_locations", []):
            continue
        field = _field(date, source, date["location"])
        field["manual_spec"] = {"version": VERSION, "mode": "original_only", "action": DATE_ACTION,
                                "sources": [block_source(b, source) for b in (heading, attestation, date, recipient)]}
        field["original_destination"] = {"version": VERSION, "kind": "paragraph_in_cell_template", "source": block_source(date, source),
                                         "visual_placement_verified": False}
        field["subject"] = {"label": "개인정보동의서", "source": block_source(heading, source)}
        field["instructions"] = date["text"] + "\n" + DATE_ACTION
        bindings.append({"label": date, "field": field, "kind": "consent_date"})
    return bindings


def bind_final_destinations(blocks, fields, source):
    actual = deepcopy(fields)
    for binding in native_final_destinations(blocks, source):
        location = binding["label"]["location"]
        if binding["kind"] == "essay":
            matching = [f for f in actual if f["source"]["location"] == location and f["field_type"] == "essay"]
            if len(matching) == 1:
                matching[0]["input_cell_source"] = binding["cell"]
                matching[0]["destination_version"] = VERSION
            continue
        actual = [f for f in actual if f["source"]["location"] != location]
        actual.append(binding["field"])
    return actual


def audit_final_destinations(blocks, fields, source):
    bindings = native_final_destinations(blocks, source)
    checks = []
    for label in blocks:
        text = compact(label["text"])
        candidate = text in ESSAY_TITLES or (text == "주민등록번호" and (label.get("row"), label.get("column")) == (0, 10)) or (label.get("kind") == "paragraph" and text == "신청자:(인/서명)*서명또는도장필수") or re.fullmatch(r"\d{4}년월일", text)
        if not candidate:
            continue
        expected = [b for b in bindings if b["label"]["location"] == label["location"]]
        actual = [f for f in fields if f["source"]["location"] == label["location"]]
        valid = len(expected) == len(actual) == 1
        if valid and expected[0]["kind"] == "essay":
            valid = actual[0]["field_type"] == "essay" and actual[0].get("input_cell_source") == expected[0]["cell"] and actual[0].get("destination_version") == VERSION
        elif valid:
            valid = all(actual[0].get(k) == expected[0]["field"].get(k) for k in ("manual_spec", "original_destination", "input_cell_source", "field_type", "required", "instructions", "subject"))
        checks.append({"source": block_source(label, source), "status": "bound" if valid else "requires_original_review",
                       "issues": [] if valid else ["final_destination_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
            "scope": "four_essay_cells_identity_cell_and_three_original_only_paragraphs_not_whole_form"}
