import io
import zipfile
from types import SimpleNamespace

import docx
from pypdf import PdfWriter

from app.services import ocr
from app.services.document_parser import parse_document
from app.services.source_assets import parse_asset
from app.utils.file_parser import extract_text_from_bytes


def test_docx_table_and_nested_table_are_in_document_order():
    document = docx.Document()
    document.add_paragraph("신청서 시작")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "지원동기 (필수, 500자 이내)"
    table.cell(0, 1).text = "학업계획 (공백 제외, 1000자 이내)"
    nested = table.cell(0, 1).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "장학금 활용계획"
    document.add_paragraph("신청서 끝")
    stream = io.BytesIO()
    document.save(stream)
    result = parse_document(stream.getvalue(), "신청서.docx")
    assert [b["text"] for b in result["blocks"]] == ["신청서 시작", "지원동기 (필수, 500자 이내)", "학업계획 (공백 제외, 1000자 이내)", "장학금 활용계획", "신청서 끝"]
    assert "cell:2" in result["blocks"][2]["location"]
    assert "장학금 활용계획" in extract_text_from_bytes(stream.getvalue(), "신청서.docx")


def test_hwpx_namespaces_cell_locations_entities_and_numeric_section_order():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for number in (10, 2):
            archive.writestr(f"Contents/section{number}.xml", f'''<x:sec xmlns:x="urn:owpml">
              <x:tbl><x:tr><x:tc><x:p><x:run><x:t>지원동기 {number} &amp; 계획</x:t></x:run></x:p></x:tc></x:tr></x:tbl></x:sec>''')
    result = parse_document(stream.getvalue(), "신청서.hwpx")
    assert [b["text"] for b in result["blocks"]] == ["지원동기 2 & 계획", "지원동기 10 & 계획"]
    assert "/tr:1/tc:1/p:1" in result["blocks"][0]["location"]
    assert extract_text_from_bytes(stream.getvalue(), "신청서.hwpx") == result["extracted_text"]


def test_pdf_keeps_read_pages_when_one_page_ocr_fails(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    monkeypatch.setattr(ocr, "pdf_page_blocks", lambda content, page: (
        ([{"text": "지원동기", "location": "page:1/ocr:1", "page": 1, "method": "ocr"}], None)
        if page == 1 else ([], "ocr_execution_failed")))
    result = parse_asset(stream.getvalue(), "양식.pdf", "https://x.test/form")[0]
    assert result["status"] == "needs_review"
    assert result["blocks"][0]["page"] == 1
    assert result["issues"][0]["location"] == "page:2"


def test_tesseract_tsv_keeps_confidence_and_coordinates(monkeypatch):
    tsv = "page_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t1\t1\t10\t20\t40\t15\t96\t학업계획\n"
    monkeypatch.setattr(ocr.shutil, "which", lambda command: "tesseract")
    calls = []
    def run(args):
        calls.append(args)
        return SimpleNamespace(stdout=tsv.encode())
    monkeypatch.setattr(ocr, "_run", run)
    blocks, issue = ocr.image_blocks(b"image")
    assert issue is None and blocks[0]["text"] == "학업계획"
    assert blocks[0]["bbox"] == [10, 20, 50, 35]
    assert "kor+eng" in calls[0]


def test_missing_ocr_and_low_confidence_remain_reviewable(monkeypatch):
    monkeypatch.setattr(ocr, "executable_path", lambda: None)
    assert ocr.image_blocks(b"image")[1] == "ocr_engine_unavailable"
    monkeypatch.setattr(ocr, "executable_path", lambda: "tesseract")
    tsv = "page_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t1\t1\t0\t0\t10\t10\t20\t불확실\n"
    monkeypatch.setattr(ocr, "_run", lambda args: SimpleNamespace(stdout=tsv.encode()))
    blocks, issue = ocr.image_blocks(b"image")
    assert blocks and issue == "ocr_low_confidence"


def test_tesseract_literal_quote_does_not_swallow_following_tsv_rows(monkeypatch):
    monkeypatch.setattr(ocr, "executable_path", lambda: "tesseract")
    tsv = ('page_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
           '1\t1\t1\t1\t0\t0\t10\t10\t95\t"\n'
           '1\t1\t1\t1\t10\t0\t10\t10\t95\t장학금\n'
           '1\t1\t1\t2\t0\t20\t10\t10\t95\t신청\n')
    monkeypatch.setattr(ocr, "_run", lambda args: SimpleNamespace(stdout=tsv.encode()))
    blocks, issue = ocr.image_blocks(b"image")
    assert issue is None
    assert [b["text"] for b in blocks] == ['" 장학금', '신청']
