"""Three-valued evaluation of source-bound, explicitly reviewed condition trees."""
import hashlib
import json
import math

VERSION = 'reviewed-eligibility-rules-v1'
ATTRIBUTES = {'income_bracket', 'gpa', 'gpa_scale', 'grade_level', 'region', 'major'}


def fingerprint(tree, content_hash):
    return hashlib.sha256(json.dumps({'tree': tree, 'content_hash': content_hash}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def review_tree(tree, documents, content_hash, reviewed_logic):
    if reviewed_logic is not True:
        raise ValueError('human review of all conditions, scopes and exceptions is required')
    nodes = 0
    keys = set()
    def check(node, depth=0):
        nonlocal nodes
        nodes += 1
        if not isinstance(node, dict) or depth > 8 or nodes > 100:
            raise ValueError('invalid or excessive condition tree')
        proof = node.get('source') or {}
        matches = [d for d in documents if d.get('sha256') == proof.get('sha256')]
        quote, start, end = proof.get('evidence'), proof.get('text_start'), proof.get('text_end')
        if not (isinstance(quote, str) and quote.strip() and type(start) is int and type(end) is int and 0 <= start < end
                and any(d.get('extracted_text', '')[start:end] == quote for d in matches)):
            raise ValueError('condition quote must match current source bytes and character positions')
        kind = node.get('kind')
        if kind in ('all', 'any'):
            if not isinstance(node.get('children'), list) or len(node['children']) < 2:
                raise ValueError('logic group needs at least two conditions')
            for child in node['children']:
                check(child, depth+1)
        elif kind == 'not':
            check(node.get('child'), depth+1)
        elif kind == 'atom':
            key, attribute, operator, value = (node.get(k) for k in ('key', 'attribute', 'operator', 'value'))
            if not isinstance(key, str) or not key.isascii() or not key.replace('_', '').isalnum() or key in keys:
                raise ValueError('condition keys must be unique stable ASCII identifiers')
            keys.add(key)
            if not isinstance(node.get('question'), str) or not node['question'].strip():
                raise ValueError('each condition needs a source-specific question')
            if attribute not in ATTRIBUTES and attribute != 'reported':
                raise ValueError('unsupported profile attribute')
            if operator not in ('eq', 'in', 'ge'):
                raise ValueError('unsupported comparison')
            if operator == 'in' and (not isinstance(value, list) or not value or any(type(v) not in (str, int, float, bool) for v in value)):
                raise ValueError('in requires a nonempty scalar list')
            if operator == 'ge' and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('numeric threshold required')
            if operator == 'eq' and type(value) not in (str, int, float, bool):
                raise ValueError('scalar expected')
            scalars = value if isinstance(value, list) else [value]
            if any(type(v) is float and not math.isfinite(v) for v in scalars):
                raise ValueError('comparison values must be finite')
            if attribute == 'reported' and (operator != 'eq' or type(value) is not bool):
                raise ValueError('reported conditions require an explicit boolean answer')
        else:
            raise ValueError('unsupported condition kind')
    check(tree)
    return {'version': VERSION, 'tree': tree, 'fingerprint': fingerprint(tree, content_hash), 'reviewed_logic': True}


def current_rules(scholarship):
    rules = (scholarship.eligibility or {}).get('condition_rules')
    if not rules:
        return None
    if (rules.get('version') != VERSION or rules.get('reviewed_logic') is not True
            or rules.get('fingerprint') != fingerprint(rules.get('tree'), scholarship.content_hash)):
        return {'invalid': True}
    return rules


def evaluate_rules(scholarship, profile, reported=None):
    rules = current_rules(scholarship)
    if rules is None:
        return None
    if rules.get('invalid'):
        return {'status': 'unknown', 'reasons': [], 'checks_needed': ['공고 변경으로 복합 자격의 원문 재검토 필요'], 'pending': []}
    pending, checks, atoms, states = [], [], [], {}
    def _evaluate(node):
        kind = node['kind']
        if kind in ('all', 'any'):
            states = [evaluate(c) for c in node['children']]
            if kind == 'all':
                return False if False in states else None if None in states else True
            return True if True in states else None if None in states else False
        if kind == 'not':
            value = evaluate(node['child'])
            return None if value is None else not value
        atoms.append(node)
        value = (reported or {}).get(node['key']) if node['attribute'] == 'reported' else getattr(profile, node['attribute'], None)
        if value is None or value == '':
            pending.append(node)
            checks.append({'key': node['key'], 'status': 'unknown', 'source': node['source']})
            return None
        expected = node['value']
        if node['operator'] == 'ge':
            result = type(value) in (int, float) and value >= expected
        elif node['operator'] == 'eq':
            result = (type(value) is type(expected) or type(value) in (int, float) and type(expected) in (int, float)) and value == expected
        else:
            result = any((type(value) is type(v) or type(value) in (int, float) and type(v) in (int, float)) and value == v for v in expected)
        checks.append({'key': node['key'], 'status': 'satisfied' if result else 'not_eligible', 'source': node['source']})
        return result
    def evaluate(node):
        value = _evaluate(node)
        states[id(node)] = value
        return value

    def needed(node, want=True):
        value = states[id(node)]
        if value is want:
            return []
        if node['kind'] == 'atom':
            return [node]
        if node['kind'] == 'not':
            return needed(node['child'], not want)
        # AND true / OR false need all branches. The other two cases need one
        # branch; prefer unknown and then the branch with fewer missing facts.
        require_all = (node['kind'] == 'all') == want
        candidates = [c for c in node['children'] if states[id(c)] is not want]
        if require_all:
            decisive = [c for c in candidates if states[id(c)] is (not want)]
            return [n for c in (decisive or candidates) for n in needed(c, want)]
        unknown = [c for c in candidates if states[id(c)] is None]
        choices = [needed(c, want) for c in (unknown or candidates)]
        return min(choices, key=len) if choices else []

    result = evaluate(rules['tree'])
    pending = needed(rules['tree'])
    status = 'unknown' if result is None else 'satisfied' if result else 'not_eligible'
    return {'status': status, 'reasons': ['원문 검토된 복합 자격 조건 충족'] if result is True else [],
            'checks_needed': [n['question'] for n in pending] if result is None else [],
            'pending': pending if result is not True else [], 'checks': checks}
