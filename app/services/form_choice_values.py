"""Source-bound choice captions with inline values, and their answer slots."""
import hashlib
import json
import re

from app.services.document_layout import compact
from app.services.form_consent import original_options
from app.services.form_semantics import block_source

VERSION = "native-choice-values-v1"
_BLANK = re.compile(r"\((?P<before>[^()]*)\s{2,}(?P<after>[^()]*)\)")


def native_value_controls(blocks, source):
    cells = {}
    for block in blocks:
        if block.get("cell_location"):
            cells.setdefault(block["cell_location"], []).append(block)
    result = []
    for peers in cells.values():
        if any(b.get("method") != "native" for b in peers):
            continue
        options = [o for b in peers for o in original_options(b["text"])]
        stems = [compact(o.split("(", 1)[0]) for o in options]
        housing = stems == ["자가", "전세", "월세", "기타"]
        care = stems == ["장기요양인정자", "중증장애인", "정신건강관련질환으로치료중"]
        if not housing and not care:
            continue
        row = [b for b in blocks if b.get("method") == "native" and b.get("table") == peers[0].get("table")
               and b.get("row") == peers[0].get("row")]
        headers = [b for b in row if compact(b["text"]) == ("주거형태" if housing else "해당사항")
                   and b.get("column", -1) + b.get("column_span", 1) == peers[0].get("column")]
        if len(headers) != 1:
            continue
        header = headers[0]
        multiple = [b for b in row if b.get("cell_location") == header.get("cell_location")
                    and compact(b["text"]) in ("※중복체크가능", "※복수선택가능")]
        if care and len(multiple) != 1:
            continue
        choices, valid = [], True
        for index, caption in enumerate(options):
            owner = next((b for b in peers if caption in b["text"]), None)
            if owner is None:
                valid = False
                break
            proof = block_source(owner, source)
            values = []
            match = _BLANK.search(caption)
            if match:
                # These are the only supported blank grammars; no guessed limits.
                inside = compact(match.group()[1:-1])
                kind = "amount" if inside in ("만원", "만원/월") else "grade" if inside == "등급:" else "text" if not inside else None
                if kind is None:
                    valid = False
                    break
                values.append({"key": f"choice_{index}_value", "kind": kind,
                               "label": caption.split("(", 1)[0].strip(),
                               "unit": inside if kind == "amount" else "", "source": proof})
            choices.append({"caption": caption, "values": values, "source": proof})
        if not valid or not any(c["values"] for c in choices):
            continue
        proof = block_source(header, source)
        identity = {"source": source, "choice_cell": peers[0]["cell_location"], "label": header["text"]}
        field = {"id": hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24],
                 "label": header["text"], "field_type": "checkbox", "required": None,
                 "min_length": None, "max_length": None, "length_unit": None, "includes_spaces": None,
                 "role": "unknown", "options": options, "evidence": header["text"],
                 "source": {**proof, "label_start": header["start"]},
                 "option_sources": [block_source(b, source) for b in peers],
                 "type_source": block_source(peers[0], source),
                 "layout": {k: header[k] for k in ("table", "table_fingerprint", "table_occurrence", "row", "column", "row_span", "column_span") if k in header},
                 "choice_spec": {"version": VERSION, "choices": choices,
                                 "multiple_explicit": bool(multiple), "multiple_sources": [block_source(b, source) for b in multiple]},
                 "instruction_sources": [proof, *[block_source(b, source) for b in multiple], *[block_source(b, source) for b in peers]]}
        # Housing's source does not specify exclusivity; don't invent a one-choice rule.
        field["selection_mode"] = "multiple" if multiple else "marks"
        field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
        result.append({"field": field, "locations": {b["location"] for b in [header, *peers]}})
    return result


def selections(field, text):
    """An exact caption or a JSON array of exact captions; no fuzzy inference."""
    if not field.get("choice_spec"):
        raise ValueError("not a structured choice")
    try:
        values = [] if field["choice_spec"].get("allow_empty") and compact(text) == "표시하지않음" else json.loads(text) if text.strip().startswith("[") else [text] if compact(text) in {compact(o) for o in field["options"]} else text.splitlines()
    except (ValueError, TypeError):
        raise ValueError("invalid choice list") from None
    options = field["options"]
    lookup = {compact(o): o for o in options}
    if not isinstance(values, list) or (not values and not field["choice_spec"].get("allow_empty")) or any(not isinstance(v, str) or compact(v) not in lookup for v in values):
        raise ValueError("select original captions")
    keys = [compact(v) for v in values]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate choice")
    if field.get("selection_mode") == "single" and len(keys) != 1:
        raise ValueError("choose one original state")
    return [o for o in options if compact(o) in keys]


def value_slots(field, answer):
    facts = (answer or {}).get("facts", {})
    text = facts.get("value", {}).get("text", "")
    if not text:
        return []
    try:
        selected = selections(field, text)
    except ValueError:
        return []
    if not selected:
        return field["choice_spec"].get("empty_values", [])
    return [v for c in field["choice_spec"]["choices"] if c["caption"] in selected for v in c["values"]]


def check_slot(field, key, text, answer):
    if key == "value":
        return selections(field, text)
    slot = next((v for v in value_slots(field, answer) if v["key"] == key), None)
    if slot is None:
        raise ValueError("inactive inline value")
    if slot["kind"] == "integer_upper_bound" and (not re.fullmatch(r"(?:0|[1-9]\d*)", text) or int(text) > slot["maximum"]):
        raise ValueError("enter a number within the original upper bound")
    if slot["kind"] == "evidence_availability" and compact(text) != "제출가능":
        raise ValueError("the source requires available supporting documents")
    if slot["kind"] == "amount" and not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", text):
        raise ValueError("amount must use the original unit")
    if slot["kind"] == "positive_integer" and not re.fullmatch(r"[1-9]\d*", text):
        raise ValueError("a reported experience needs a positive count")
    if slot["kind"] == "grade" and (not text.strip() or compact(text) in ("네", "예", "확인", "모름", "없음")):
        raise ValueError("enter the actual grade")
    if not text.strip() or compact(text) in ("네", "예", "확인", "모름"):
        raise ValueError("enter the actual value")
    return None


def render_choice(field, answer):
    facts = answer.get("facts", {})
    selected = selections(field, facts.get("value", {}).get("text", ""))
    if not selected and field["choice_spec"].get("allow_empty"):
        lines = ["표시하지 않음"]
        for slot in value_slots(field, answer):
            value = facts.get(slot["key"], {}).get("text", "")
            check_slot(field, slot["key"], value, answer)
            lines.append(f"{slot['label']}: {value}" + (f" {slot['unit']}" if slot.get("unit") else ""))
        return "\n".join(lines)
    rendered = []
    for choice in field["choice_spec"]["choices"]:
        if choice["caption"] not in selected:
            continue
        caption = choice["caption"]
        separate = []
        for slot in choice["values"]:
            value = facts.get(slot["key"], {}).get("text", "")
            check_slot(field, slot["key"], value, answer)
            if slot.get("placement") == "separate_cell":
                separate.append(f"{slot['label'].rstrip(':：')}: {value}")
                continue
            replacement = f"({value} {slot['unit']})" if slot["kind"] == "amount" else f"(등급: {value})" if slot["kind"] == "grade" else f"({value})"
            caption = _BLANK.sub(lambda _: replacement, caption, count=1)
        if field["choice_spec"].get("mark"):
            caption += f"({field['choice_spec']['mark']})"
        rendered.append("\n".join([caption, *separate]))
    return "\n".join(rendered)
