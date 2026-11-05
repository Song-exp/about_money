import io
import json
import struct
import zlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.services.document_layout import finalize_blocks
from app.services.form_constraints import parse_constraints
from app.services.hwp_layout import embedded_blocks, section_blocks
from app.services.pdf_reconciliation import reconcile_page
from app.services import ocr, llm
from app.services.posting_extract import extract_structured


def native_pdf(rotation=0):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 250 Td (Birth) Tj 0 -50 Td (500 characters) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    if rotation:
        page.rotate(rotation)
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


def reading(text, x0=29, y0=38, x1=70, y1=53):
    return {"text": text, "bbox": [x0, y0, x1, y1], "render_dpi": 72,
            "page": 1, "method": "ocr", "location": "page:1/ocr:1"}


def test_pdf_duplicate_requires_equal_text_at_same_region_and_preserves_audit():
    values = [reading("Birth"), reading("Birth", y0=140, y1=155),
              reading("800 characters", y0=88, y1=103, x1=130)]
    native = {"text": "Birth\n500 characters", "location": "page:1"}
    kept, decisions = reconcile_page(native_pdf(), 1, native, values)
    assert kept == values[1:]
    assert decisions[0]["result"] == "duplicate" and decisions[0]["reading"] == values[0]
    assert decisions[1]["reason"] == "native_ocr_region_disagreement"
    assert "500 characters" in decisions[1]["native_region_text"]


@pytest.mark.parametrize("kind", ["rotation", "bad_pdf", "missing_coordinates", "different_page", "nonfinite", "scanned"])
def test_pdf_unverifiable_reading_is_never_removed(kind):
    value, content = reading("Birth"), native_pdf()
    native = {"text": "Birth", "location": "page:1"}
    if kind == "rotation":
        content = native_pdf(90)
    elif kind == "bad_pdf":
        content = b"broken"
    elif kind == "missing_coordinates":
        value.pop("render_dpi")
    elif kind == "different_page":
        value["page"] = 2
    elif kind == "nonfinite":
        value["bbox"][0] = float("nan")
    else:
        native = None
    kept, _ = reconcile_page(content, 1, native, [value])
    assert kept == [value]


def test_hwp_keeps_distinct_inputs_and_role_within_one_cell(monkeypatch):
    def record(tag, level, data):
        return struct.pack("<I", tag | level << 10 | len(data) << 20) + data
    data = record(71, 1, b" lbt") + record(77, 2, b"\0" * 4 + struct.pack("<HH", 1, 1))
    data += record(72, 2, b"\0" * 8 + struct.pack("<HHHH", 0, 0, 1, 1) + b"\0" * 18)
    texts = ["성명(신청인) : (서명 또는 인)", "(생년월일: )"]
    for text in texts:
        data += record(67, 3, text.encode("utf-16-le"))
    blocks = finalize_blocks(section_blocks(data, "BodyText/Section0"))
    assert [b["text"] for b in blocks] == texts
    assert len({b["location"] for b in blocks}) == 2
    assert len({b["cell_location"] for b in blocks}) == 1
    assert [b["role"] for b in blocks] == ["applicant", "applicant"]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": "성명(신청인)", "field_type": "signature", "label_block_id": "b0000"}]}))
    fields = extract_structured("\n".join(texts), {"sha256": "hwp"}, blocks)["application_fields"]
    assert len(fields) == 2
    assert [f["layout"]["cell_paragraph"] for f in fields] == [0, 1]


def test_hwp_embedded_ocr_keeps_stream_provenance_and_review(monkeypatch):
    png = b"\x89PNG\r\n\x1a\nfixture"
    encoder = zlib.compressobj(wbits=-15)
    compressed = encoder.compress(png) + encoder.flush()
    class Ole:
        def listdir(self):
            return [["BinData", "BIN0001.PNG"]]
        def get_size(self, entry):
            return len(compressed)
        def openstream(self, entry):
            return io.BytesIO(compressed)
    monkeypatch.setattr(ocr, "image_blocks", lambda content: ([{"text": "사진 지침", "page": 1,
                        "location": "ocr:1", "method": "ocr"}], "ocr_low_confidence") if content == png else ([], None))
    blocks, issues = embedded_blocks(Ole(), True, 1000)
    assert blocks[0]["location"] == "BinData/BIN0001.PNG/ocr:1" and "page" not in blocks[0]
    assert {i["error"] for i in issues} == {"hwp_image_placement_requires_review", "ocr_low_confidence"}
    assert embedded_blocks(Ole(), True, 4)[0] == []


@pytest.mark.parametrize("proof,expected", [
    ("500 자 이 상 (글자수 미달시 탈락)", {"min_length": 500, "length_unit": "characters"}),
    ("총 3장 이내로 작성", {"max_length": 3, "length_unit": "pages"}),
    ("A4 3장 내외", {}),
    ("필수, 공백 포함, 500자 이내", {"required": True, "includes_spaces": True, "max_length": 500, "length_unit": "characters"}),
])
def test_reviewed_real_constraint_wording(proof, expected):
    assert parse_constraints(proof) == (expected, [])


def test_shared_total_is_not_silently_assigned_to_each_question(monkeypatch):
    text = "지원동기 총 3장 이내로 작성 / 아래 질문에 답변해주세요"
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": "지원동기", "field_type": "essay", "max_length": 3, "length_unit": "pages", "evidence": text}]}))
    result = extract_structured(text)
    field = result["application_fields"][0]
    assert field["max_length"] is None and field["length_unit"] is None
    assert field["shared_constraints"] == {"max_length": 3, "length_unit": "pages"}
    assert "shared_length_scope_requires_review" in field["constraint_issues"]
