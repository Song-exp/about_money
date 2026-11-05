"""공개 공고 평가 실행의 격리·출처 지문. 운영 사용자 데이터는 읽지 않는다."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
from datetime import datetime, timezone

from app.core.config import settings
from app.services.document_parser import PARSER_VERSION
from app.services.posting_extract import EXTRACTOR_VERSION, _SYSTEM
from app.services.ocr import runtime_status


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               default=lambda v: v.isoformat()), encoding="utf-8")


def fingerprint(root):
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def create_run(baseline, output, mode):
    baseline, output = Path(baseline).resolve(), Path(output).resolve()
    if not baseline.is_dir() or not (baseline / "gold.json").is_file():
        raise ValueError("baseline with gold.json required")
    if output == baseline or baseline in output.parents or output in baseline.parents:
        raise ValueError("evaluation output must be separate from baseline")
    if output.exists():
        raise ValueError("evaluation output already exists; choose a new run directory")
    source_hashes = fingerprint(baseline)
    output.mkdir(parents=True)
    gold = json.loads((baseline / "gold.json").read_text(encoding="utf-8"))
    paths = {"reference-listing.json", "database-before.json", "listing-metrics.json"}
    paths.update(p.name for p in baseline.glob("listing-*.html"))
    # 정답 근거를 새 추출 텍스트/첨부 출력과 다른 경로에 고정한다.
    proofs = {item["proof"] for group in ("deadlines", "facts", "forms")
              for item in gold.get(group, []) if item.get("proof")}
    for name in proofs:
        source = (baseline / name).resolve()
        if baseline not in source.parents:
            raise ValueError("proof path outside baseline")
        target = output / "_reference" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for group in ("deadlines", "facts", "forms"):
        for item in gold.get(group, []):
            if item.get("proof"):
                item["proof"] = "_reference/" + item["proof"]
    write_json(output / "gold.json", gold)
    for name in paths:
        source = (baseline / name).resolve()
        if baseline not in source.parents:
            raise ValueError("proof path outside baseline")
        if source.is_file():
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    project = Path(__file__).resolve().parents[2]
    versions = {}
    for name in ("pypdfium2", "Pillow", "pypdf", "openai", "openpyxl", "xlrd", "olefile"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True)
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "state": "running", "mode": mode,
                "baseline": str(baseline), "baseline_sha256": source_hashes,
                "gold_sha256": source_hashes["gold.json"],
                "gold_proof_scope": "Original gold evidence copied under _reference; values unchanged",
                "code_sha256": {str(p.relative_to(project)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((project / "app").rglob("*.py"))},
                "git_head": git.stdout.strip() if git.returncode == 0 else None,
                "python": platform.python_version(), "packages": versions,
                "parser_version": PARSER_VERSION, "extractor_version": EXTRACTOR_VERSION,
                "prompt_sha256": hashlib.sha256(_SYSTEM.encode()).hexdigest(),
                "model": settings.llm_model, "llm_configured": bool(settings.deepseek_api_key),
                "settings": {name: getattr(settings, name) for name in (
                    "crawl_external_hosts", "crawl_max_document_mb", "crawl_max_related_links", "crawl_max_archive_members",
                    "ocr_enabled", "ocr_languages", "ocr_timeout_sec", "ocr_min_confidence", "pdf_render_backend", "ocr_max_render_pixels")},
                "document_runtime": runtime_status(), "database_written": False}
    write_json(output / "run.json", manifest)
    return manifest


def finish_run(baseline, output, manifest, *, error=None):
    unchanged = fingerprint(Path(baseline).resolve()) == manifest["baseline_sha256"]
    manifest.update(finished_at=datetime.now(timezone.utc).isoformat(), baseline_unchanged=unchanged,
                    state="failed" if error or not unchanged else "complete", error=error)
    write_json(Path(output) / "run.json", manifest)
    if not unchanged:
        raise RuntimeError("baseline changed during evaluation")
