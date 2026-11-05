"""Native attachment marks collect user preparation reports, never file verification."""
import re

from app.services.document_layout import compact
from app.services.form_basic_inputs import _cell
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-submission-checklist-v1"
TITLES = (
    (1, 1, 3, ("장학금신청서+자기소개서", "+개인정보동의서")),
    (2, 1, 3, ("주민등록등본",)),
    (3, 1, 3, ("재학증명서(국문)",)),
    (4, 1, 3, ("성적증명서(국문)",)),
    (5, 3, 1, ("수급자증명서", "or차상위계층증명서")),
    (6, 3, 1, ("학자금지원구간통지서",)),
    (7, 1, 3, ("가족돌봄청년", "증빙서류")),
)
GENERAL = ("◎제출서류체크리스트", "◎아래제시된순서대로", "◎모든서류는원본임을")
FOOTNOTES = ("※가정경제형편을증명하는서류인5번", "※모든증빙서류는", "※증빙서류는각인정발급일")


def _candidate(shape, texts):
    anchors = {title for _, _, _, titles in TITLES for title in titles}
    found = {compact(b["text"]) for b in texts}
    return shape.get("rows") == 8 and shape.get("columns") == 7 and ("첨부여부" in found or len(found & anchors) >= 3)


def native_submission_checklists(blocks, source):
    groups = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        table = shape.get("table")
        texts = [b for b in blocks if b.get("table") == table] if table else []
        if not _candidate(shape, texts) or shape.get("method") != "native" or any(b.get("method") != "native" for b in texts):
            continue
        header = [b for b in texts if b.get("row") == 0 and b.get("column") == 4]
        if [compact(b["text"]) for b in header] != ["첨부여부", "★체크★"]:
            continue
        # Context must be adjacent to this table, rather than borrowed from another checklist.
        indices = [i for i, b in enumerate(blocks) if b.get("table") == table]
        before = blocks[max(0, min(indices) - 3):min(indices)]
        after = blocks[max(indices) + 1:max(indices) + 5]
        general = [b for b in before if any(compact(b["text"]).startswith(p) for p in GENERAL)]
        footer = [b for b in after if any(compact(b["text"]).startswith(p) for p in FOOTNOTES)]
        if len(general) != 3 or len(footer) != 3 or any(b.get("method") != "native" for b in general + footer):
            continue
        if any(sum(compact(b["text"]).startswith(p) for b in general) != 1 for p in GENERAL) or any(sum(compact(b["text"]).startswith(p) for b in footer) != 1 for p in FOOTNOTES):
            continue
        economic = [b for b in texts if compact(b["text"]) == "택1" and b.get("row") == 5 and b.get("column") == 2 and b.get("row_span") == 2]
        if len(economic) != 1:
            continue
        fields, used, valid = [], [], True
        for row, col, span, titles in TITLES:
            labels = [b for b in texts if b.get("row") == row and b.get("column") == col]
            mark_texts = [b for b in texts if b.get("row") == row and b.get("column") == 4]
            cell = _cell(shape, row, 4, 1, blank=row != 1)
            title_cell = _cell(shape, row, col, span, blank=False)
            notes = [b for b in texts if b.get("row") == row and b.get("column") == 5]
            dates = [b for b in texts if b.get("row") == row and b.get("column") == 6]
            if ([compact(b["text"]) for b in labels] != list(titles) or
                any(b.get("column_span") != span or b.get("row_span") != 1 for b in labels) or not cell or not title_cell or
                title_cell.get("text_locations") != [b["location"] for b in labels] or
                len(notes) != {1: 2, 2: 2, 3: 0, 4: 3, 5: 2, 6: 2, 7: 5}[row] or
                (row != 1 and (len(dates) != 1 or not re.fullmatch(r"\d{4}\.?\s*\d{1,2}\.\s*\d{1,2}\.\s*이후", dates[0]["text"].strip()))) or
                (row == 1 and dates) or
                (row == 1 and (len(mark_texts) != 1 or compact(mark_texts[0]["text"]) != "예시:√" or cell.get("text_locations") != [mark_texts[0]["location"]])) or
                (row != 1 and mark_texts)):
                valid = False
                break
            field = _field(labels[0], source, cell["cell_location"])
            field["label"] = " ".join(b["text"] for b in labels)
            field["semantic_kind"] = "submission_checklist"
            field["input_cell_source"] = {**(source or {}), **cell}
            field["layout"].update({k: cell[k] for k in ("row", "column", "row_span", "column_span", "cell_location")})
            rules = [*general, *notes, *dates, *footer] + (economic if row in (5, 6) else [])
            field["instruction_sources"] = [block_source(b, source) for b in rules]
            field["instructions"] = "\n".join(s["evidence"] for s in field["instruction_sources"])
            field["input_spec"] = {"version": VERSION, "preparation_only": True, "slots": [
                {"key": "preparation_status", "label": "이번 신청의 서류 준비 상태", "kind": "preparation_status",
                 "unit": "", "options": ["준비됨", "준비 중"] + (["해당 없음"] if row in (5, 6) else []),
                 "service_only": True, "destination": None, "source": block_source(labels[0], source)}],
                "ready_slots": [{"key": "rules_ack", "label": "원문 발급·제출 조건 확인", "kind": "document_rule_confirmation",
                                 "unit": "", "options": ["원문 조건 확인"], "service_only": True, "destination": None,
                                 "source": block_source(general[2], source)}]}
            field["preparation_spec"] = {"version": VERSION, "user_report_only": True, "file_verified": False,
                "original_marks": ["O", "√"], "example_only": row == 1,
                "example_source": block_source(mark_texts[0], source) if row == 1 else None,
                "document_sources": [block_source(b, source) for b in labels],
                "header_sources": [block_source(b, source) for b in header],
                "economic_rule_review_required": row in (5, 6), "automatic_marking": False}
            field["manual_spec"] = {"mode": "original_submission_check",
                "action": "실제 서류와 원문 발급·제출 조건을 대조한 뒤 직접 첨부하고 원본 첨부여부 칸에 O 또는 √를 표시하세요. 예시 표시는 제출 완료를 뜻하지 않습니다."}
            fields.append(field)
            used.extend(labels)
        if valid and len(fields) == 7:
            # The proven column caption is printed structure, rather than an
            # eighth attachment checkbox. Keep its body marks as seven inputs.
            groups.append({"fields": fields, "locations": {b["location"] for b in [*used, *header]}})
    return groups


def check_preparation(slot, text):
    if slot.get("kind") in ("preparation_status", "document_rule_confirmation") and text not in slot["options"]:
        raise ValueError("report the current preparation status or explicitly acknowledge the original rules")


def audit_submission_checklists(blocks, fields, source):
    groups = native_submission_checklists(blocks, source)
    checks = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        texts = [b for b in blocks if b.get("table") == shape.get("table")]
        if not _candidate(shape, texts):
            continue
        candidates = [g for g in groups if g["fields"][0]["layout"]["table"] == shape["table"]]
        targets = candidates[0]["fields"] if len(candidates) == 1 else []
        valid = bool(targets) and all(len(actual := [f for f in fields if f["id"] == t["id"]]) == 1 and
            all(actual[0].get(k) == t.get(k) for k in ("source", "input_spec", "input_cell_source", "preparation_spec", "manual_spec", "instructions", "instruction_sources", "semantic_kind", "role", "required")) for t in targets)
        checks.append({"source": block_source(owner, source), "status": "bound" if valid else "requires_original_review",
                       "issues": [] if valid else ["submission_checklist_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
            "scope": "attachment_mark_bindings_not_document_verification_or_submission_completion"}
