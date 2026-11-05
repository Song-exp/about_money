"""확인한 사실의 문장만 구성에 사용하고 문항의 분량을 검사한다."""
import hashlib
import json
import re
from app.services import llm
from app.services.writing_requirements import requirements, field_label
from app.utils.pii import mask_pii
from app.services.document_layout import compact
from app.services.form_content import item_minimum, validate_items
from app.services.form_semantics import original_unchecked


def validate_length(field, text):
    unit = field.get("length_unit") or "characters"
    value = re.sub(r"\s", "", text) if field.get("includes_spaces") is False else text
    warnings, errors = [], []
    if (field.get("input_spec") or {}).get("original_layout_review_required"):
        warnings.append("original_paragraph_layout_check_required")
    if ((field.get("input_spec") or {}).get("repeat") or {}).get("original_layout_review_required"):
        warnings.append("original_repeating_layout_check_required")
    if unit == "pages":
        count = None
        warnings.append("original_layout_page_check_required")
    elif unit == "bytes":
        evidence = (field.get("instructions") or "") + field.get("evidence", "")
        encoding = "utf-8" if re.search("utf[- ]?8", evidence, re.I) else "cp949" if re.search("euc[- ]?kr|cp949", evidence, re.I) else None
        if encoding:
            try:
                count = len(value.encode(encoding))
            except UnicodeEncodeError:
                count = None
                errors.append("unsupported_characters_for_encoding")
        else:
            count = len(value.encode("utf-8"))
            warnings.append("byte_encoding_check_required")
    else:
        count = len(value.split()) if unit == "words" else len(value)
    if count is not None and "byte_encoding_check_required" not in warnings:
        minimum = field.get("min_length")
        maximum = field.get("max_length")
        minimum_count = len(re.sub(r"\s", "", value)) if unit == "characters" and field.get("includes_spaces") is None else count
        if minimum is not None and minimum_count < minimum:
            errors.append("below_min_length")
        if maximum is not None and count > maximum:
            errors.append("above_max_length")
    if not text.strip() and field.get("required") is not False and field["field_type"] != "signature":
        errors.append("required_content_missing")
    if re.search(r"\[보완 필요\]|TODO|미확인", text):
        errors.append("unresolved_placeholder")
    if field["field_type"] == "checkbox" and not field.get("choice_spec") and field.get("options") and text.strip() and compact(text) not in {compact(o) for o in field["options"]} and not original_unchecked(field, text):
        errors.append("option_not_in_original")
    if any(r.get("kind") == "prompted_detail" for r in field.get("content_requirements", [])):
        warnings.append("original_content_review_required")
    errors.extend(validate_items(field, text))
    return {"count": count, "unit": unit, "errors": errors, "warnings": warnings}


def compose(field, answer, scholarship):
    if answer.get("status") == "skipped":
        return {"field_id": field["id"], "label": field_label(field), "text": "", "sources": [], "skipped": True,
                "validation": {"count": 0, "unit": field.get("length_unit") or "characters", "errors": [], "warnings": []}}
    facts = answer["facts"]
    order = [r["key"] for r in requirements(field, answer)] + [k for k in facts if k.startswith("extra_")]
    if field.get("input_spec"):
        from app.services.form_profile_inputs import render_input
        text = render_input(field, answer)
        return {"field_id": field["id"], "label": field_label(field), "text": text,
                "method": "confirmed_original_components", "sources": [{"slot_key": k, **facts[k].get("source", {"kind": "conversation"})} for k in order],
                "validation": validate_length(field, text), "reviewed": False}
    if field.get("choice_spec"):
        from app.services.form_choice_values import render_choice
        text = render_choice(field, answer)
        return {"field_id": field["id"], "label": field_label(field), "text": text,
                "method": "confirmed_original_choices_and_values", "sources": [{"slot_key": k, **facts[k].get("source", {"kind": "conversation"})} for k in order],
                "validation": validate_length(field, text), "reviewed": False}
    method = "confirmed_evidence_assembly"
    if llm.has_llm() and field["field_type"] == "essay":
        from app.services.essay_generation import generate
        generated = generate(field, {key: facts[key] for key in order}, scholarship)
        if generated:
            return {"field_id": field['id'], "label": field_label(field), **generated,
                    "sources": [{"slot_key": key, **facts[key].get('source', {'kind': 'conversation'})} for key in order],
                    "validation": validate_length(field, generated['text']), "reviewed": False}
    texts = list(dict.fromkeys(facts[k]["text"] for k in order))
    text = "\n\n".join(f"{i}. {value}" for i, value in enumerate(texts, 1)) if item_minimum(field) else "\n\n".join(texts)
    return {"field_id": field["id"], "label": field_label(field), "text": text, "method": method,
            "sources": [{"slot_key": k, **facts[k].get("source", {"kind": "conversation"})} for k in order],
            "validation": validate_length(field, text), "reviewed": False}


def draft_digest(drafts):
    return hashlib.sha256(json.dumps([{k: d.get(k) for k in ("field_id", "text", "skipped")} for d in drafts], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_groups(groups, fields, drafts, workflow):
    from app.services.form_semantics import active_fields
    from app.services.form_structure import group_conflicts
    active = {f["id"] for f in active_fields(fields, workflow)}
    by_id = {d["field_id"]: d for d in drafts}
    validations = []
    for group in groups:
        members = [i for i in group["field_ids"] if i in active]
        errors, warnings, count, unit = [], [], None, group.get("constraints", {}).get("length_unit") or "characters"
        if group_conflicts([group], fields):
            errors.append("shared_group_requires_review")
        if members and drafts:
            if any(i not in by_id for i in members):
                errors.append("shared_group_drafts_missing")
            text = "\n\n".join(by_id[i]["text"] for i in members if i in by_id)
            result = validate_length({**group.get("constraints", {}), "field_type": "essay", "required": False, "evidence": group["evidence"]}, text)
            errors.extend(result["errors"])
            warnings.extend(result["warnings"])
            count = result["count"]
            if group.get("format_rules"):
                warnings.append("original_group_format_check_required")
        validations.append({"group_id": group["id"], "label": group["label"], "field_ids": members,
                            "count": count, "unit": unit, "errors": errors, "warnings": warnings})
    return validations
