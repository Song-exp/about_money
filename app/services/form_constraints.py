"""입력란에 연결된 원문에서 명시적인 제약만 읽는다."""
import re

from app.services.document_layout import compact

UNITS = {"자": "characters", "글자": "characters", "byte": "bytes", "bytes": "bytes",
         "바이트": "bytes", "쪽": "pages", "페이지": "pages", "매": "pages", "장": "pages", "단어": "words"}
_UNIT = r"글자|자|bytes?|바이트|쪽|페이지|매|장|단어"
LIMIT_RE = re.compile(rf"(?<![\d.,])(\d[\d,]*)\s*({_UNIT})\s*(이내|이하|이상)", re.I)
_RANGE = re.compile(rf"(?<![\d.,])(\d[\d,]*)\s*(?:({_UNIT})\s*)?[~～∼–]\s*(\d[\d,]*)\s*({_UNIT})(?=$|\s|[),.]|이내|이하|이상)", re.I)


def parse_constraints(proof):
    values, issues = {}, []
    bounds, units = {"min_length": set(), "max_length": set()}, set()
    # OCR may split a single Korean word (e.g. '이 상'). Keep original proof untouched.
    length_proof = re.sub(r"이\s+([내하상])", r"이\1", proof)
    for match in LIMIT_RE.finditer(length_proof):
        unit = UNITS[match[2].lower()]
        bounds["min_length" if match[3] == "이상" else "max_length"].add(int(match[1].replace(",", "")))
        units.add(unit)
    for match in _RANGE.finditer(length_proof):
        lower, upper = int(match[1].replace(",", "")), int(match[3].replace(",", ""))
        unit = UNITS[match[4].lower()]
        if match[2] and UNITS[match[2].lower()] != unit:
            issues.append("conflicting_length_units")
        bounds["min_length"].add(lower)
        bounds["max_length"].add(upper)
        units.add(unit)
    if len(units) > 1:
        issues.append("conflicting_length_units")
    elif units:
        values["length_unit"] = next(iter(units))
    for name, candidates in bounds.items():
        if len(candidates) > 1:
            issues.append("conflicting_" + name)
        elif candidates:
            values[name] = next(iter(candidates))
    if (values.get("min_length", 0) > values.get("max_length", float("inf"))
            or values.get("max_length") == 0):
        issues.append("invalid_length_range")
    if any("length" in issue for issue in issues):
        for name in ("min_length", "max_length", "length_unit"):
            values.pop(name, None)
    text = compact(proof)
    space_rules = [value for token, value in (("공백포함", True), ("공백제외", False)) if token in text]
    if len(space_rules) > 1:
        issues.append("conflicting_space_rules")
    elif space_rules:
        values["includes_spaces"] = space_rules[0]
    # "선택지", "필수 아님" 등을 필수/선택 항목으로 잘못 해석하지 않는다.
    required = bool(re.search(r"(?:^|[\s(\[<:：])필수(?:\s*(?:항목|작성|제출))?(?=$|[\s)\]>:：,])", proof))
    optional = bool(re.search(r"(?:^|[\s(\[<:：])선택(?:\s*(?:항목|사항|작성|제출))?(?=$|[\s)\]>:：,])", proof))
    if required and (optional or re.search(r"필수\s*(?:아님|아니|없|가\s*아니)", proof)):
        issues.append("conflicting_required_rules")
    elif optional and re.search(r"선택\s*(?:사항)?\s*(?:아님|아니|가\s*아니)", proof):
        issues.append("conflicting_required_rules")
    elif required or optional:
        values["required"] = required
    return values, list(dict.fromkeys(issues))
