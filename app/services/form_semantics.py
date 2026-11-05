"""native 양식의 타입·동의 구역·국소 적용 조건을 원문 위치에 연결한다."""
import hashlib
import json
import re

from app.services.document_layout import compact, explicit_role
from app.services.form_consent import original_options, consent_context

MANUAL_TYPES = {"signature", "image", "attachment"}
_PHOTO_LABELS = {"사진", "증명사진", "반명함사진", "사진첨부"}
_ATTACHMENT_LABELS = {"첨부파일", "파일첨부", "파일업로드", "증빙파일첨부"}
_CONDITION = re.compile(r"경우.*(?:추가|만|한하여).*(?:서명|작성|제출|첨부)|해당자(?:만|에한하여).*(?:작성|제출|서명|첨부)")


def block_source(block, source):
    return {**(source or {}), "location": block["location"], "text_start": block["start"],
            "text_end": block["end"], "evidence": block["text"]}


def local_condition(block, blocks):
    if block.get("method") != "native" or not block.get("cell_location"):
        return None
    index = blocks.index(block)
    for previous in reversed(blocks[max(0, index - 3):index]):
        if previous.get("cell_location") != block["cell_location"] or previous.get("method") != "native":
            break
        prior_role = explicit_role(previous["text"])
        if prior_role and block.get("role", "unknown") not in (prior_role, "unknown"):
            break
        if _CONDITION.search(compact(previous["text"])):
            return previous
    return None


def enrich_semantics(field, block, blocks, source):
    if block.get("method") != "native":
        return
    label = compact(field["label"]).strip("()[]〈〉<>:：")
    if explicit_role(block["text"]) and re.search(r"\([^)]*(?:서명|날인)[^)]*\)", block["text"]):
        field["field_type"] = "signature"
        field["type_source"] = block_source(block, source)
    if block.get("table") and label in _PHOTO_LABELS | _ATTACHMENT_LABELS:
        field["field_type"] = "image" if label in _PHOTO_LABELS else "attachment"
        field["type_source"] = block_source(block, source)
        # The local photo caption often occupies a separate paragraph of this cell.
        local = [b for b in blocks if b.get("cell_location") and b.get("cell_location") == block.get("cell_location")
                 and b.get("method") == "native"
                 and b["start"] >= block["end"] and len(b["text"]) <= 250
                 and re.search(r"사진|첨부|업로드|불가", b["text"])]
        proofs = list(field.get("instruction_sources", []))
        if not proofs and field.get("instruction_source"):
            proof = dict(field["instruction_source"])
            proof.setdefault("evidence", field.get("instruction_evidence", ""))
            proofs.append(proof)
        for item in local:
            proof = block_source(item, source)
            if not any(s.get("text_start") == proof["text_start"] for s in proofs):
                proofs.append(proof)
        if proofs:
            proofs.sort(key=lambda s: s["text_start"])
            field.update(instruction_sources=proofs, instruction_source=proofs[0],
                         instructions="\n".join(s["evidence"] for s in proofs),
                         instruction_evidence="\n".join(s["evidence"] for s in proofs))
    if (field["field_type"] == "checkbox" or "동의" in compact(field["label"])) and block.get("table") and isinstance(block.get("column"), int):
        choices = [b for b in blocks if b.get("table") == block["table"] and b.get("row") == block.get("row")
                   and b.get("method") == "native"
                   and b.get("column") == block["column"] + (block.get("column_span") or 1)
                   and re.search(r"[☐□☑■]", b["text"])]
        options, proofs = [], []
        for choice in choices:
            values = original_options(choice["text"])
            if values:
                options.extend(values)
                proofs.append(block_source(choice, source))
        if options:
            field.update(field_type="checkbox", options=options,
                         option_sources=proofs, type_source=proofs[0])
    if field["field_type"] == "checkbox" and ("동의" in compact(field["label"]) or
            field.get("options") and all("동의" in compact(o) for o in field["options"])):
        field["semantic_kind"] = "consent"
        context = consent_context(block, blocks, source)
        if context:
            field["section"] = {k: context[k] for k in ("kind", "label", "source")}
            field["consent_context"] = {"text": context["text"], "sources": context["sources"]}
            if re.search(r"[（(]\s*필수\s*[）)]", context["label"]):
                field["required"] = True
                field["required_source"] = context["source"]
        if re.search(r"[（(]\s*해당\s*시\s*[）)]", block["text"]):
            identity = hashlib.sha256((block["location"] + compact(block["text"])).encode()).hexdigest()[:24]
            field["applicability"] = {"id": identity, "mode": "user_confirmation", "evidence": block["text"], "source": block_source(block, source)}
    condition = local_condition(block, blocks)
    if condition:
        evidence = condition["text"]
        scope = {k: condition.get(k) for k in ("table_fingerprint", "table_occurrence", "row", "column", "cell_paragraph")}
        identity = hashlib.sha256(json.dumps({"evidence": compact(evidence), "scope": scope,
                                             "role": block.get("role", "unknown")}, sort_keys=True).encode()).hexdigest()[:24]
        field["applicability"] = {"id": identity, "mode": "user_confirmation", "evidence": evidence,
                                  "source": block_source(condition, source)}
    headers = {compact(b["text"]) for b in blocks if b.get("table") == block.get("table") and b.get("row") == 0}
    if {"구분", "성명", "동의여부", "서명또는인"}.issubset(headers) and field["field_type"] == "signature" and not re.search(r"서명|날인|\(인\)", block["text"]):
        field.setdefault("constraint_issues", []).append("template_signature_requires_review")


def is_manual(field):
    return field["field_type"] in MANUAL_TYPES or field.get("semantic_kind") in ("consent", "submission_checklist") or (field.get("manual_spec") or {}).get("mode") == "original_only"


def original_unchecked(field, text):
    return (field.get("semantic_kind") == "consent" and field.get("selection_mode") == "single_mark" and
            field.get("options") == ["동의함"] and compact(text) in ("표시하지않음", "선택하지않음", "동의하지않음"))


def applicability_state(field, workflow):
    condition = field.get("applicability")
    if not condition:
        return "applicable"
    answer = (workflow.get("applicability_answers") or {}).get(condition["id"], {})
    if answer.get("confirmed") is not True or type(answer.get("applies")) is not bool:
        return "unknown"
    return "applicable" if answer["applies"] else "not_applicable"


def active_fields(fields, workflow):
    return [f for f in fields if applicability_state(f, workflow) == "applicable"]
