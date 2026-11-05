"""불변 최초 평가에 대한 별도 실행. 원문 재생과 실제 사이트 재수집을 구분한다."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.crawlers.base import RawPosting
from app.crawlers.public import KhuScholarshipCrawler
from app.crawlers.registry import _enrich_from_body
from app.models.scholarship import Category, SourceType
from app.services.evaluation_runs import create_run, finish_run, write_json, fingerprint
from app.services.document_parser import PARSER_VERSION
from app.services.posting_documents import structure_documents
from app.services.scholarship_evaluation import evaluate


def replay_client(baseline):
    catalog = {}
    for folder in baseline.glob("notice-*"):
        capture_path = folder / "capture.json"
        if not capture_path.exists():
            continue
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        for document in capture["documents"]:
            if document.get("local_file") and not document.get("member_path"):
                content = (folder / document["local_file"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != document["sha256"]:
                    raise ValueError("replay source checksum mismatch")
                catalog[str(httpx.URL(document["url"]))] = (content, document.get("filename"), document.get("kind"))
        reference_path = folder / "reference-images.json"
        if reference_path.exists():
            for image in json.loads(reference_path.read_text(encoding="utf-8")):
                content = (folder / image["local_file"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != image["sha256"]:
                    raise ValueError("replay reference image checksum mismatch")
                catalog[str(httpx.URL(image["source_url"]))] = (content, image["local_file"], "image")
    requests = []
    def transport(request):
        url = str(request.url)
        requests.append({"url": url, "available": url in catalog})
        if url not in catalog:
            return httpx.Response(404, text="No saved original; no network fallback")
        content, filename, kind = catalog[url]
        headers = {"content-type": "text/html" if kind == "html" else "image/png" if kind == "image" else "application/octet-stream"}
        if filename:
            headers["content-disposition"] = "attachment; filename*=UTF-8''" + quote(filename)
        return httpx.Response(200, content=content, headers=headers)
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(transport), **kwargs)
    return factory, requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--parsed-input", help="Repeat real model extraction against a completed run's identical parsed inputs")
    args = parser.parse_args()
    baseline, output = Path(args.baseline).resolve(), Path(args.output).resolve()
    manifest = create_run(baseline, output, args.mode)
    manifest["capture_only"] = args.capture_only
    parsed_input = Path(args.parsed_input).resolve() if args.parsed_input else None
    if parsed_input:
        if args.mode != "replay" or not parsed_input.is_dir() or parsed_input == output:
            raise ValueError("parsed-input requires a separate replay input directory")
        prior = json.loads((parsed_input / "run.json").read_text(encoding="utf-8"))
        if prior.get("state") != "complete" or prior.get("parser_version") != PARSER_VERSION:
            raise ValueError("parsed-input must be complete with current parser version")
        if prior.get("baseline_sha256") != manifest["baseline_sha256"]:
            raise ValueError("parsed-input baseline differs")
        manifest.update(parsed_input=str(parsed_input), parsed_input_sha256=fingerprint(parsed_input),
                        input_scope="Reused exact parsed blocks/text; real model calls repeated, no download/OCR repetition")
    factory, requests = replay_client(baseline) if args.mode == "replay" else (None, [])
    crawler = KhuScholarshipCrawler()
    started = time.monotonic()
    error = None
    try:
        if args.mode == "live":
            crawler.preflight(respect_robots=settings.crawl_respect_robots)
        prediction_paths = sorted(baseline.glob("notice-*/prediction.json"))
        if args.ids:
            requested = set(args.ids)
            prediction_paths = [p for p in prediction_paths if p.parent.name.removeprefix("notice-") in requested]
            if len(prediction_paths) != len(requested):
                raise ValueError("unknown or duplicate requested IDs")
        if not prediction_paths:
            raise ValueError("baseline has no evaluated notices")
        if not args.capture_only and not settings.deepseek_api_key:
            raise ValueError("real model configuration is required for full evaluation")
        sample_results = []
        for prediction_path in prediction_paths:
            before = json.loads(prediction_path.read_text(encoding="utf-8"))
            original = json.loads((prediction_path.parent / "capture.json").read_text(encoding="utf-8"))
            folder = output / prediction_path.parent.name
            folder.mkdir(exist_ok=True)
            raw = RawPosting(title=before["title"], source_url=before["source_url"], source_platform="khu_janghak",
                             source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP)
            print(f"start id={before['id']} mode={args.mode}", flush=True)
            sample_start = time.monotonic()
            if parsed_input:
                saved = json.loads((parsed_input / prediction_path.parent.name / "capture.json").read_text(encoding="utf-8"))
                raw.source_documents = saved["documents"]
                for document in raw.source_documents:
                    if document.get("local_file"):
                        content = (parsed_input / prediction_path.parent.name / document["local_file"]).read_bytes()
                        if hashlib.sha256(content).hexdigest() != document["sha256"]:
                            raise ValueError("parsed-input document checksum mismatch")
                        document["content"] = content
                raw.body_text = (parsed_input / prediction_path.parent.name / "extracted.txt").read_text(encoding="utf-8")
                raw.fetch_status, raw.fetch_error = saved["fetch_status"], saved.get("fetch_error")
            else:
                crawler.fetch_detail(raw, client_factory=factory)
            documents = []
            for index, document in enumerate(raw.source_documents):
                item = {k: v for k, v in document.items() if k != "content"}
                if "content" in document:
                    digest = hashlib.sha256(document["content"]).hexdigest()
                    filename = f"asset-{index}-{digest[:12]}.bin"
                    (folder / filename).write_bytes(document["content"])
                    item.update(sha256=digest, local_file=filename)
                documents.append(item)
            expected_assets = sorted({d["url"] for d in original["documents"] if d.get("kind") != "html" and not d.get("member_path")})
            capture = {"id": before["id"], "title": raw.title, "source_url": raw.source_url,
                       "fetch_status": raw.fetch_status, "fetch_error": raw.fetch_error,
                       "expected_attachments": original["expected_attachments"],
                       "expected_related_assets": expected_assets, "documents": documents}
            write_json(folder / "capture.json", capture)
            (folder / "extracted.txt").write_text(raw.body_text, encoding="utf-8")
            with (folder / "trace.jsonl").open("w", encoding="utf-8") as trace_file:
                def trace(event):
                    trace_file.write(json.dumps(event, ensure_ascii=False, default=lambda v: v.isoformat()) + "\n")
                    trace_file.flush()
                trace({"stage": "fetch_parse", "id": before["id"], "fetch_status": raw.fetch_status,
                       "documents": [{k: d.get(k) for k in ("url", "filename", "kind", "status", "error", "ocr_error")}
                                     | {"block_count": len(d.get("blocks", [])), "text_length": len(d.get("extracted_text", ""))}
                                     for d in documents]})
                if not args.capture_only:
                    structure_documents(raw, trace_sink=trace)
                    before_merge = [f["id"] for d in raw.source_documents for f in d.get("structured", {}).get("application_fields", [])]
                    _enrich_from_body(raw)
                    after_merge = [f["id"] for f in raw.eligibility.get("application_fields", [])]
                    trace({"stage": "merge", "fields_before": before_merge, "fields_after": after_merge,
                           "removed_occurrences": dict(Counter(before_merge) - Counter(after_merge)),
                           "decisions": raw.eligibility.get("field_merge_decisions", []),
                           "issues": raw.eligibility.get("extraction_issues", [])})
                    write_json(folder / "prediction.json", {"id": before["id"], "title": raw.title, "source_url": raw.source_url,
                               "deadline_at": raw.deadline_at, "eligibility": raw.eligibility, "benefit": raw.benefit,
                               "required_documents": raw.required_documents, "status": raw.fetch_status,
                               "documents": [{k: v for k, v in d.items() if k != "content"} for d in raw.source_documents]})
            sample_results.append({"id": before["id"], "seconds": round(time.monotonic() - sample_start, 2),
                                   "documents": len(documents), "fetch_status": capture["fetch_status"],
                                   "fields": len(raw.eligibility.get("application_fields", []))})
            print(json.dumps(sample_results[-1]), flush=True)
            manifest["samples"] = sample_results
            write_json(output / "run.json", manifest)
        gold = json.loads((output / "gold.json").read_text(encoding="utf-8")) if not args.capture_only else {}
        manifest["evaluated_ids"] = [str(s["id"]) for s in sample_results]
        if args.ids:
            gold = {**gold, **{group: [item for item in gold.get(group, []) if str(item["id"]) in args.ids]
                              for group in ("deadlines", "facts", "forms")}}
        metrics = evaluate(output, gold, include_unstructured_captures=True)
        metrics["evaluation_mode"] = args.mode
        metrics["capture_only"] = args.capture_only
        metrics["evaluated_ids"] = manifest["evaluated_ids"]
        write_json(output / "metrics.json", metrics)
        manifest.update(samples=sample_results, elapsed_seconds=round(time.monotonic() - started, 2),
                        replay_request_count=len(requests), replay_missing=[r for r in requests if not r["available"]])
        print(json.dumps({k: v for k, v in metrics.items() if k.endswith("retrieval") or k.endswith("recall")
                          or k in ("field_label_recall_upper_bound", "download_failure_types")}), flush=True)
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        if parsed_input and fingerprint(parsed_input) != manifest["parsed_input_sha256"]:
            error = "parsed input changed during repeat"
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 2)
        finish_run(baseline, output, manifest, error=error)


if __name__ == "__main__":
    main()
