import hashlib
import json

import pytest

from app.services.evaluation_runs import create_run, finish_run, fingerprint
from app.services.posting_extract import extract_structured
from app.services import llm, ocr
from app.scripts.remeasure_scholarships import replay_client


def baseline(tmp_path):
    root = tmp_path / "baseline"
    root.mkdir()
    (root / "gold.json").write_text('{"facts":[]}', encoding="utf-8")
    return root


def test_run_cannot_overwrite_baseline_or_another_run(tmp_path):
    root = baseline(tmp_path)
    original = fingerprint(root)
    with pytest.raises(ValueError, match="separate"):
        create_run(root, root / "new-run", "replay")
    output = tmp_path / "run"
    create_run(root, output, "replay")
    with pytest.raises(ValueError, match="already exists"):
        create_run(root, output, "replay")
    assert fingerprint(root) == original
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert manifest["database_written"] is False
    assert "deepseek_api_key" not in manifest["settings"]


def test_changed_baseline_marks_run_failed(tmp_path):
    root = baseline(tmp_path)
    output = tmp_path / "run"
    manifest = create_run(root, output, "replay")
    (root / "gold.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="baseline changed"):
        finish_run(root, output, manifest)
    assert json.loads((output / "run.json").read_text(encoding="utf-8"))["state"] == "failed"


def test_gold_proof_cannot_be_overwritten_by_new_extracted_text(tmp_path):
    root = baseline(tmp_path)
    folder = root / "notice-1"
    folder.mkdir()
    original = b"official reference"
    (folder / "extracted.txt").write_bytes(original)
    gold = {"facts": [{"id": 1, "proof": "notice-1/extracted.txt", "proof_sha256": hashlib.sha256(original).hexdigest()}]}
    (root / "gold.json").write_text(json.dumps(gold), encoding="utf-8")
    output = tmp_path / "run"
    create_run(root, output, "replay")
    (output / "notice-1").mkdir()
    (output / "notice-1/extracted.txt").write_text("new extraction", encoding="utf-8")
    proof = json.loads((output / "gold.json").read_text(encoding="utf-8"))["facts"][0]
    assert hashlib.sha256((output / proof["proof"]).read_bytes()).hexdigest() == proof["proof_sha256"]


def test_replay_unicode_urls_use_original_bytes_without_network(tmp_path):
    root = baseline(tmp_path)
    folder = root / "notice-1"
    folder.mkdir()
    content = b"original image"
    (folder / "original.bin").write_bytes(content)
    url = "https://com.khu.ac.kr/장학금 포스터.png"
    (folder / "capture.json").write_text(json.dumps({"documents": [{
        "url": url, "kind": "image", "filename": "포스터.png", "local_file": "original.bin",
        "sha256": hashlib.sha256(content).hexdigest()}]}), encoding="utf-8")
    factory, requests = replay_client(root)
    with factory() as client:
        assert client.get(url).content == content
        assert client.get("https://unavailable.test/file").status_code == 404
    assert requests[0]["available"] is True
    assert requests[1]["available"] is False


def test_trace_distinguishes_schema_evidence_duplicate_and_accepted(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    accepted = {"label": "성명", "field_type": "text", "evidence": "성명"}
    candidate = {"application_fields": [accepted, accepted,
                 {"label": "직업", "field_type": "invalid", "evidence": "직업"},
                 {"label": "주소", "evidence": "없는 주소"}]}
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: json.dumps(candidate))
    trace = []
    result = extract_structured("성명\n직업\n주소", trace_sink=trace.append)
    assert len(result["application_fields"]) == 1
    decisions = trace[0]["field_decisions"]
    assert [d["result"] for d in decisions] == ["accepted", "deduplicated", "rejected", "rejected"]
    assert decisions[2]["reason"] == "invalid_application_field"
    assert decisions[3]["reason"] == "field_not_supported_by_evidence"
    assert trace[0]["model_response"]


def test_runtime_checks_missing_korean_data(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(ocr, "executable_path", lambda: "tesseract")
    monkeypatch.setattr(ocr, "_run", lambda args: SimpleNamespace(stdout=(
        b"tesseract 5\n" if "--version" in args else b'List of languages:\neng\n')))
    status = ocr.runtime_status()
    assert status["ocr_ready"] is False
    assert "ocr_language_data_missing" in status["issues"]


def test_pdfium_bounds_reject_invalid_page_and_oversized_image(tmp_path):
    from pypdf import PdfWriter
    from app.scripts.render_pdf_page import render
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    source, target = tmp_path / "source.pdf", tmp_path / "page.png"
    writer.write(source)
    with pytest.raises(ValueError, match="page_out_of_range"):
        render(source, target, 2, 1_000_000)
    with pytest.raises(ValueError, match="pixel_limit"):
        render(source, target, 1, 100)
    render(source, target, 1, 1_000_000)
    assert target.read_bytes().startswith(b"\x89PNG")
