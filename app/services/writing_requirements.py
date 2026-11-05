"""실제 문항에 대응하는 정보 수집 항목. 원문 필드 자체를 추가하지 않는다."""
import hashlib
import json
import re
from app.schemas.schemas import UserProfile
from app.services.matching import evaluate_eligibility
from app.services.form_semantics import MANUAL_TYPES, applicability_state
from app.services.form_content import item_minimum, duplicate_slot


def field_label(field):
    role = {"student": "학생", "guardian": "보호자", "applicant": "신청인", "recommender": "추천인"}.get(field.get("role"))
    subject = (field.get("subject") or {}).get("label")
    label = f"{subject or role} · {field['label']}" if subject or role else field["label"]
    if (field.get("choice_spec") and field["choice_spec"].get("multiple_explicit")) or (field.get("input_spec") or {}).get("person_row"):
        label = f"원본 표 {field['layout']['row'] + 1}행 · {label}"
    section = {"collection_use": "수집·이용", "provision_inquiry": "제공·조회", "identity_processing": "고유식별정보 처리"}.get((field.get("section") or {}).get("kind"))
    return f"{section} · {label}" if section else label


def _grade_input_guidance(slot):
    if slot.get("kind") == "checklist_income_bracket":
        return f" 원문에 지정된 학기의 실제 소득구간을 0 이상의 정수로 알려주세요. 원문 상한은 {slot['maximum']}분위입니다. 다른 신청 항목의 답변에서 자동 복사하지 않습니다."
    if slot.get("kind") == "gpa_scale":
        return " 원본 만점 기준 중 본인의 기준을 선택하세요: " + " / ".join(slot["options"])
    if slot.get("kind") in ("gpa", "nonnegative_number"):
        return " 이 항목의 원본 학기/범위에 해당하는 0 이상의 숫자를 입력하세요. 다른 성적에서 자동 변환하지 않습니다."
    return ""


def requirements(field, answer=None):
    if field["field_type"] in MANUAL_TYPES or (field.get("manual_spec") or {}).get("mode") == "original_only":
        return []
    if field.get("input_spec"):
        from app.services.form_external_history import input_slots
        if field['input_spec'].get('version') == 'spreadsheet-recommendation-v1':
            return [{'key': s['key'], 'source': s['source'], 'destination': s['destination'], 'question':
                     f"'{field_label(field)}'의 '{s['label']}'에 넣을 해당 작성 대상의 실제 정보를 알려주세요." +
                     (' 생년월일은 YYYY-MM-DD로 알려주세요.' if s['kind'] == 'reported_birth_date' else
                      ' 원문 만 나이를 본인이 확인하여 숫자로 알려주세요. 생년월일에서 자동 계산하지 않습니다.' if s['key'] == 'age' else
                      ' 실제 학년을 숫자로 알려주세요.' if s['key'] == 'grade' else
                      ' 한자 이름을 기재하지 않으려면 미기재로 알려주세요. 원문 필수 여부는 확인되지 않았습니다.' if s['key'] == 'chinese_name' else '') +
                     (' 추천인에게 확인한 정보만 답해주세요. 이 답변은 추천인의 실제 서명이나 추천 의사 확인을 대신하지 않습니다.' if s['role'] == 'recommender' else '')}
                    for s in input_slots(field, answer)]
        if field['input_spec'].get('version') == 'spreadsheet-education-v1':
            rows = []
            for slot in input_slots(field, answer):
                kind = slot['kind']
                if kind == 'education_presence':
                    question = f"원본의 '{slot['label']}'를 알려주세요: 작성함 / 작성하지 않음. 미리 인쇄된 학교명은 본인의 학력 사실이 아닙니다. 작성하지 않으면 이 행을 비워 둡니다."
                else:
                    question = f"'{slot['label']}'에 넣을 실제 내용을 알려주세요."
                    if kind == 'education_month':
                        question += " 실제 졸업한 년월을 YYYY-MM으로 알려주세요. 아직 졸업하지 않았다면 '졸업 전', 원본 칸을 비워 두려면 '표시하지 않음'으로 답해 주세요. 예정 졸업일은 자동으로 만들지 않습니다."
                    elif kind == 'education_school':
                        question += f" '{slot['template']}'는 인쇄된 틀입니다. 본인의 실제 학교명을 알려주세요."
                    elif kind == 'education_status' and slot['original_options']:
                        question += ' 원문 구분: ' + ' / '.join(slot['original_options']) + ". 표시할 수 있는 항목이 없으면 '표시하지 않음'으로 답하고 원본을 확인하세요."
                    elif kind == 'education_status':
                        question += " 이 행에는 인쇄된 구분 선택지가 없습니다. 실제 내용을 알려주거나 비워 두려면 '표시하지 않음'으로 답해 주세요."
                    else:
                        question += " 해당 내용이 없으면 '해당 없음'으로 답해 주세요."
                rows.append({'key': slot['key'], 'source': slot['source'], 'destination': slot.get('destination'), 'question': question})
            return rows
        if field['input_spec'].get('version') == 'spreadsheet-repeating-v1':
            capacity = field['input_spec']['repeat']['collection_limit']
            return [{'key': slot['key'], 'source': slot['source'], 'destination': slot.get('destination'),
                     'question': (f"'{field_label(field)}'의 현재 양식 {capacity}개 작성 행에 넣을 내역 수를 0~{capacity}로 알려주세요. 없으면 0입니다. 추가 내역은 원본 배치를 확인해야 합니다."
                                  if slot['kind'] == 'row_count' else
                                  f"'{field_label(field)}'의 '{slot['label']}'에 넣을 실제 내용을 알려주세요." +
                                  (' 원문 값 Y 또는 N으로 답해 주세요.' if slot['kind'] == 'sheet_enum' else
                                   ' 날짜는 YYYY-MM-DD로 알려주세요.' if slot['kind'] == 'date' else
                                   ' 해당 내용이 없으면 해당 없음으로 답할 수 있습니다.'))}
                    for slot in input_slots(field, answer)]
        if field["input_spec"].get("paragraph_template"):
            return [{"key": slot["key"], "source": slot["source"], "question":
                     f"'{field_label(field)}'의 '{slot['label']}'에 넣을 본인의 실제 값을 알려주세요." + _grade_input_guidance(slot) +
                     " 확인한 값은 준비 초안에 기록합니다. 원본 비고에 옮긴 뒤 해당 문단의 위치와 만점/단위를 직접 확인하세요.\n원문 작성 지침:\n" + field["instructions"]}
                    for slot in input_slots(field, answer)]
        if field["input_spec"].get("preparation_only"):
            return [{"key": slot["key"], "source": slot["source"], "question":
                     f"'{field_label(field)}'의 이번 신청 준비 상태를 알려주세요: " + " / ".join(slot["options"]) +
                     ". 이 답변은 사용자 보고이며 원본 표시나 실제 파일 검증·제출 완료가 아닙니다.\n원문 지침:\n" + field["instructions"]
                     if slot["kind"] == "preparation_status" else
                     f"'{field_label(field)}'의 아래 원문 발급일·주소·서명·제출 형식을 실제 서류와 대조한 뒤 '원문 조건 확인'으로 답해 주세요. " +
                     ("경제형편 증빙의 '택1'과 '해당 서류 1가지 이상' 지침을 함께 확인하고 최종 제출 대상을 원본에서 직접 확인하세요. " if field["preparation_spec"]["economic_rule_review_required"] else "") +
                     "이 응답으로 서류 검증이 완료되지는 않습니다.\n원문 지침:\n" + field["instructions"]}
                    for slot in input_slots(field, answer)]
        return [{"key": slot["key"], "question": f"'{field_label(field)}'의 '{slot['label']}'에 넣을 실제 내용을 알려주세요." + _grade_input_guidance(slot) +
                 (" 원본 제외 조건을 확인하고 '제외 조건 확인'으로 답해 주세요. 제외 여부가 불명확한 내역은 원문과 먼저 대조해 주세요." if slot.get("kind") == "scope_confirmation" else " 제외 조건을 적용한 뒤 작성할 내역 수를 알려주세요. 없으면 0입니다. 한 대화는 최대 100건을 수집하며 이는 원본의 행 수 제한이 아닙니다." if slot.get("kind") == "row_count" else " 실제 수혜연도를 네 자리 숫자로 알려주세요. 원본 연도나 다른 답변에서 자동으로 채우지 않습니다." if slot.get("kind") == "year" else " 실제 날짜를 YYYY-MM-DD로 알려주세요. 예정 날짜도 본인이 확인한 날짜로 입력하세요." if slot.get("kind") == "date" else " 금액과 실제 단위(원 또는 만원)를 함께 알려주세요." + (" 일시 장학이면 원문 지침대로 '일시'로 알려주세요." if slot.get("kind") == "semester_amount" else "") if slot.get("kind") in ("semester_amount", "reported_amount") else f" {slot['unit']} 단위의 숫자로 입력하세요. 해당 수입이 없으면 0으로 알려주세요." if slot.get("kind") == "amount" else " 인원수를 숫자로 입력하세요." if slot.get("kind") == "nonnegative_integer" else " 본인의 순서를 숫자로 입력하세요." if slot.get("kind") == "positive_integer" else ""),
                 "source": slot["source"]} for slot in input_slots(field, answer)]
    if field["field_type"] == "checkbox" and field.get("options"):
        if field.get("choice_spec"):
            from app.services.form_choice_values import value_slots
            return [{"key": "value", "question": f"'{field_label(field)}'의 원문에서 실제로 표시할 항목을 선택해 주세요." + ("" if field.get("selection_mode") == "single" else " 여러 항목은 목록으로 보낼 수 있습니다.") + " " + " / ".join(field["options"])}] + [
                {"key": slot["key"], "question": f"선택한 '{slot['label']}'의 " +
                 (f"금액을 {slot['unit']} 단위의 숫자로 알려주세요." if slot["kind"] == "amount" else f"실제 구간을 숫자로 알려주세요. 원문 기준은 {slot['maximum']}구간 이내입니다." if slot["kind"] == "integer_upper_bound" else "증빙서류를 제출할 수 있는지 확인해 주세요. 가능하면 '제출 가능'으로 답해 주세요. 제출이 불가능하면 원문 인정 조건을 충족하지 않아 이 항목으로 초안을 완성할 수 없습니다. 이 답변은 서류 검증 완료가 아닌 본인의 응답으로 기록됩니다." if slot["kind"] == "evidence_availability" else "실제 장기요양 등급을 알려주세요." if slot["kind"] == "grade" else "실제 횟수를 숫자로 알려주세요." if slot["kind"] == "positive_integer" else "원본 칸에 넣을 실제 내용을 알려주세요."),
                 "source": slot["source"]} for slot in value_slots(field, answer)]
        if field.get("selection_mode") == "single_mark":
            return [{"key": "value", "question": f"'{field_label(field)}'의 원문에는 '동의함' 표시 칸 하나가 있습니다. 원문 내용을 확인하고 '동의함' 또는 '표시하지 않음'으로 알려주세요. '표시하지 않음'은 칸을 비워 두겠다는 본인의 응답으로 기록하며 원문 선택지를 추가하지 않습니다. 실제 동의 표시는 해당 본인이 원본에서 직접 확인해야 합니다.\n원문 지침: {field.get('instructions') or field['evidence']}"}]
        return [{"key": "value", "question": f"'{field_label(field)}'의 원문 선택지 중 본인이 선택할 내용을 알려주세요: " +
                 " / ".join(field["options"]) + (". 선택 내용은 준비 자료에 기록하며, 원본의 동의 표시는 본인이 직접 확인하세요.\n원문 동의 내용:\n" + field.get("consent_context", {}).get("text", field["evidence"]) if field.get("semantic_kind") == "consent" else "") }]
    if field["field_type"] != "essay":
        return [{"key": "value", "question": f"'{field_label(field)}'에 넣을 정확한 내용을 알려주세요."}]
    minimum = item_minimum(field)
    from app.services.essay_collection import active_details
    details = [{"key": r["key"], "question": f"원문에서 요청한 '{r.get('collection_label', r['evidence'])}'에 대한 실제 내용을 알려주세요. " +
                ("지원 여부를 '지원 있음' 또는 '지원 없음'으로 알려주세요. 지원 내용과 기관은 지원이 있는 경우 따로 확인합니다." if r.get('response_options') else "해당 내용이 없으면 '해당 없음'으로 답할 수 있습니다.") +
                f"\n원문 요청: {r['context']}", "source": r["source"]} for r in active_details(field, answer)]
    if minimum:
        return [{"key": f"original_item_{index}", "question": f"원문은 {minimum}가지 이상의 항목을 요구합니다. {index}번째 항목의 구체적인 내용을 알려주세요. 앞 항목과 구분되는 본인의 내용을 적어주세요."}
                for index in range(1, minimum + 1)] + details
    if details:
        return details
    label = re.sub(r"\s", "", field["label"])
    if "동기" in label:
        items = [("reason", "이 장학금을 신청하는 구체적인 이유를 알려주세요."), ("impact", "장학금이 본인의 학업이나 계획에 어떻게 도움이 될지 알려주세요.")]
    elif "학업" in label or "진로" in label:
        items = [("goal", "본인의 구체적인 학업 또는 진로 목표를 알려주세요."), ("action", "목표를 위해 본인이 실행할 계획을 알려주세요."), ("schedule", "실행 시기나 일정을 알려주세요.")]
    elif "활동" in label:
        items = [("goal", "본인이 계획하는 활동의 목표를 알려주세요."), ("action", "활동에서 본인의 역할과 실행 방법을 알려주세요."), ("schedule", "활동 일정과 기대하는 결과를 알려주세요.")]
    elif "자기소개" in label:
        items = [("strength", "소개하고 싶은 본인의 강점이나 가치를 알려주세요."), ("experience", "그 강점을 보여주는 본인의 실제 경험을 알려주세요."), ("learning", "그 경험에서 본인이 배운 점을 알려주세요.")]
    elif "활용" in label or "사용" in label:
        items = [("purpose", "장학금을 어디에 사용할 계획인지 알려주세요."), ("plan", "구체적인 활용 방법과 일정을 알려주세요.")]
    else:
        items = [("content", f"'{field['label']}'의 원문 지침에 맞는 본인 정보를 알려주세요.")]
    # 원문이 명시한 내용은 일반적인 질문 구성보다 우선한다.
    guidance = field.get("instructions") or field["evidence"]
    extras = [("experience", "실제로 본인이 맡은 역할과 수행한 경험을 알려주세요.", "경험"),
              ("result", "본인이 확인할 수 있는 실제 결과나 배운 점을 알려주세요.", "성과")]
    for key, question, keyword in extras:
        if keyword in guidance and not any(k == key for k, _ in items):
            items.append((key, question))
    return [{"key": key, "question": question} for key, question in items]


def normalize_answers(session):
    answers = json.loads(json.dumps(session.answers or {}, ensure_ascii=False))
    for field in session.fields:
        item = answers.get(field["id"])
        if item and "facts" not in item:
            slots = requirements(field)
            item["facts"] = {slots[0]["key"]: {"text": item["text"], "confirmed": False, "source": {"kind": "carried_profile" if item.get("status") == "carried_profile" else "conversation"}}} if slots else {}
        if item:
            from app.services.essay_collection import normalize_details
            normalize_details(field, item)
    return answers


def notice_fingerprint(scholarship):
    eligibility = scholarship.eligibility or {}
    core = {"eligibility": {k: eligibility.get(k) for k in ("income_bracket", "gpa_min", "grade_level", "region", "major", "target_desc", "scope", "condition_rules")},
            "benefit": scholarship.benefit or {}, "documents": scholarship.required_documents or []}
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def writing_fingerprint(scholarship):
    from app.services.scholarship_readiness import form_fingerprint
    return hashlib.sha256(f"{form_fingerprint(scholarship)}:{notice_fingerprint(scholarship)}".encode()).hexdigest()


def next_action(session, scholarship, answers, workflow):
    eligibility = scholarship.eligibility or {}
    profile = UserProfile.model_validate(session.profile)
    from app.services.eligibility_rules import evaluate_rules
    rule_result = evaluate_rules(scholarship, profile, workflow.get('qualification_answers'))
    if rule_result is not None and rule_result['status'] != 'satisfied':
        pending = rule_result.get('pending', [])
        if not pending:
            return {'kind': 'qualification', 'key': 'rules_review', 'failed': rule_result['status'] == 'not_eligible',
                    'question': '복합 자격의 현재 답변 또는 변경된 공고 원문을 다시 확인하세요. 자격이 충족되기 전에는 초안을 만들 수 없습니다.'}
        node = pending[0]
        return {'kind': 'qualification', 'key': 'rule:' + node['key'], 'rule': node, 'failed': rule_result['status'] == 'not_eligible',
                'question': node['question'] + '\n원문 조건: ' + node['source']['evidence'] +
                            (' 해당하면 예, 해당하지 않으면 아니요로 알려주세요.' if node['attribute'] == 'reported' else '')}
    # 불일치한 조건도 정정할 수 있지만, 충족하기 전에는 생성하지 않는다.
    checks = (("income_bracket", "income_bracket", "소득분위"), ("gpa_min", "gpa", "학점"),
              ("region", "region", "거주 지역"), ("grade_level", "grade_level", "학년"))
    for criterion, key, label in checks:
        if rule_result is not None:
            break
        allowed, value = eligibility.get(criterion), getattr(profile, key)
        if allowed is None or allowed == [] or allowed == "":
            continue
        failed = False
        if value is not None:
            if criterion == "gpa_min":
                failed = value < allowed
            elif criterion == "grade_level":
                failed = value not in [str(v) for v in allowed]
            elif criterion == "region":
                failed = value not in allowed and "전국" not in allowed
            else:
                failed = value not in allowed
        if value is None or failed:
            return {"kind": "qualification", "key": key, "failed": failed,
                    "question": f"공고의 {label} 기준은 {allowed}입니다. 현재 본인의 {label}을 정확히 알려주세요."}
    if rule_result is None and evaluate_eligibility(scholarship, profile)["checks_needed"] and not workflow.get("additional_qualification_confirmed"):
        detail = eligibility.get("target_desc") or eligibility.get("scope") or "공고 원문의 모든 추가 자격 조건"
        return {"kind": "qualification", "key": "additional", "question": f"{detail}\n공고 원문을 확인했고 본인이 추가 자격 조건에 해당하는지 알려주세요."}
    for field in session.fields:
        applicability = applicability_state(field, workflow)
        if applicability == "unknown":
            condition = field["applicability"]
            return {"kind": "applicability", "condition_id": condition["id"], "field_id": field["id"],
                    "question": (f"원문 제출 지침: {condition['evidence']}\n이번 신청에서 학자금지원구간통지서를 제출 대상으로 선택하셨습니까? 선택했다면 '예', 선택하지 않았다면 '아니요'로 알려주세요. 준비 상태나 다른 증빙 답변으로 자동 판단하지 않습니다."
                                 if condition.get("mode") == "user_selected_document" else f"원문 작성 조건: {condition['evidence']}\n이 조건에 해당합니까? 해당하면 '예', 해당하지 않으면 '아니요'로 알려주세요.")}
        if applicability == "not_applicable":
            continue
        slots = requirements(field, answers.get(field["id"]))
        if not slots:
            continue
        item = answers.get(field["id"], {})
        if item.get("status") == "skipped" and field.get("required") is False:
            continue
        facts = item.get("facts", {})
        for slot in slots:
            if not facts.get(slot["key"], {}).get("text"):
                from app.services.essay_responses import followup
                issue = item.get('previous_details', {}).get(slot['key'], {}).get('response_issue')
                return {"kind": "field", "field_id": field["id"], "slot_key": slot["key"],
                        "question": (followup(issue) + "\n" if issue else "") + f"'{field_label(field)}'\n{slot['question']}\n원문 지침: {field.get('instructions') or field['evidence']}"}
        duplicate = duplicate_slot(field, facts)
        if duplicate:
            return {"kind": "field", "field_id": field["id"], "slot_key": duplicate,
                    "question": f"'{field_label(field)}'의 항목이 중복됩니다. 원문의 {item_minimum(field)}가지 요건을 위해 다른 구체적인 항목으로 수정해 주세요."}
        if any(not f.get("confirmed") for f in facts.values()):
            if field.get("semantic_kind") == "consent":
                return {"kind": "confirm", "field_id": field["id"], "question": f"'{field_label(field)}'의 원문 동의 내용과 선택을 확인해 주세요. 본인이 직접 선택한 내용만 준비 자료에 기록하고 동의 표시는 원본에서 직접 합니다."}
            if field.get("semantic_kind") == "submission_checklist":
                return {"kind": "confirm", "field_id": field["id"], "question": f"'{field_label(field)}'의 이번 신청 준비 상태가 본인의 현재 응답과 맞는지 확인해 주세요. 실제 첨부·제출 완료는 원본과 서류에서 직접 확인합니다."}
            if field['field_type'] == 'essay':
                from app.services.essay_collection import active_details
                labels = {r['key']: r.get('collection_label', r['evidence']) for r in active_details(field, item)}
                review = '\n'.join(f"- {labels.get(slot['key'], slot['question'])}: {facts[slot['key']]['text']}" for slot in slots)
                return {'kind':'confirm', 'field_id':field['id'], 'question':
                        f"'{field_label(field)}'의 각 질문과 답변을 대조해 주세요.\n{review}\n각 답변이 해당 질문을 설명하고 이번 신청의 본인 사실과 맞습니까? 빠진 내용이나 다른 질문의 답변은 항목별로 수정한 뒤 확인해 주세요."}
            return {"kind": "confirm", "field_id": field["id"], "question": f"'{field_label(field)}'에 모은 정보를 확인해 주세요. 해당 작성 대상의 사실인지 확인하거나 항목별로 수정할 수 있습니다."}
    return None
