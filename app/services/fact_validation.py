"""원문 발췌와 값의 관계를 보수적으로 검사한다. 불명확한 조건은 게시하지 않는다."""
import re


def _readable(evidence):
    # 해석용으로만 제어 문자와 한국어 글자 사이 OCR 공백을 처리한다.
    # 숫자 사이 공백은 합치지 않으며 저장 근거는 수정하지 않는다.
    text = ''.join(c if ord(c) >= 32 or c.isspace() else ' ' for c in evidence)
    return re.sub(r'(?<=[가-힣])[^\S\n]+(?=[가-힣])', '', text)


def _income_context(evidence):
    if re.search('소득|학자금.*지원', evidence):
        return True
    # 국가장학금 신청자 범위와 지원구간 안내가 함께 인용된 경우만 해석한다.
    return bool(re.search(r'국가장학금[^\n]{0,45}신청자\s*중[^\n]{0,30}\d\s*[~～\-]\s*\d\s*구간', evidence)
                and re.search('지원구간심사', evidence))


def validate_fact(group, key, value, evidence):
    original_evidence = evidence
    evidence = _readable(evidence)
    if group == "benefit":
        if key == "amount_desc":
            return isinstance(value, str) and value in original_evidence
        if key == "amount_krw":
            if type(value) not in (int, float) or value <= 0 or not re.search("장학금|지원금|지급액|지원내용", evidence):
                return False
            amounts = [float(m[1].replace(",", "")) * {"만원": 10000, "백만원": 1000000, "천원": 1000, "원": 1}[m[2]]
                       for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(백만원|만원|천원|원)", evidence)]
            return len(set(amounts)) == 1 and value == amounts[0]
        return False
    if key == "gpa_min":
        # 서로 같은 최소 평점을 표현하는 두 만점 척도만 대안 예외에서 제외한다.
        # 3.0/4.5 또는 3.5/4.3 같은 서로 다른 기준은 계속 거부한다.
        def same_scale_threshold(match):
            return match[1] if float(match[1]) == float(match[2]) else match[0]
        evidence = re.sub(r"([0-4](?:\.\d+)?)\s*/\s*4\.[35]\s*또는\s*([0-4](?:\.\d+)?)\s*/\s*4\.[35]", same_scale_threshold, evidence)
    if key != "target_desc" and re.search(r"제외|미만|초과|아닌|예외|또는|단\s*[,：:]", evidence):
        # 현재 정형 필터로 표현할 수 없는 예외/대안 조건을 단순 조건으로 바꾸지 않는다.
        return False
    if key == "gpa_min":
        if type(value) not in (int, float) or not 0 <= value <= 4.5:
            return False
        matches = re.findall(r"(?:학점|평점|GPA)[^\n\d]{0,15}(?:[A-F][0+\-]\s*)?\(?\s*([0-4](?:\.\d+)?)\s*(?:/\s*4\.[35]\s*)?\)?\s*(?:점\s*)?이상", evidence, re.I)
        return len(set(matches)) == 1 and float(matches[0]) == value
    if key == "income_bracket":
        if not isinstance(value, list) or not value or any(type(v) is not int or not 0 <= v <= 10 for v in value):
            return False
        if not _income_context(evidence):
            return False
        ranges = list(re.finditer(r"(?<!\d)(\d+)\s*(?:분위|구간)?\s*[~～\-]\s*(\d+)\s*(?:분위|구간)", evidence))
        if ranges:
            if len(ranges) != 1 or not 0 <= int(ranges[0][1]) <= int(ranges[0][2]) <= 10:
                return False
            remaining = evidence[:ranges[0].start()] + evidence[ranges[0].end():]
            if re.search(r'\d+\s*(?:분위|구간)', remaining):
                return False
            return sorted(set(value)) == list(range(int(ranges[0][1]), int(ranges[0][2]) + 1))
        match = re.search(r"(\d+)\s*(?:분위|구간)\s*이하", evidence)
        if match:
            # 0구간/기초생활수급자 포함 여부를 임의로 결정하지 않는다.
            return False
        numbers = re.findall(r"(\d+)\s*(?:분위|구간)", evidence)
        return bool(numbers) and sorted(set(value)) == sorted({int(n) for n in numbers})
    if key == "grade_level":
        if re.search(r"학년\s*(?:이상|이하)", evidence):
            return False
        ranges = list(re.finditer(r"([1-6])\s*[~～\-]\s*([1-6])\s*학년", evidence))
        if ranges:
            if len(ranges) != 1 or int(ranges[0][1]) > int(ranges[0][2]):
                return False
            remaining = evidence[:ranges[0].start()] + evidence[ranges[0].end():]
            if re.search(r"[1-6]\s*학년", remaining):
                return False
            return (isinstance(value, list) and all(isinstance(v, str) for v in value)
                    and sorted(set(value)) == [str(n) for n in range(int(ranges[0][1]), int(ranges[0][2]) + 1)])
        return (isinstance(value, list) and bool(value) and
                all(isinstance(v, str) for v in value) and
                sorted(set(value)) == sorted(set(re.findall(r"([1-6])\s*학년", evidence))))
    if key == "region":
        return (isinstance(value, list) and bool(value) and
                bool(re.search("거주|주소|지역|전국", evidence)) and
                all(isinstance(v, str) and v.strip() and v in evidence for v in value))
    if key == "target_desc":
        return isinstance(value, str) and bool(value.strip()) and value in original_evidence
    return False


def grounded_facts(data, chunk, source):
    facts, issues = [], []
    evidence_map = data.get("fact_evidence") or {}
    if not isinstance(evidence_map, dict):
        evidence_map = {}
    groups = {}
    for group in ("eligibility", "benefit"):
        groups[group] = {}
        values = data.get(group)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if value is None or value == [] or value == "":
                continue
            evidence = evidence_map.get(f"{group}.{key}")
            if not isinstance(evidence, str) or not evidence.strip() or evidence not in chunk or not validate_fact(group, key, value, evidence):
                issues.append(f"unsupported_fact:{group}.{key}")
                continue
            groups[group][key] = value
            facts.append({"group": group, "key": key, "value": value, "evidence": evidence, "source": source or {}})
    return groups, facts, issues
