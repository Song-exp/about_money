"""명시적인 원문 구역으로 공유 규칙과 설명 셀의 범위를 제한한다."""
import hashlib
import json
import re

from app.services.document_layout import compact
from app.services.form_constraints import parse_constraints


def proof_source(block, source):
    return {**(source or {}), "location": block["location"],
            "text_start": block["start"], "text_end": block["end"], "evidence": block["text"]}


def group_scopes(blocks):
    """Bracketed essay section, explicit shared rule, numbered native cell prompts.

    Other layouts are left to review. A privacy/new form heading ends the section;
    OCR, unnumbered prompts, and broken numbering cannot establish complete scope.
    """
    scopes = []
    for index, heading in enumerate(blocks):
        if heading.get("method") != "native" or compact(heading["text"]) not in ("[자기소개서]", "【자기소개서】", "[자소서]"):
            continue
        section = []
        for block in blocks[index + 1:]:
            text = compact(block["text"])
            if (re.fullmatch(r"[\[【].+[\]】]", text) or
                    (len(text) < 80 and text.endswith(("동의서", "신청서", "지원서", "추천서")))):
                break
            section.append(block)
        rules = [b for b in section if b.get("method") == "native" and
                 re.search(r"총\d+(?:글자|자|bytes?|바이트|쪽|페이지|매|장|단어)(?:이내|이하|이상)", compact(b["text"]), re.I) and
                 any(t in compact(b["text"]) for t in ("아래질문", "각문항", "전체문항"))]
        if not rules:
            continue
        prompts = [b for b in section if b.get("method") == "native" and b.get("kind") == "cell" and
                   b["start"] > rules[0]["end"] and re.match(r"^\s*\d+[.)]\s*\S", b["text"])]
        numbers = [int(re.match(r"^\s*(\d+)", b["text"])[1]) for b in prompts]
        issues = []
        if len(rules) != 1 or len(prompts) < 2 or numbers != list(range(1, len(prompts) + 1)):
            issues.append("shared_scope_requires_review")
        if any(b.get("method") != "native" for b in section):
            issues.append("shared_scope_requires_review")
        scopes.append({"heading": heading, "rules": rules, "prompts": prompts, "section": section,
                       "issues": list(dict.fromkeys(issues))})
    return scopes


def scope_for_block(block, scopes):
    return next((s for s in scopes if any(p["location"] == block.get("location") for p in s["prompts"])), None)


def native_essay_prompts(scopes):
    prompts = []
    for scope in scopes:
        if scope["issues"] or any(re.search(r"[□☐☑■]|서명|사진|첨부", p["text"]) or
                not re.search(r"소개|동기|경험|계획|비전|사용|도움|돌봄|활동|학업|진로|목표|방안", p["text"])
                for p in scope["prompts"]):
            continue
        for block in scope["prompts"]:
            prompts.append({"label": block["text"], "field_type": "essay", "label_block_id": block["block_id"],
                            "instruction_block_ids": [scope["rules"][0]["block_id"]], "instructions": scope["rules"][0]["text"],
                            "_location": block["location"]})
    return prompts


def reconcile_essay_prompt(item, prompts):
    if not isinstance(item, dict):
        return item
    matches = []
    for prompt in prompts:
        if item.get("label_block_id"):
            match = item["label_block_id"] == prompt["label_block_id"]
        elif item.get("location"):
            match = item["location"] == prompt["_location"]
        else:
            full = compact(prompt["label"])
            match = compact(item.get("label", "")) in (full, re.sub(r"^\d+[.)]", "", full))
        if match:
            matches.append(prompt)
    return {k: v for k, v in matches[0].items() if not k.startswith("_")} if len(matches) == 1 else item


def descriptive_cells(blocks, source=None):
    """Exclude only native, positively identified explanatory cells. Keep choices.

    Checklist check columns remain possible inputs; document names and issue-date
    instructions are never interpreted as applicant text fields.
    """
    tables, excluded = {}, {}
    for block in blocks:
        if block.get("method") == "native" and block.get("table"):
            tables.setdefault(block["table"], []).append(block)
    def exclude(block, reason):
        excluded[block["location"]] = {"reason": reason, "source": proof_source(block, source)}
    for cells in tables.values():
        headers = [b for b in cells if b.get("row") == 0]
        names = {compact(b["text"]) for b in headers}
        if {"구분", "성명", "동의여부", "서명또는인"}.issubset(names):
            for header in headers:
                exclude(header, "repeated_table_header_requires_review")
        checklist = "서류명" in names and any(n.startswith(("발급일기준", "발급처")) for n in names) and any(n.startswith("비고") for n in names)
        if checklist:
            check_cols = {b.get("column") for b in headers if compact(b["text"]).startswith(("첨부여부", "제출여부", "확인"))}
            for block in cells:
                if block.get("column") not in check_cols and not re.search(r"[□☐☑]|동의함|서명\s*[:：]", block["text"]):
                    exclude(block, "document_checklist_description")
        privacy = any(("개인정보" in compact(b["text"]) or "주민등록번호처리" in compact(b["text"])) and
                      any(t in compact(b["text"]) for t in ("수집", "처리")) for b in headers)
        if privacy:
            labels = {"수집이용목적", "수집이용항목", "보유이용기간", "목적항목", "근거"}
            for block in cells:
                text = re.sub(r"[·ㆍ・]", "", compact(block["text"]))
                if text not in labels:
                    continue
                peers = [b for b in cells if b.get("row") == block.get("row") and b.get("column") != block.get("column")]
                if peers and all(len(compact(b["text"])) > 12 and not re.search(r"[□☐☑]|동의함|서명", b["text"]) for b in peers):
                    for b in [block, *peers]:
                        exclude(b, "privacy_processing_description")
        for block in cells:
            # This is an original consent prompt, but native text exposes no control.
            # Keep it as a review task rather than inventing an essay or choices.
            if (privacy and "동의를거부할수있으나" in compact(block["text"]) and
                    "동의하십니까" in compact(block["text"]) and not re.search(r"[□☐☑]|_{3,}", block["text"])):
                exclude(block, "consent_control_requires_original_review")
            if re.match(r"^\s*▣\s*(?:개인정보|돌봄대상자\s*정보)\s*[:：]", block["text"]) and not re.search(r"[□☐☑]", block["text"]):
                exclude(block, "privacy_data_inventory")
    return excluded


def attach_groups(fields, scopes, source=None):
    groups = []
    for scope in scopes:
        rule = scope["rules"][0]
        constraints, issues = parse_constraints(rule["text"])
        issues = [*scope["issues"], *issues]
        members = []
        for prompt in scope["prompts"]:
            matches = [f for f in fields if prompt["start"] <= f.get("source", {}).get("label_start", -1) < prompt["end"]]
            if len(matches) != 1 or matches[0]["field_type"] != "essay":
                issues.append("shared_group_members_missing_or_ambiguous")
            else:
                members.append(matches[0])
        rules_text = "\n".join(b["text"] for b in scope["rules"])
        format_rules = []
        for kind, pattern in (("font_family", r"맑은\s*고딕"), ("font_size", r"(?:글씨|글자|글꼴)\s*크기\s*\d+(?:\.\d+)?\s*pt"),
                              ("line_spacing", r"줄\s*간격\s*\d+(?:\.\d+)?\s*%?")):
            for match in re.finditer(pattern, rule["text"], re.I):
                format_rules.append({"kind": kind, "evidence": match[0], "source": {
                    **proof_source(rule, source), "text_start": rule["start"] + match.start(), "text_end": rule["start"] + match.end(), "evidence": match[0]}})
        identity = {"heading": compact(scope["heading"]["text"]), "location": scope["heading"]["location"], "source": source}
        group = {"id": hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24],
                 "label": scope["heading"]["text"], "field_ids": [f["id"] for f in members],
                 "expected_members": [proof_source(p, source) for p in scope["prompts"]],
                 "evidence": rules_text, "source": proof_source(rule, source),
                 "constraints": constraints, "format_rules": format_rules,
                 "issues": list(dict.fromkeys(issues)), "status": "needs_review" if issues else "source_bound"}
        last = max((p["end"] for p in scope["prompts"]), default=rule["end"])
        policies = [b for b in scope["section"] if b["start"] < last and
                    all(t in compact(b["text"]) for t in ("이전신청", "그대로붙여넣", "탈락"))]
        if policies:
            group["history_reuse_policy"] = {"mode": "direct_copy_prohibited", "sources": [proof_source(b, source) for b in policies]}
        for field in members:
            field.setdefault("group_ids", []).append(group["id"])
            if policies:
                field["history_reuse_policy"] = group["history_reuse_policy"]
            # Resolve this one issue only once all original numbered members are bound.
            if not issues:
                field["constraint_issues"] = [i for i in field.get("constraint_issues", []) if i != "shared_length_scope_requires_review"]
                if not field["constraint_issues"] and field.get("validation_status") == "constraints_require_review":
                    field.pop("validation_status", None)
        groups.append(group)
    return groups


def group_conflicts(groups, fields):
    ids = {f["id"] for f in fields if f.get("id")}
    return any(g.get("issues") or g.get("status") != "source_bound" or not g.get("source", {}).get("sha256") or
               not g.get("field_ids") or len(g["field_ids"]) != len(set(g["field_ids"])) or
               not set(g["field_ids"]).issubset(ids) or len(g["field_ids"]) != len(g.get("expected_members", [])) for g in groups)
