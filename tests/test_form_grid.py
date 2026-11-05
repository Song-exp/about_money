import json
import struct
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_grid import native_grids
from app.services.hwp_layout import section_blocks
from app.services.posting_extract import extract_structured
from app.services.form_semantics import active_fields, is_manual
from app.services.writing_requirements import requirements, next_action, field_label
from app.services.writing_workflow import _check_choice, _set_fact
from app.services.writing_drafts import compose
from app.services.posting_documents import merge_posting_documents
from app.models.scholarship import PostingStatus


def record(tag, level, payload):
    return struct.pack("<I", tag | level << 10 | len(payload) << 20) + payload


def grid_data(offset=8, reference=True):
    data = record(71, 1, b" lbt") + record(77, 2, b"\0" * 4 + struct.pack("<HH", 4, 5))
    texts = {(0, i): text for i, text in enumerate(("구분", "성명", "신청자와의 관계", "동의 여부", "서명 또는 인"))}
    texts.update({(1, 0): "신청자", (2, 0): "돌봄대상자", (3, 0): "추가 돌봄대상자(해당 시)",
                  (1, 3): "위 표시와 같음" if reference else "동의함 □", (2, 3): "동의함 □", (3, 3): "동의함 □"})
    for row in range(4):
        for column in range(5):
            header = struct.pack("<II", 1, 0x20) if offset == 8 else struct.pack("<HI", 1, 0x20)
            cell = header + struct.pack("<HHHHII", column, row, 1, 1, 100, 100) + b"\0" * 10
            data += record(72, 2, cell)
            text = texts.get((row, column), "")
            data += record(66, 2, struct.pack("<I", (len(text) + 1) | 0x80000000))
            if text:
                data += record(67, 3, (text + "\r").encode("utf-16-le"))
    return data, [texts[key] for key in sorted(texts)]


def parsed_grid(reference=True):
    data, _ = grid_data(reference=reference)
    return finalize_blocks(section_blocks(data, "BodyText/Section0"))


@pytest.mark.parametrize("offset", [6, 8])
def test_native_empty_cells_keep_exact_text_and_cell_record_geometry(offset):
    data, texts = grid_data(offset)
    blocks = finalize_blocks(section_blocks(data, "BodyText/Section0"))
    assert [b["text"] for b in blocks] == texts
    shape = blocks[0]["table_structure"]
    assert not shape["issues"] and len(shape["cells"]) == 20
    blanks = [c for c in shape["cells"] if c["blank"]]
    assert len(blanks) == 9 and all(c["column"] in (1, 2, 4) for c in blanks)
    assert all(c["header_offset"] == offset and len(c["payload_sha256"]) == 64 for c in shape["cells"])
    assert all(c["text_locations"] == [] and c["paragraph_char_counts"] == [1] for c in blanks)


def test_grid_omission_and_false_model_row_signatures_do_not_lose_or_duplicate_inputs(monkeypatch):
    blocks = parsed_grid()
    candidates = [{"label": b["text"], "field_type": "signature", "label_block_id": f"b{i:04d}"}
                  for i, b in enumerate(blocks) if b.get("row", 0) > 0 and b.get("column") == 0]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": candidates}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "source"}, blocks)
    fields = result["application_fields"]
    assert len(fields) == 11 and len({f["id"] for f in fields}) == 11
    assert sum(f["field_type"] == "text" for f in fields) == 6
    assert sum(f["field_type"] == "signature" for f in fields) == 3
    assert sum(f["semantic_kind"] == "consent" for f in fields if "semantic_kind" in f) == 2
    assert all(f["required"] is None for f in fields)
    assert "repeating_table_inputs_require_review" not in result["issues"]
    assert "consent_reference_requires_original_review" in result["issues"]
    assert all(f["source"]["location"] != f["input_cell_source"]["cell_location"] for f in fields)
    assert result["coverage"]["native_repeating_grids"]["fields"] == 11
    extra = [f for f in fields if f["layout"]["row"] == 3]
    assert len(extra) == 4 and len({f["applicability"]["id"] for f in extra}) == 1
    text = "\n".join(b["text"] for b in blocks)
    for field in fields:
        for proof in [field["source"], field["subject"]["source"], *field["instruction_sources"], *field.get("option_sources", [])]:
            assert text[proof["text_start"]:proof["text_end"]] == proof["evidence"]


@pytest.mark.parametrize("problem", ["missing_cell", "overlap", "merged", "nonblank", "object", "ocr", "wrong_text_pointer", "bad_geometry"])
def test_unproven_grid_is_left_for_original_review(problem):
    blocks = parsed_grid()
    shape = blocks[0]["table_structure"]
    if problem == "missing_cell":
        shape["cells"].pop()
    elif problem == "overlap":
        shape["cells"][-1] = deepcopy(shape["cells"][-2])
    elif problem == "merged":
        shape["cells"][-1]["column_span"] = 2
    elif problem == "nonblank":
        shape["cells"][-1]["paragraph_char_counts"] = [8]
    elif problem == "object":
        shape["cells"][-1]["has_control"] = True
    elif problem == "ocr":
        blocks[-1]["method"] = "ocr"
    elif problem == "wrong_text_pointer":
        shape["cells"][0]["text_locations"] = ["wrong"]
    else:
        shape["issues"].append("ambiguous")
    assert not native_grids(blocks, {"sha256": "source"})
    monkey = pytest.MonkeyPatch()
    monkey.setattr(llm, "has_llm", lambda: False)
    try:
        result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "source"}, blocks)
    finally:
        monkey.undo()
    assert "repeating_table_inputs_require_review" in result["issues"]


def test_repeated_people_remain_distinct_after_attachment_merge():
    blocks = parsed_grid()
    fields = native_grids(blocks, {"sha256": "one"})[0]["fields"]
    raw = SimpleNamespace(source_documents=[{"structured": {"application_fields": fields}},
                                           {"structured": {"application_fields": deepcopy(fields)}}])
    merged = merge_posting_documents(raw)
    assert len(merged["application_fields"]) == 11 and not merged["issues"]
    assert len([f for f in merged["application_fields"] if f["label"] == "성명"]) == 3


def test_collect_row_facts_conditions_and_unchecked_consent_without_auto_answers():
    fields = native_grids(parsed_grid(reference=False), {"sha256": "source"})[0]["fields"]
    session = SimpleNamespace(fields=fields, profile={})
    scholarship = SimpleNamespace(eligibility={"validation_version": "evidence-v1"}, category=None,
                                  status=PostingStatus.OPEN, deadline_at=None, content_hash="source")
    answers, workflow = {}, {"additional_qualification_confirmed": True}
    while True:
        action = next_action(session, scholarship, answers, workflow)
        if not action:
            break
        if action["kind"] == "applicability":
            workflow["applicability_answers"] = {action["condition_id"]: {"applies": False, "confirmed": True}}
            continue
        field = next(f for f in fields if f["id"] == action["field_id"])
        if action["kind"] == "confirm":
            for fact in answers[field["id"]]["facts"].values():
                fact["confirmed"] = True
            continue
        assert field["id"] not in answers
        assert field["subject"]["label"] in action["question"]
        value = "표시하지 않음" if field["field_type"] == "checkbox" else "본인이 확인한 실제 이름 또는 관계"
        _check_choice(field, value)
        _set_fact(answers, field, action["slot_key"], value, {"kind": "conversation"})
    assert not any(f["id"] in answers for f in fields if f["layout"]["row"] == 3)
    consent = [f for f in fields if f["field_type"] == "checkbox" and f["layout"]["row"] < 3]
    assert all(answers[f["id"]]["facts"]["value"]["selection_state"] == "unchecked" for f in consent)
    assert all(f["options"] == ["동의함"] for f in consent)
    with pytest.raises(HTTPException):
        _check_choice(consent[0], "네")
    with pytest.raises(HTTPException):
        _check_choice({**consent[0], "selection_mode": None}, "표시하지 않음")
    drafts = [compose(f, answers[f["id"]], SimpleNamespace(title="신청서")) for f in active_fields(fields, workflow) if not is_manual(f)]
    assert len(drafts) == 4 and all("·" in d["label"] for d in drafts)
    assert all(not requirements(f) for f in fields if f["field_type"] == "signature")
    assert all(field_label(f).startswith(f["subject"]["label"]) for f in fields)
