"""Separate, source-reviewed physical-field gold. Never used as model input."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from app.services.document_layout import compact
from reports.source_proof import document_texts


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, data):
    path = Path(path)
    if path.exists():
        raise ValueError("output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# Native paragraph indices reviewed against the actual captured form contents.
# Indices select source paragraphs, never previous model predictions/matches.
MOONJU = [("student.name", 47), ("student.birth", 48), ("student.photo", 49),
          ("student.address", 51), ("student.phone", 52), ("student.email", 54),
          ("student.school", 55), ("student.department_grade", 56), ("student.tuition", 57),
          ("student.gpa", 58), ("student.bank", 60), ("guardian.name", 63),
          ("guardian.relation", 64), ("guardian.job", 65), ("guardian.address", 66),
          ("guardian.phone", 67), ("application.signature", 74), ("consent.signature", 105)]
NONSAN = [("type", 2), ("student.name", 6), ("student.birth", 7), ("student.gender", 8),
          ("student.address", 10), ("student.phone", 11), ("student.school", 13),
          ("student.school_region", 14), ("student.department", 16), ("student.number_grade", 17),
          ("applicant.name", 23), ("applicant.birth", 24), ("applicant.relation", 25),
          ("applicant.address", 27), ("applicant.phone", 28), ("bank", 30),
          ("application.signature", 35), ("collection.consent", 49), ("collection.identifier", 53),
          ("collection.refusal", 57), ("provision.consent", 68), ("provision.identifier", 72),
          ("provision.refusal", 76), ("student.consent_signature", 81), ("student.consent_birth", 82),
          ("applicant.consent_signature", 84), ("applicant.consent_birth", 85)]


def build_gold(capture_root, original_gold):
    capture_root, original = Path(capture_root), read(original_gold)
    forms = []
    for identity, prefix, annotations in ((3, "589bd83c52b2", MOONJU), (881, "8168b62f52b6", NONSAN)):
        capture = read(capture_root / f"notice-{identity}" / "capture.json")
        document = next(d for d in capture["documents"] if d.get("sha256", "").startswith(prefix))
        proof = capture_root / f"notice-{identity}" / document["local_file"]
        if hashlib.sha256(proof.read_bytes()).hexdigest() != document["sha256"]:
            raise ValueError("source checksum mismatch")
        source_form = next(f for f in original["forms"] if f["id"] == identity)
        known = {f["key"]: f for f in source_form["fields"]}
        blocks, paragraphs = document["blocks"], Counter()
        ordinal = []
        for block in blocks:
            cell = block.get("cell_location") or block["location"]
            ordinal.append(paragraphs[cell]); paragraphs[cell] += 1
        fields = []
        for key, index in annotations:
            block = blocks[index]
            kind, role = "text", key.split(".")[0]
            role = role if role in ("student", "guardian", "applicant") else "unknown"
            if "signature" in key:
                kind = "signature"
                role = "student" if key.startswith("student.") else "applicant"
            elif key == "student.photo":
                kind = "image"
            elif identity == 881 and (key in ("type", "student.gender", "applicant.relation") or key.startswith(("collection.", "provision."))):
                kind = "checkbox"
            options = (["학업장려 장학금"] if key == "type" else ["남", "여"] if key == "student.gender"
                       else ["부", "모"] if key == "applicant.relation" and identity == 881
                       else ["동의함", "동의하지 않음"] if key.startswith(("collection.", "provision.")) else [])
            section = "collection_use" if key.startswith("collection.") else "provision_inquiry" if key.startswith("provision.") else None
            conditional = identity == 881 and key in ("applicant.consent_signature", "applicant.consent_birth")
            guidance_indices = ([50, 69] if identity == 3 and key == "student.photo" else
                                [70] if identity == 3 and key == "student.tuition" else
                                [71] if identity == 3 and key == "student.bank" else
                                [32] if identity == 881 and key == "bank" else [])
            expected_key = "student.department_grade" if identity == 3 and key == "student.department_grade" else key
            if expected_key not in known:
                raise ValueError("annotation key missing in original gold: " + expected_key)
            item = {"key": key, "labels": known[expected_key]["labels"], "role": role, "field_type": kind,
                    "options": options, "semantic_kind": "consent" if section else None, "section": section,
                    "condition": blocks[83]["text"] if conditional else None,
                    "manual": kind in ("signature", "image", "attachment") or bool(section),
                    "required": None, "min_length": None, "max_length": None, "length_unit": None, "includes_spaces": None,
                    "guidance": [blocks[i]["text"] for i in guidance_indices],
                    "reference": {"sha256": document["sha256"], "location": block["location"], "text": block["text"]},
                    "physical": {**{k: block.get(k) for k in ("table_fingerprint", "table_occurrence", "row", "column")},
                                 "cell_paragraph": ordinal[index]}}
            fields.append(item)
        forms.append({"id": identity, "source_sha_prefixes": source_form["source_sha_prefixes"], "proof_path": str(proof),
                      "proof_sha256": document["sha256"], "fields": fields})
    return {"reviewer": "Codex native source paragraph review; independent human second review pending",
            "scope": "45 existing reviewed inputs in two forms, now linked to physical paragraphs, types, roles, options and two local conditional inputs",
            "limits": ["No positive required/length constraints in these two reviewed forms", "Five targeted guidance clauses, not exhaustive semantic guidance review",
                       "No additional photo uploads or dynamic forms sampled", "No independent human review or service-wide accuracy claim"],
            "original_gold_sha256": hashlib.sha256(Path(original_gold).read_bytes()).hexdigest(), "forms": forms}


def metric(correct, total):
    return {"correct": correct, "total": total, "rate": correct / total if total else None}


def evaluate(run_root, gold):
    root = Path(run_root)
    run = read(root / "run.json")
    if run["state"] != "complete":
        raise ValueError("incomplete run")
    forms, checks = [], Counter()
    for form in gold["forms"]:
        if hashlib.sha256(Path(form["proof_path"]).read_bytes()).hexdigest() != form["proof_sha256"]:
            raise ValueError("reviewed source checksum mismatch")
        prediction = read(root / f"notice-{form['id']}" / "prediction.json")
        available = [f for f in prediction["eligibility"]["application_fields"]
                     if any(f.get("source", {}).get("sha256", "").startswith(s) for s in form["source_sha_prefixes"])]
        documents = document_texts(root / f"notice-{form['id']}", prediction)
        results = []
        for expected in form["fields"]:
            def physically_matches(field):
                physical = expected["physical"]
                if physical.get("table_fingerprint"):
                    return all(field.get("layout", {}).get(k) == v for k, v in physical.items())
                return (field.get("source", {}).get("sha256") == expected["reference"]["sha256"]
                        and field.get("source", {}).get("location") == expected["reference"]["location"])
            candidates = [f for f in available if physically_matches(f)]
            actual = candidates[0] if len(candidates) == 1 else None
            if actual:
                available.remove(actual)
            detail = {"physical": bool(actual), "label": bool(actual) and compact(actual["label"]) in {compact(s) for s in expected["labels"]}}
            for name in ("role", "field_type", "required", "min_length", "max_length", "length_unit", "includes_spaces", "semantic_kind"):
                detail[name] = bool(actual) and actual.get(name) == expected[name]
            detail["options"] = bool(actual) and sorted(compact(s) for s in actual.get("options", [])) == sorted(compact(s) for s in expected["options"])
            detail["section"] = bool(actual) and (actual.get("section") or {}).get("kind") == expected["section"]
            detail["condition"] = bool(actual) and compact((actual.get("applicability") or {}).get("evidence", "")) == compact(expected["condition"] or "")
            detail["manual"] = bool(actual) and (actual["field_type"] in ("signature", "image", "attachment") or actual.get("semantic_kind") == "consent") == expected["manual"]
            detail["guidance"] = bool(actual) and all(compact(s) in compact(actual.get("instructions", "")) for s in expected["guidance"])
            proofs = []
            if actual:
                proofs = [(actual.get("source", {}), actual.get("evidence", ""))]
                proofs.extend((s, s.get("evidence", "")) for name in ("instruction_sources", "option_sources") for s in actual.get(name, []))
                for name in ("type_source",):
                    if actual.get(name):
                        proofs.append((actual[name], actual[name].get("evidence", "")))
                for name in ("section", "applicability"):
                    if actual.get(name):
                        source = actual[name].get("source", {})
                        proofs.append((source, source.get("evidence", "")))
            def grounded(source, evidence):
                text = documents.get(source.get("sha256"), "")
                lower, upper = source.get("text_start"), source.get("text_end")
                return bool(evidence) and isinstance(lower, int) and isinstance(upper, int) and 0 <= lower <= upper <= len(text) and text[lower:upper] == evidence
            detail["source_grounding"] = bool(actual) and all(grounded(s, e) for s, e in proofs)
            for name, passed in detail.items():
                checks[name + ":total"] += 1
                checks[name + ":correct"] += passed
            results.append({"key": expected["key"], "actual_id": actual.get("id") if actual else None,
                            "checks": detail, "correct": all(detail.values())})
        forms.append({"id": form["id"], "fields": results, "extras": [{"id": f["id"], "label": f["label"]} for f in available],
                      "all_reviewed_attributes_match": all(f["correct"] for f in results) and not available})
    return {"reviewer": gold["reviewer"], "limits": gold["limits"], "gold_sha256": hashlib.sha256(json.dumps(gold, sort_keys=True).encode()).hexdigest(),
            "fields": metric(sum(f["correct"] for form in forms for f in form["fields"]), sum(len(form["fields"]) for form in forms)),
            "attributes": {name.split(":")[0]: metric(checks[name.replace(":total", ":correct")], count) for name, count in checks.items() if name.endswith(":total")},
            "whole_reviewed_forms": metric(sum(f["all_reviewed_attributes_match"] for f in forms), len(forms)), "forms": forms}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-from")
    parser.add_argument("--original-gold", default="reports/scholarship-evaluation-20261005/gold.json")
    parser.add_argument("--gold")
    parser.add_argument("--run")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    data = build_gold(args.build_from, args.original_gold) if args.build_from else evaluate(args.run, read(args.gold))
    write_new(args.output, data)
    print(json.dumps({k: data[k] for k in ("fields", "whole_reviewed_forms") if k in data}))


if __name__ == "__main__":
    main()
