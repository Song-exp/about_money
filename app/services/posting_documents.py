"""파일별 구조화 결과를 보존하고 공고 단위로 병합한다."""
import hashlib
from copy import deepcopy
from datetime import datetime

from app.services.posting_extract import EXTRACTOR_VERSION, extract_structured, merge_deadline_candidates, KST
from app.services.document_layout import compact


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def structure_documents(raw, previous_documents=None, *, trace_sink=None):
    def key(d, digest):
        return (digest, d.get("filename"), d.get("parser_version"), d.get("url"), d.get("member_path"))

    previous = {key(d, d.get("sha256")): d for d in previous_documents or []}
    for document in raw.source_documents:
        text = document.get("extracted_text")
        if not text:
            continue
        filename = document.get("filename") or ""
        document["document_role"] = ("application_form" if any(word in filename for word in ("신청서", "지원서", "양식", "서식", "자기소개", "활동계획")) else
                                     "announcement" if document.get("kind") == "html" else "unknown")
        digest = hashlib.sha256(document["content"]).hexdigest() if "content" in document else None
        old = previous.get(key(document, digest))
        if old and old.get("structured", {}).get("extractor_version") == EXTRACTOR_VERSION and not old["structured"].get("issues"):
            document["structured"] = old["structured"]
        else:
            source = {"url": document["url"], "filename": document.get("filename"), "sha256": digest,
                      "member_path": document.get("member_path")}
            document["structured"] = _json_safe(extract_structured(text, source, document.get("blocks"), trace_sink=trace_sink,
                                                                  spreadsheet_inventory=document.get('spreadsheet_inventory')))
        if document["structured"].get("issues"):
            document.update(status="needs_review", error="structured_validation_failed")
            raw.fetch_status = "partial"


def merge_posting_documents(raw) -> dict:
    results = [d["structured"] for d in raw.source_documents if d.get("structured")]
    if not results:
        # 목록만 있는 게시글을 양식 확인 완료로 취급하지 않는다.
        results = [extract_structured(raw.body_text, {"url": raw.source_url})]
    merged = {"eligibility": {}, "benefit": {}, "required_documents": [], "application_fields": [], "form_groups": [], "form_exclusions": [], "issues": [], "facts": [], "coverage": []}
    deadlines = []
    for result in results:
        merged["issues"].extend(result.get("issues", []))
        merged["facts"].extend(result.get("facts", []))
        merged["coverage"].append(result.get("coverage", {}))
        merged["form_groups"].extend(deepcopy(result.get("form_groups", [])))
        merged["form_exclusions"].extend(result.get("form_exclusions", []))
        for group in ("eligibility", "benefit"):
            for key, value in result.get(group, {}).items():
                if key in merged[group] and merged[group][key] != value:
                    merged["issues"].append(f"conflicting_{group}_{key}")
                    merged[group].pop(key, None)
                elif f"conflicting_{group}_{key}" not in merged["issues"]:
                    merged[group][key] = value
        merged["required_documents"].extend(result.get("required_documents", []))
        merged["application_fields"].extend(result.get("application_fields", []))
        if result.get("deadline_at"):
            value = result["deadline_at"]
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    merged['issues'].append('invalid_student_deadline')
                    continue
            if not isinstance(value, datetime):
                merged['issues'].append('invalid_student_deadline')
                continue
            value = value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)
            deadlines.append((value, result.get("deadline_source")))
    value, source, error = merge_deadline_candidates(deadlines)
    if error:
        merged['issues'].append(error)
    elif value:
        merged['deadline_at'], merged['deadline_source'] = value, source
    seen, fields, decisions, aliases = {}, [], [], {}
    for original in merged["application_fields"]:
        field = dict(original)
        layout = field.get("layout", {})
        key = (layout["table_fingerprint"], layout.get("table_occurrence"), layout.get("row"), layout.get("column"),
               layout.get("cell_paragraph", 0), layout.get("block_offset"),
               compact(field["label"]), field.get("role", "unknown")) if layout.get("table_fingerprint") else (field["id"],)
        existing = seen.get(key)
        if existing:
            aliases[field["id"]] = existing["id"]
            constraints = ("field_type", "required", "min_length", "max_length", "length_unit", "includes_spaces", "instructions", "options",
                           "section", "applicability", "semantic_kind", "constraint_issues", "content_requirements", "history_reuse_policy", "subject", "selection_mode", "choice_spec", "input_spec", "input_cell_source", "input_cell_sources", "manual_spec", "original_destination", "destination_version", "preparation_spec")
            def normalized(name, value):
                if name == "options":
                    return sorted(set(compact(option) for option in value or []))
                if name == "instructions":
                    return compact(value or "")
                if name == "section":
                    return (value or {}).get("kind")
                if name == "subject":
                    return compact((value or {}).get("label", ""))
                if name == "manual_spec":
                    return ((value or {}).get("version"), (value or {}).get("mode"), (value or {}).get("action"), [compact(s.get("evidence", "")) for s in (value or {}).get("sources", [])])
                if name == "original_destination":
                    return ((value or {}).get("version"), (value or {}).get("kind"), (value or {}).get("visual_placement_verified"), (value or {}).get("cell_geometry_verified"), (value or {}).get("contains_controls"), compact((value or {}).get("source", {}).get("evidence", "")))
                if name == "input_cell_sources":
                    return [tuple(c.get(k) for k in ("row", "column", "row_span", "column_span", "blank", "has_control")) for c in value or []]
                if name == "input_cell_source":
                    return tuple((value or {}).get(k) for k in ("row", "column", "row_span", "column_span", "blank", "has_control"))
                if name == "choice_spec":
                    def slot_signature(v):
                        return (v["key"], v["kind"], v["unit"], v.get("placement"), v.get("maximum"))
                    return ((value or {}).get("version"), (value or {}).get("multiple_explicit"), (value or {}).get("allow_empty"), (value or {}).get("mark"),
                            [(compact(c["caption"]), [slot_signature(v) for v in c["values"]])
                             for c in (value or {}).get("choices", [])],
                            [slot_signature(v) for v in (value or {}).get("empty_values", [])])
                if name == "input_spec":
                    if (value or {}).get('version') in ('spreadsheet-education-v1', 'spreadsheet-repeating-v1', 'spreadsheet-recommendation-v1'):
                        return value
                    repeat = (value or {}).get("repeat", {})
                    def signature(s):
                        return (s["key"], compact(s["label"]), s.get("kind"), s.get("unit"), s.get("options"), s.get("scale_key"), s.get("service_only"), s.get("maximum"))
                    return ((value or {}).get("version"), (value or {}).get("person_row"), (value or {}).get("preparation_only"), (value or {}).get("paragraph_template"), (value or {}).get("original_layout_review_required"), [signature(s) for s in (value or {}).get("slots", [])],
                            [signature(s) for s in (value or {}).get("ready_slots", [])],
                            repeat.get("count_key"), repeat.get("collection_limit"), repeat.get("original_layout_review_required"),
                            [(s["key"], compact(s["label"]), s.get("kind"), s.get("unit")) for s in repeat.get("columns", [])],
                            [compact(t["year_source"]["evidence"]) for t in repeat.get("templates", [])])
                if name == "applicability":
                    return ((value or {}).get("mode"), compact((value or {}).get("evidence", "")))
                if name == "preparation_spec":
                    v = value or {}
                    return (tuple(v.get(k) for k in ("version", "user_report_only", "file_verified", "example_only", "economic_rule_review_required", "automatic_marking")),
                            v.get("original_marks"), compact((v.get("example_source") or {}).get("evidence", "")),
                            [[compact(s.get("evidence", "")) for s in v.get(k, [])] for k in ("document_sources", "header_sources")])
                if name == "content_requirements":
                    return sorted((r.get("kind"), r.get("minimum"), compact(r.get("evidence", ""))) for r in value or [])
                if name == "history_reuse_policy":
                    return ((value or {}).get("mode"), sorted(compact(s.get("evidence", "")) for s in (value or {}).get("sources", [])))
                return value
            variants = existing.get("attribute_variants", [deepcopy(existing)])
            conflicts = [k for k in constraints if any(normalized(k, v.get(k)) != normalized(k, field.get(k)) for v in variants)]
            existing.setdefault("alternative_sources", []).append(field.get("source", {}))
            if conflicts:
                merged["issues"].append("conflicting_duplicate_form_field")
                existing["attribute_variants"] = [*variants, deepcopy(field)]
                existing["attribute_conflicts"] = sorted(set(existing.get("attribute_conflicts", [])) | set(conflicts))
                existing["validation_status"] = "conflict_requires_review"
                # The canonical item represents one physical input. Nullable constraints are
                # withheld until review; all proposed values and evidence stay in variants.
                for name in existing["attribute_conflicts"]:
                    if name in ("required", "min_length", "max_length", "length_unit", "includes_spaces"):
                        existing[name] = None
                    elif name in ("instructions", "options"):
                        existing[name] = "" if name == "instructions" else []
                decisions.append({"field_id": field["id"], "result": "merged_conflict", "into": existing["id"], "constraints": conflicts})
            else:
                if existing.get("attribute_variants"):
                    existing["attribute_variants"].append(deepcopy(field))
                decisions.append({"field_id": field["id"], "result": "merged", "into": existing["id"], "reason": "identical_table_and_field"})
        else:
            seen[key] = field
            fields.append(field)
            decisions.append({"field_id": field["id"], "result": "kept"})
    merged["application_fields"] = fields
    groups = {}
    for group in merged["form_groups"]:
        group["field_ids"] = [aliases.get(i, i) for i in group["field_ids"]]
        existing = groups.get(group["id"])
        if existing and existing != group:
            existing.setdefault("issues", []).append("conflicting_shared_form_group")
            existing["status"] = "needs_review"
            existing.setdefault("variants", []).append(group)
            merged["issues"].append("conflicting_shared_form_group")
        elif not existing:
            groups[group["id"]] = group
    merged["form_groups"] = list(groups.values())
    for field in fields:
        field["group_ids"] = [g["id"] for g in merged["form_groups"] if field["id"] in g["field_ids"]]
    merged["merge_decisions"] = decisions
    for index, field in enumerate(merged["application_fields"], 1):
        field = {**field, "order": index}
        merged["application_fields"][index - 1] = field
    merged["application_questions"] = [f["label"] for f in merged["application_fields"] if f["field_type"] == "essay"]
    merged["required_documents"] = list(dict.fromkeys(merged["required_documents"]))
    merged["issues"] = list(dict.fromkeys(merged["issues"]))
    return merged
