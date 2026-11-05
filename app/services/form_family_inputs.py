"""Bounded source grammars for family marks, siblings and original income units."""
import hashlib
import re

from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-family-inputs-v1"
LABELS = {"동거가족", "기타동거가족", "가족월평균수입", "수입출처", "형제자매()남()녀중()째"}


def native_family_inputs(blocks, source):
    groups = []
    for label in blocks:
        if label.get("method") != "native" or not label.get("table"):
            continue
        name = compact(label["text"])
        if name not in LABELS:
            continue
        row = [b for b in blocks if b.get("table") == label["table"] and b.get("row") == label.get("row")]
        if any(b.get("method") != "native" for b in row):
            continue
        peers = [b for b in row if b.get("cell_location") == label.get("cell_location")]
        adjacent = [b for b in row if b.get("column") == label["column"] + label.get("column_span", 1)]
        if name == "동거가족":
            if [compact(b["text"]) for b in adjacent] != ["조부(),조모(),부(),모()", "※해당하는가족구성원에‘O’표시"]:
                continue
            field = _field(label, source, adjacent[0]["location"])
            field.update(field_type="checkbox", options=["조부", "조모", "부", "모"], selection_mode="marks",
                         option_sources=[block_source(adjacent[0], source)],
                         choice_spec={"version": "native-choice-values-v1", "multiple_explicit": False, "multiple_sources": [],
                                      "allow_empty": True, "mark": "O", "mark_source": block_source(adjacent[1], source),
                                      "choices": [{"caption": caption, "values": [], "source": block_source(adjacent[0], source)}
                                                  for caption in ("조부", "조모", "부", "모")]})
            used = [label, *adjacent]
        else:
            if name == "형제자매()남()녀중()째":
                if len(peers) != 1:
                    continue
                destination, used = label, [label]
                specs = [("male_count", "남", "nonnegative_integer", ""), ("female_count", "녀", "nonnegative_integer", ""),
                         ("birth_order", "째", "positive_integer", "")]
            elif name == "기타동거가족":
                if len(adjacent) != 1 or compact(adjacent[0]["text"]) != "※있을시작성":
                    continue
                destination, used = adjacent[0], [label, *adjacent]
                specs = [("other_family", "기타 동거가족", "text", "")]
            elif name == "가족월평균수입":
                if [compact(b["text"]) for b in peers] != ["가족월평균수입", "(본인포함동거가족)"] or len(adjacent) != 1 or compact(adjacent[0]["text"]) != "(세후)만원":
                    continue
                destination, used = adjacent[0], [*peers, *adjacent]
                specs = [("monthly_net_income", "가족월평균수입", "amount", "만원")]
            else:
                if len(adjacent) != 1 or compact(adjacent[0]["text"]) != "정부지원원/근로수입원/후원금원":
                    continue
                destination, used = adjacent[0], [label, *adjacent]
                specs = [("government_income", "정부지원", "amount", "원"), ("work_income", "근로수입", "amount", "원"),
                         ("donation_income", "후원금", "amount", "원")]
            field = _field(label, source, destination["location"])
            field["input_spec"] = {"version": VERSION, "slots": [
                {"key": key, "label": title, "kind": kind, "unit": unit,
                 "source": block_source(destination, source), "destination": destination["location"]}
                for key, title, kind, unit in specs]}
            if name == "기타동거가족":
                proof = block_source(destination, source)
                field["applicability"] = {"id": hashlib.sha256((destination["location"] + compact(destination["text"])).encode()).hexdigest()[:24],
                                          "mode": "user_confirmation", "evidence": destination["text"], "source": proof,
                                          "discard_facts_on_change": True}
        field["instruction_sources"] = [block_source(b, source) for b in used]
        field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
        groups.append({"fields": [field], "locations": {b["location"] for b in used}})
    return groups


def audit_family_inputs(blocks, fields, source):
    recovered = native_family_inputs(blocks, source)
    checks = []
    for label in blocks:
        if label.get("method") != "native" or not label.get("table") or compact(label["text"]) not in LABELS:
            continue
        matches = [g for g in recovered if label["location"] in g["locations"]]
        target = matches[0]["fields"][0] if len(matches) == 1 else None
        actual = [f for f in fields if target and f["id"] == target["id"]]
        valid = len(actual) == 1 and all(actual[0].get(k) == target.get(k) for k in (
            "source", "input_spec", "choice_spec", "applicability", "selection_mode"))
        checks.append({"source": block_source(label, source), "status": "bound" if valid else "requires_original_review",
                       "issues": [] if valid else ["family_inputs_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
            "scope": "five_native_family_groups_not_whole_form"}
