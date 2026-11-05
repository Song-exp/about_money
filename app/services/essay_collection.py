"""명시 복합 요청의 부분 근거를 유지한 사실별 수집. 문장 의미를 추정하지 않는다."""
import hashlib
from copy import deepcopy

# 보수적으로 확인한 문장만 분리한다. 대안(통원·입원, 단체나 국가)은 분리하지 않는다.
COMPOUNDS = {
    "평소 어떤 돌봄을 얼마나 자주 제공하는지": [("평소 어떤 돌봄을", "돌봄 내용"), ("얼마나 자주 제공하는지", "돌봄 빈도")],
    "어떤 돌봄을 얼마나 자주 제공하는지": [("어떤 돌봄을", "돌봄 내용"), ("얼마나 자주 제공하는지", "돌봄 빈도")],
    "그로 인해 학업과 생계에 어떤 영향을 받고 있는지": [("학업", "학업에 미치는 영향"), ("생계", "생계에 미치는 영향")],
    "학업과 생계에 어떤 영향을 받고 있는지": [("학업", "학업에 미치는 영향"), ("생계", "생계에 미치는 영향")],
    "있었다면 주로 어떤 도움을 어디서 받았는지": [("있었다면 주로 어떤 도움을", "이전 지원 내용"), ("어디서 받았는지", "이전 지원 기관")],
    "병원 방문 시 이동·동행 방법": [("이동", "병원 방문 시 이동 방법"), ("동행", "병원 방문 시 동행 방법")],
}
SUPPORT_STATUS = "지금까지 외부 단체나 국가 지원이 있었는지"


def detail_slots(field):
    result = []
    rules = [r for r in field.get('content_requirements', []) if r.get('kind') == 'prompted_detail']
    status = next((r for r in rules if r.get('evidence') == SUPPORT_STATUS), None)
    for rule in rules:
        parts = COMPOUNDS.get(rule.get('evidence'))
        if not parts:
            result.append(deepcopy(rule))
        else:
            for fragment, label in parts:
                child = deepcopy(rule)
                relative = rule['evidence'].index(fragment)
                proof = child['source']
                proof.update(text_start=proof['text_start'] + relative,
                             text_end=proof['text_start'] + relative + len(fragment), evidence=fragment)
                digest = hashlib.sha256((rule['key'] + ':' + fragment).encode()).hexdigest()[:12]
                child.update(key='original_detail_' + digest, evidence=fragment, collection_label=label, parent_key=rule['key'])
                if status and rule['evidence'] == '있었다면 주로 어떤 도움을 어디서 받았는지':
                    child['depends_on'] = {'key': status['key'], 'value': '지원 있음'}
                result.append(child)
    if status:
        next(r for r in result if r['key'] == status['key'])['response_options'] = ['지원 있음', '지원 없음']
    return result


def active_details(field, answer=None):
    facts = (answer or {}).get('facts', {})
    return [r for r in detail_slots(field) if not r.get('depends_on') or
            facts.get(r['depends_on']['key'], {}).get('text') == r['depends_on']['value']]


def retired_keys(field):
    return {r['parent_key'] for r in detail_slots(field) if r.get('parent_key')}


def needs_recollection(session):
    from app.services.essay_responses import response_issue
    for field in session.fields:
        facts = (session.answers or {}).get(field['id'], {}).get('facts', {})
        if any(response_issue(field, key, fact['text']) for key, fact in facts.items()) or retired_keys(field).intersection(facts) or any(
                r.get('response_options') and r['key'] in facts and facts[r['key']]['text'] not in r['response_options']
                for r in detail_slots(field)):
            return True
    return False


def normalize_details(field, item):
    from app.services.essay_responses import response_issue
    # 과거 복합 응답은 참고용으로만 보관한다. 자식 사실로 복제하거나 확정하지 않는다.
    old = retired_keys(field) | {r['key'] for r in detail_slots(field) if r.get('response_options') and
                               r['key'] in item.get('facts', {}) and item['facts'][r['key']]['text'] not in r['response_options']}
    old |= {key for key, fact in item.get('facts', {}).items() if response_issue(field, key, fact['text'])}
    for key in old:
        fact = item.get('facts', {}).pop(key, None)
        if fact:
            issue = response_issue(field, key, fact['text'])
            item.setdefault('previous_details', {})[key] = {**fact, 'confirmed': False, **({'response_issue':issue} if issue else {})}
    if item.get('previous_details'):
        item['text'] = '\n\n'.join(dict.fromkeys(f['text'] for f in item.get('facts', {}).values()))


def check_detail(field, key, text, answer=None):
    slots = detail_slots(field)
    rule = next((r for r in slots if r['key'] == key), None)
    if not rule:
        return
    if rule not in active_details(field, answer):
        raise ValueError('inactive detail')
    if rule.get('response_options') and text not in rule['response_options']:
        raise ValueError('explicit support status required')


def clear_dependents(field, item, key, text):
    slots = detail_slots(field)
    children = [r for r in slots if r.get('depends_on', {}).get('key') == key]
    if children and item.get('facts', {}).get(key, {}).get('text') != text:
        for child in children:
            item.get('facts', {}).pop(child['key'], None)
        for fact in item.get('facts', {}).values():
            fact['confirmed'] = False
