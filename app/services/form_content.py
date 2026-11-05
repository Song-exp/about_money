"""선택된 원문 지침의 명시적인 항목 수를 수집·초안 검사에 연결한다."""
import re

from app.services.document_layout import compact


def enrich_content(field):
    label = compact(field["label"]).strip("():：")
    if field["field_type"] != "essay" or not label:
        return
    proofs = field.get("instruction_sources", []) or ([field["instruction_source"]] if field.get("instruction_source") else [])
    proofs = [*proofs, {**field["source"], "evidence": field["evidence"]}]
    rules = []
    for proof in proofs:
        # Exact title followed by the rule prevents a neighbouring topic's rule leaking.
        match = re.search(re.escape(label) + r"(?:은|는|을|를)?(?P<n>\d+)가지이상(?:제시|작성|기재)", compact(proof.get("evidence", "")))
        if not match or not 1 <= int(match["n"]) <= 10:
            continue
        rule = {"kind": "distinct_items", "minimum": int(match["n"]), "evidence": proof["evidence"], "source": proof}
        if not any(r["minimum"] == rule["minimum"] for r in rules):
            rules.append(rule)
    if rules:
        field["content_requirements"] = rules
        if len(rules) > 1:
            field.setdefault("constraint_issues", []).append("conflicting_content_item_counts")


def item_minimum(field):
    values = {r["minimum"] for r in field.get("content_requirements", []) if r.get("kind") == "distinct_items"}
    return next(iter(values)) if len(values) == 1 else None


def duplicate_slot(field, facts):
    minimum = item_minimum(field)
    if not minimum:
        return None
    seen = set()
    for index in range(1, minimum + 1):
        key = f"original_item_{index}"
        text = compact(facts.get(key, {}).get("text", ""))
        if text and text in seen:
            return key
        seen.add(text)
    return None


def validate_items(field, text):
    minimum = item_minimum(field)
    if not minimum:
        return []
    items = re.findall(r"(?:^|\n)\s*\d+[.)]\s*(.*?)(?=\n\s*\d+[.)]\s*|\Z)", text, re.S)
    values = [compact(item) for item in items if compact(item)]
    errors = []
    if len(values) < minimum:
        errors.append("missing_original_content_items")
    if len(set(values)) < len(values):
        errors.append("duplicate_original_content_items")
    return errors
