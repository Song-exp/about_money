import io
import json
import struct
from pathlib import Path

import docx
import openpyxl
import pytest

from app.services import llm
from app.services.document_parser import parse_document
from app.services.document_layout import finalize_blocks
from app.services.hwp_layout import records, section_blocks
from app.services.posting_extract import extract_structured, _chunks
from app.services.posting_documents import merge_posting_documents
from app.services.source_assets import parse_asset
from app.services.source_evidence import find_quote
from tests.test_posting_documents import raw_posting


def test_spreadsheet_signature_routes_once_and_preserves_merged_cells():
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "신청양식"
    sheet.merge_cells("A1:D1")
    sheet["A1"] = "학생 인적사항"
    sheet["A2"] = "성 명"
    sheet["B2"] = "=1+1"
    stream = io.BytesIO()
    book.save(stream)
    asset = parse_asset(stream.getvalue(), "unknown.bin", "https://x.test/form")[0]
    assert asset["detected_filename"].endswith(".xlsx")
    assert asset["blocks"][0]["column_span"] == 4
    assert asset["blocks"][1]["role"] == "student"
    assert asset["blocks"][1]["row"] == 1
    assert asset["status"] == "needs_review"
    assert any(i["error"] == "spreadsheet_formula_requires_review" for i in asset["issues"])
    assert "2" not in [b["text"] for b in asset["blocks"]]


def test_docx_role_does_not_escape_table_or_vertical_heading_span():
    document = docx.Document()
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = "보호자"
    table.cell(0, 1).text = "성명"
    table.cell(1, 1).text = "주소"
    table.cell(2, 1).text = "성명"
    document.add_paragraph("성명")
    stream = io.BytesIO()
    document.save(stream)
    blocks = parse_document(stream.getvalue(), "form.docx")["blocks"]
    assert [b["role"] for b in blocks if b["text"] == "성명"] == ["guardian", "unknown", "unknown"]


def test_repeated_fields_null_instruction_and_whitespace_quote_are_grounded(monkeypatch):
    blocks = finalize_blocks([
        {"text": "학생", "location": "t/r1/c1", "table": "t", "row": 0, "column": 0, "row_span": 2},
        {"text": "성 명", "location": "t/r1/c2", "table": "t", "row": 0, "column": 1},
        {"text": "보호자", "location": "t/r3/c1", "table": "t", "row": 2, "column": 0},
        {"text": "성 명", "location": "t/r3/c2", "table": "t", "row": 2, "column": 1},
    ])
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    response = {"application_fields": [{"label": "성명", "evidence": "성명", "instructions": None,
                                        "field_type": "text", "location": b["location"]} for b in (blocks[1], blocks[3])]}
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: json.dumps(response))
    text = "\n".join(b["text"] for b in blocks)
    result = extract_structured(text, {"sha256": "source"}, blocks)
    fields = result["application_fields"]
    assert [f["role"] for f in fields] == ["student", "guardian"]
    assert len({f["id"] for f in fields}) == 2
    assert all(f["evidence"] == "성 명" and f["instructions"] == "" for f in fields)
    assert extract_structured(text, {"sha256": "source"}, blocks)["application_fields"] == fields
    assert find_quote("지원동기\x00 500자", "지원동기 500자") == (0, 10, "whitespace")
    assert find_quote("지원동기 500자", "지원동기 1000자") is None


def test_json_retry_is_bounded_and_logged(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    calls, events = [], []
    def reply(*args, **kwargs):
        calls.append(args)
        return "bad JSON" if len(calls) == 1 else '{"application_fields":[]}'
    monkeypatch.setattr(llm, "chat_complete", reply)
    extract_structured("공고", trace_sink=events.append)
    assert len(calls) == 2
    assert events[0]["model_attempts"][0]["error"] == "ValueError"


def test_table_is_not_split_when_it_fits_chunk():
    blocks = finalize_blocks([{"text": "a" * 6500, "location": "p"},
                              {"text": "b" * 2000, "location": "t/c1", "table": "t"},
                              {"text": "c" * 1000, "location": "t/c2", "table": "t"}])
    chunks = list(_chunks("\n".join(b["text"] for b in blocks), blocks=blocks))
    assert any("b" * 2000 + "\n" + "c" * 1000 in chunk for chunk, _ in chunks)


def test_duplicate_form_merges_only_identical_table_and_retains_conflicts():
    def field(identity, digest, maximum=None):
        return {"id": identity, "label": "성명", "role": "student", "field_type": "text", "max_length": maximum,
                "source": {"sha256": digest}, "layout": {"table_fingerprint": "identical", "row": 0, "column": 1}}
    raw = raw_posting()
    raw.source_documents = [{"structured": {"application_fields": [field("a", "source1")]}},
                            {"structured": {"application_fields": [field("b", "source2"), field("c", "source2", 10)]}}]
    result = merge_posting_documents(raw)
    assert [f["id"] for f in result["application_fields"]] == ["a"]
    canonical = result["application_fields"][0]
    assert canonical["alternative_sources"] == [{"sha256": "source2"}, {"sha256": "source2"}]
    assert canonical["max_length"] is None
    assert canonical["attribute_conflicts"] == ["max_length"]
    assert [f["max_length"] for f in canonical["attribute_variants"]] == [None, 10]
    assert canonical["validation_status"] == "conflict_requires_review"
    assert "conflicting_duplicate_form_field" in result["issues"]


def test_hwp_record_bounds_and_table_coordinates():
    def record(tag, level, data):
        return struct.pack("<I", tag | (level << 10) | (len(data) << 20)) + data
    data = record(71, 1, b" lbt") + record(77, 2, b"\0" * 4 + struct.pack("<HH", 2, 2))
    data += record(72, 2, b"\0" * 8 + struct.pack("<HHHH", 1, 0, 1, 2) + b"\0" * 18)
    data += record(67, 3, "성명".encode("utf-16-le"))
    block = section_blocks(data, "BodyText/Section0")[0]
    assert (block["row"], block["column"], block["row_span"]) == (0, 1, 2)
    with pytest.raises(ValueError, match="truncated"):
        list(records(data[:-1]))


def test_role_heading_split_across_paragraphs_in_one_merged_cell():
    blocks = finalize_blocks([
        {"text": "대 상 자", "location": "t/c1/p1", "cell_location": "t/c1", "table": "t", "row": 0, "column": 0, "row_span": 3},
        {"text": "인적사항", "location": "t/c1/p2", "cell_location": "t/c1", "table": "t", "row": 0, "column": 0, "row_span": 3},
        {"text": "성명", "location": "t/c2", "table": "t", "row": 1, "column": 1},
        {"text": "성명", "location": "t/c3", "table": "t", "row": 3, "column": 1},
    ])
    assert blocks[2]["role"] == "student" and blocks[3]["role"] == "unknown"
    assert blocks[2]["role_evidence"] == "대 상 자\n인적사항"


def test_distant_instruction_has_separate_exact_evidence(monkeypatch):
    blocks = finalize_blocks([{"text": "지원동기", "location": "field"},
                              {"text": "그 외 공지", "location": "middle"},
                              {"text": "지원동기는 500자 이내로 작성", "location": "guidance"}])
    item = {"label": "지원동기", "evidence": "지원동기", "location": "field", "max_length": 500,
            "length_unit": "characters", "instructions": "500자 이내로 작성",
            "instruction_evidence": "지원동기는 500자 이내로 작성"}
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: json.dumps({"application_fields": [item]}))
    text = "\n".join(b["text"] for b in blocks)
    result = extract_structured(text, blocks=blocks)
    assert result["application_fields"][0]["instruction_source"]["location"] == "guidance"
    item["instruction_evidence"] = "지원동기는 1000자 이내로 작성"
    assert extract_structured(text, blocks=blocks)["application_fields"] == []


def test_wrong_location_cannot_bind_a_title_from_a_different_cell(monkeypatch):
    blocks = finalize_blocks([{"text": "성명", "location": "student/c1"},
                              {"text": "생년월일", "location": "student/c2"}])
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: json.dumps({"application_fields": [
        {"label": "성명", "evidence": "성명\n생년월일", "location": "student/c2"}]}))
    assert extract_structured("성명\n생년월일", blocks=blocks)["application_fields"] == []


def test_block_guidance_uses_original_text_and_offsets(monkeypatch):
    blocks = [{"text": "등록금 납부액", "location": "field"},
              {"text": "등록금 납부액은 감면 후 차액(실 납부금액)만 기재", "location": "guide"}]
    item = {"label": "등록금 납부액", "field_type": "text", "label_block_id": "b0000",
            "instruction_block_ids": ["b0001"], "instructions": "모델이 다시 쓴 지침"}
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [item]}))
    body = "\n".join(b["text"] for b in blocks)
    field = extract_structured(body, blocks=blocks)["application_fields"][0]
    assert field["instructions"] == blocks[1]["text"]
    proof = field["instruction_source"]
    assert body[proof["text_start"]:proof["text_end"]] == field["instructions"]
    item["instruction_block_ids"] = ["b9999"]
    assert extract_structured(body, blocks=blocks)["application_fields"] == []
    item["instruction_block_ids"] = ["b0001"]
    item["label"] = "성명"
    assert extract_structured(body, blocks=blocks)["application_fields"] == []


def test_pointer_cannot_borrow_another_fields_guidance(monkeypatch):
    blocks = [{"text": "성명", "location": "field"}, {"text": "주소는 도로명으로 기재", "location": "guide"}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": "성명", "label_block_id": "b0000", "instruction_block_ids": ["b0001"]}]}))
    assert extract_structured("성명\n주소는 도로명으로 기재", blocks=blocks)["application_fields"] == []


def test_guidance_heading_and_specific_instruction_can_be_selected_together(monkeypatch):
    blocks = [{"text": "등록금납부액", "location": "field"},
              {"text": "<작성방법>", "location": "guide/p1", "cell_location": "guide"},
              {"text": "등록금 납부액은 실 납부금액만 기재", "location": "guide/p2", "cell_location": "guide"}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": "등록금납부액", "label_block_id": "b0000", "instruction_block_ids": ["b0001", "b0002"]}]}))
    field = extract_structured("\n".join(b["text"] for b in blocks), blocks=blocks)["application_fields"][0]
    assert len(field["instruction_sources"]) == 2
    assert field["instructions"] == "<작성방법>\n등록금 납부액은 실 납부금액만 기재"


def test_split_label_and_choices_bind_to_same_physical_input(monkeypatch):
    blocks = [{"text": "수집‧이용", "location": "t/c1/p1", "cell_location": "t/c1", "table": "t", "row": 0, "column": 0},
              {"text": "동의 여부", "location": "t/c1/p2", "cell_location": "t/c1", "table": "t", "row": 0, "column": 0},
              {"text": "동의함 □ 동의하지 않음 □", "location": "t/c2", "cell_location": "t/c2", "table": "t", "row": 0, "column": 1}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    item = {"label": "수집‧이용 동의 여부", "field_type": "checkbox", "label_block_id": "b0000",
            "option_block_ids": ["b0002"], "options": ["동의함", "동의하지 않음"]}
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [item]}))
    body = "\n".join(b["text"] for b in blocks)
    field = extract_structured(body, blocks=blocks)["application_fields"][0]
    assert field["evidence"] == "수집‧이용\n동의 여부"
    assert field["option_sources"][0]["location"] == "t/c2"
    blocks[2]["row"] = 1
    assert extract_structured(body, blocks=blocks)["application_fields"] == []


def test_local_guidance_inside_label_row_span_is_grounded(monkeypatch):
    blocks = [{"text": "지급계좌", "location": "t/c1", "table": "t", "row": 0, "column": 0, "row_span": 2, "method": "native"},
              {"text": "본인 명의의 통장만 가능", "location": "t/c2", "table": "t", "row": 1, "column": 1, "method": "native"}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": "지급계좌", "label_block_id": "b0000", "instruction_block_ids": ["b0001"]}]}))
    body = "\n".join(b["text"] for b in blocks)
    assert extract_structured(body, blocks=blocks)["application_fields"][0]["instructions"] == blocks[1]["text"]
    blocks[1]["row"] = 2
    assert extract_structured(body, blocks=blocks)["application_fields"] == []


def test_native_signature_birth_inputs_remain_distinct_and_grounded(monkeypatch):
    blocks = [{"text": text, "location": f"t/c/p{i}", "cell_location": "t/c", "table": "t",
               "row": 0, "column": 0, "method": "native"} for i, text in enumerate([
                   "성명(지원대상자 본인) : (서명 또는 인)", "(생년월일:    )",
                   "성명(신청인) : (서명 또는 인)", "(생년월일:    )"])]
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    body = "\n".join(b["text"] for b in blocks)
    fields = [f for f in extract_structured(body, {"sha256": "proof"}, blocks)["application_fields"] if f["label"] == "생년월일"]
    assert [f["role"] for f in fields] == ["student", "applicant"]
    assert len({f["id"] for f in fields}) == 2
    assert [f["layout"]["cell_paragraph"] for f in fields] == [1, 3]
    assert all(body[f["source"]["text_start"]:f["source"]["text_end"]] == f["evidence"] for f in fields)


@pytest.mark.parametrize("text,cell,method", [("(생년월일: 2001-01-01)", "t/c", "native"),
                                            ("생년월일은 작성하지 않습니다", "t/c", "native"),
                                            ("(생년월일: )", "t/other", "native"),
                                            ("(생년월일: )", "t/c", "ocr")])
def test_signature_birth_recovery_excludes_values_prose_other_cells_and_ocr(monkeypatch, text, cell, method):
    blocks = [{"text": "성명(신청인) : (서명 또는 인)", "location": "t/c/p1", "cell_location": "t/c", "method": "native"},
              {"text": text, "location": cell + "/p2", "cell_location": cell, "method": method}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: '{"application_fields":[]}')
    assert extract_structured("\n".join(b["text"] for b in blocks), blocks=blocks)["application_fields"] == []


def test_duplicate_fields_keep_separate_paragraphs_and_normalize_option_order():
    def field(identity, digest, paragraph, options):
        return {"id": identity, "label": "동의 여부", "field_type": "checkbox", "options": options,
                "source": {"sha256": digest}, "layout": {"table_fingerprint": "same", "row": 0, "column": 0,
                                                         "cell_paragraph": paragraph, "block_offset": 0}}
    raw = raw_posting()
    raw.source_documents = [{"structured": {"application_fields": [field("a", "s1", 0, ["동의함", "동의하지 않음"]),
                                                                         field("b", "s1", 1, ["동의함"])]}},
                            {"structured": {"application_fields": [field("c", "s2", 0, ["동의하지않음", "동의함"])]}}]
    result = merge_posting_documents(raw)
    assert [f["id"] for f in result["application_fields"]] == ["a", "b"]
    assert "conflicting_duplicate_form_field" not in result["issues"]


def test_conflicts_block_writing_even_with_matching_manual_review():
    from types import SimpleNamespace
    from app.models.scholarship import PostingStatus
    from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="source", eligibility={
        "validation_version": VALIDATION_VERSION, "application_fields": [{"source": {"sha256": "source"},
            "evidence": "성명", "attribute_conflicts": ["field_type"]}]})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    state = readiness(row)
    assert state["writing_ready"] is False and "application_form_conflicts" in state["reasons"]


@pytest.mark.parametrize("folder,prefix,extension", [("notice-1", "58a72e5048bb", ".hwp"),
                                                    ("notice-new-555351", "280961688897", ".xlsx"),
                                                    ("notice-new-555351", "a0167c494ee6", ".xls")])
def test_captured_official_form_native_layout(folder, prefix, extension):
    paths = list((Path("reports/scholarship-evaluation-20261005") / folder).glob(f"asset-*-{prefix}.bin"))
    if not paths:
        pytest.skip("official evaluation capture not installed")
    result = parse_document(paths[0].read_bytes(), "form" + extension)
    assert result["blocks"] and any("row" in b and "column" in b for b in result["blocks"])
    assert all(result["extracted_text"][b["start"]:b["end"]] == b["text"] for b in result["blocks"])
