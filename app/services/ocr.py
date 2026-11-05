"""Tesseract TSV OCR. 실행 파일/언어/렌더러 미설치와 실패를 명시한다."""
import csv
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import importlib.util

from app.core.config import settings


def _run(args):
    options = {"capture_output": True, "timeout": settings.ocr_timeout_sec, "check": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **options)


def executable_path():
    executable = shutil.which(settings.ocr_command)
    if not executable and os.name == "nt" and settings.ocr_command == "tesseract":
        local = Path(__file__).resolve().parents[2] / "data/tools/tesseract/tesseract.exe"
        if local.is_file():
            executable = str(local)
    return executable


def _filesystem_path(path):
    # Windows Tesseract의 std::filesystem은 한글 절대 경로를 거부할 수 있다.
    # 작업 디렉터리와 공통된 사용자 경로를 상대 경로로 제거한다.
    if os.name == "nt":
        try:
            return os.path.relpath(path)
        except ValueError:
            pass
    return str(path)


def _language_args(executable):
    directory = Path(executable).resolve().parent / "tessdata"
    return ["--tessdata-dir", _filesystem_path(directory)] if directory.is_dir() else []


def renderer_backend():
    requested = settings.pdf_render_backend
    if requested not in ("auto", "poppler", "pdfium"):
        return None
    if requested in ("auto", "poppler") and shutil.which(settings.pdf_render_command):
        return "poppler"
    if requested in ("auto", "pdfium") and importlib.util.find_spec("pypdfium2"):
        return "pdfium"
    return None


def runtime_status():
    """실행 파일 존재뿐 아니라 버전과 요청한 언어 데이터도 확인한다."""
    executable = executable_path() if settings.ocr_enabled else None
    status = {"enabled": settings.ocr_enabled, "ocr_executable": executable,
              "languages_requested": settings.ocr_languages.split("+"),
              "languages_available": [], "ocr_ready": False,
              "pdf_backend": renderer_backend(), "issues": []}
    if not executable:
        status["issues"].append("ocr_engine_unavailable" if settings.ocr_enabled else "ocr_disabled")
        return status
    try:
        version = _run([executable, "--version"])
        status["ocr_version"] = version.stdout.decode("utf-8", errors="replace").splitlines()[0]
        languages = _run([executable, *_language_args(executable), "--list-langs"]).stdout.decode("utf-8", errors="replace").splitlines()[1:]
        status["languages_available"] = [line.strip() for line in languages if line.strip()]
        missing = set(status["languages_requested"]) - set(status["languages_available"])
        status["ocr_ready"] = not missing
        if missing:
            status["issues"].append("ocr_language_data_missing")
    except (subprocess.SubprocessError, OSError, IndexError):
        status["issues"].append("ocr_execution_failed")
    if not status["pdf_backend"]:
        status["issues"].append("pdf_renderer_unavailable")
    return status


def image_blocks(content: bytes) -> tuple[list[dict], str | None]:
    executable = executable_path() if settings.ocr_enabled else None
    if not executable:
        return [], "ocr_engine_unavailable"
    try:
        with tempfile.TemporaryDirectory(prefix="scholarship_ocr_") as temporary:
            path = Path(temporary) / "input.image"
            path.write_bytes(content)
            result = _run([executable, _filesystem_path(path), "stdout", *_language_args(executable), "-l", settings.ocr_languages, "tsv"])
        # Tesseract TSV는 따옴표를 CSV처럼 이스케이프하지 않는다.
        rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE)
        lines = {}
        for row in rows:
            if not row.get("text", "").strip():
                continue
            confidence = float(row["conf"])
            if confidence < 0:
                continue
            key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
            lines.setdefault(key, []).append(row)
        blocks = []
        for key, words in lines.items():
            boxes = [(int(w["left"]), int(w["top"]), int(w["width"]), int(w["height"])) for w in words]
            left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
            blocks.append({"text": " ".join(w["text"] for w in words), "method": "ocr", "kind": "line",
                           "location": "ocr:" + ":".join(key), "page": int(key[0]),
                           "confidence": sum(float(w["conf"]) for w in words) / len(words),
                           "bbox": [left, top, max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)]})
        if not blocks:
            return [], "ocr_no_text"
        if any(b["confidence"] < settings.ocr_min_confidence for b in blocks):
            return blocks, "ocr_low_confidence"
        return blocks, None
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        return [], "ocr_execution_failed"


def pdf_page_blocks(content: bytes, page: int) -> tuple[list[dict], str | None]:
    backend = renderer_backend()
    if not backend:
        return [], "pdf_renderer_unavailable"
    if backend == "pdfium":
        # 별도 프로세스에서 렌더링하여 손상 PDF의 시간·픽셀 한도를 적용한다.
        import sys
        try:
            with tempfile.TemporaryDirectory(prefix="scholarship_pdf_") as temporary:
                source, target = Path(temporary) / "source.pdf", Path(temporary) / "page.png"
                source.write_bytes(content)
                _run([sys.executable, "-m", "app.scripts.render_pdf_page", str(source), str(target),
                      str(page), str(settings.ocr_max_render_pixels)])
                blocks, issue = image_blocks(target.read_bytes())
                for block in blocks:
                    block.update(page=page, location=f"page:{page}/{block['location']}", renderer="pdfium", render_dpi=200)
                return blocks, issue
        except (subprocess.SubprocessError, OSError):
            return [], "pdf_render_failed"
    renderer = shutil.which(settings.pdf_render_command)
    try:
        with tempfile.TemporaryDirectory(prefix="scholarship_pdf_") as temporary:
            source, target = Path(temporary) / "source.pdf", Path(temporary) / "page"
            source.write_bytes(content)
            _run([renderer, "-f", str(page), "-l", str(page), "-r", "200", "-singlefile", "-png", str(source), str(target)])
            blocks, issue = image_blocks(target.with_suffix(".png").read_bytes())
            for block in blocks:
                block.update(page=page, location=f"page:{page}/{block['location']}", render_dpi=200)
            return blocks, issue
    except (subprocess.SubprocessError, OSError):
        return [], "pdf_render_failed"
