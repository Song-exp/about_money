"""보관한 공적 빈 신청 양식 1개를 현재 LLM에 재입력하여 거절 원인을 기록한다.

1차 prediction과 정확도 집계는 변경하지 않는다. 사용자 신청서에는 사용하지 않는다.
"""
import argparse
import json
import re
from pathlib import Path
from pydantic import ValidationError
from app.services import llm
from app.services.posting_extract import _SYSTEM, ApplicationField
from app.services.fact_validation import grounded_facts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-folder", required=True)
    parser.add_argument("--document-index", type=int, required=True)
    args = parser.parse_args()
    folder = Path(args.notice_folder)
    capture = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    document = capture["documents"][args.document_index]
    text = document["extracted_text"]
    if len(text) > 8000:
        raise ValueError("Choose a single short public form for diagnosis")
    raw = llm.chat_complete(_SYSTEM, f"[문서 일부]\n{text}", temperature=0)
    data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    _, _, fact_issues = grounded_facts(data, text, {"sha256": document["sha256"]})
    fields = []
    for field in data.get("application_fields", []):
        errors = []
        try:
            ApplicationField.model_validate(field)
        except ValidationError as exc:
            errors = [{"location": error["loc"], "type": error["type"]} for error in exc.errors()]
        evidence = field.get("evidence")
        exact = isinstance(evidence, str) and evidence in text
        normalized = isinstance(evidence, str) and re.sub(r"[\s\x00]+", "", evidence) in re.sub(r"[\s\x00]+", "", text)
        fields.append({"label": field.get("label"), "schema_errors": errors, "exact_evidence_match": exact, "normalized_evidence_match": normalized})
    result = {"source_url": capture["source_url"], "filename": document["filename"], "source_sha256": document["sha256"],
              "model_response": data, "field_diagnostics": fields, "fact_issues": fact_issues,
              "scope": "Additional live model diagnostic; excluded from first-pass accuracy metrics"}
    (folder / "diagnostic-model-response.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"fields": len(fields), "schema_invalid_fields": sum(bool(f["schema_errors"]) for f in fields),
                      "whitespace_only_evidence_rejections": sum(not f["exact_evidence_match"] and f["normalized_evidence_match"] for f in fields), "fact_issues": fact_issues}))


if __name__ == "__main__":
    main()
