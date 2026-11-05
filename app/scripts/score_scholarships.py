import argparse
import json
from pathlib import Path
from app.services.scholarship_evaluation import evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output)
    run_path = root / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else None
    if run and run.get("state") != "complete":
        raise ValueError("incomplete or failed run cannot be scored as a final evaluation")
    capture_only = bool(run and run.get("capture_only"))
    gold = {} if capture_only else json.loads((root / "gold.json").read_text(encoding="utf-8"))
    if run and run.get("evaluated_ids"):
        ids = set(run["evaluated_ids"])
        gold = {**gold, **{group: [item for item in gold.get(group, []) if str(item["id"]) in ids]
                          for group in ("deadlines", "facts", "forms")}}
    results = evaluate(root, gold, include_unstructured_captures=bool(run))
    if run:
        results.update(evaluation_mode=run["mode"], capture_only=capture_only, evaluated_ids=run.get("evaluated_ids"))
    (root / "metrics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k.endswith("recall") or k.endswith("retrieval") or k in ("prediction_count", "field_label_recall_upper_bound", "forms_with_all_reviewed_fields", "complete_detail_fetches", "linked_asset_parsed_without_issue", "download_failure_types")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
