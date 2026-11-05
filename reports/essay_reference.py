"""Bounded source-authored essay contracts, independent of production restoration."""
from reports.atomic_input_reference import record_of
from reports.form_semantic_reference import SOURCE_SHA256
from reports.choice_repeat_reference import proof_matches
from reports.submission_consent_reference import cell_matches

# Explicit original clauses, not inferred from model outputs or production keys.
CLAUSES = {
    1070: [(1070, "본인에 대한 간단한 소개"), (1070, "가족 돌봄 상황"),
           (1074, "가족 구성"), (1074, "현재 경제 상황"),
           (1074, "돌봄이 필요한 가족의 건강 상태"), (1074, "통원·입원 치료 여부"),
           (1074, "병원 방문 시 이동·동행 방법"), (1074, "치료비 부담"),
           (1074, "평소 어떤 돌봄을 얼마나 자주 제공하는지"),
           (1074, "그로 인해 학업과 생계에 어떤 영향을 받고 있는지")],
    1083: [(1087, "지금까지 외부 단체나 국가 지원이 있었는지"),
           (1087, "있었다면 주로 어떤 도움을 어디서 받았는지"),
           (1087, "그럼에도 불구하고 사각지대로서 필요한 지원이 무엇인지")],
    1111: [], 1120: [],
}
DESTINATIONS = {1070: 1077, 1083: 1094, 1111: 1114, 1120: 1123}


def build_essay_gold(inventory):
    if inventory['source_sha256'] != SOURCE_SHA256:
        raise ValueError('not the annotated original')
    texts = {t['record']: t for t in inventory['sections'][0]['texts']}
    cells = {c['record']: c for c in inventory['sections'][0]['list_records']}
    prompts = []
    for record, clauses in CLAUSES.items():
        for parent, fragment in clauses:
            assert texts[parent]['text'].count(fragment) == 1
        cell = cells[DESTINATIONS[record]]
        assert len(cell['geometry_candidates']) == 1
        prompts.append({'record': record, 'source': texts[record], 'cell': cell,
                        'clauses': [{'source': texts[parent], 'fragment': fragment} for parent, fragment in clauses]})
    return {'version': 'donghaeng-essay-reference-v1', 'source_sha256': SOURCE_SHA256,
            'prompts': prompts, 'shared_rule': texts[1056], 'copy_rule': texts[1105],
            'format_rules': [('font_family', '맑은고딕'), ('font_size', '글씨크기 10pt'), ('line_spacing', '줄 간격 160 ')],
            'compound_response_completeness_verified': False, 'complete_atomic_gold': False,
            'whole_form_complete': False, 'independent_human_review_complete': False}


def audit_essays(gold, fields, groups):
    checks, sha, selected = [], gold['source_sha256'], []
    for prompt in gold['prompts']:
        matches = [f for f in fields if record_of(f.get('source')) == prompt['record']]
        issues = []
        if len(matches) != 1:
            issues.append('missing_or_duplicate_prompt')
        else:
            f = matches[0]
            selected.append(f)
            if f.get('label') != prompt['source']['text'] or f.get('field_type') != 'essay':
                issues.append('prompt_changed')
            if not proof_matches({**f['source'], 'evidence': f.get('evidence')}, prompt['source'], sha):
                issues.append('prompt_source_changed')
            if not cell_matches(f.get('input_cell_source', {}), prompt['cell'], sha):
                issues.append('destination_changed')
            actual = f.get('content_requirements', [])
            if len(actual) != len(prompt['clauses']):
                issues.append('detail_count_changed')
            for clause in prompt['clauses']:
                found = [r for r in actual if r.get('evidence') == clause['fragment']]
                if len(found) != 1:
                    issues.append('detail_missing_or_duplicate')
                    continue
                r = found[0]
                if (r.get('kind') != 'prompted_detail' or record_of(r.get('source')) != clause['source']['record']
                        or r['source'].get('sha256') != sha or r['source'].get('evidence') != clause['fragment']
                        or r.get('context') not in clause['source']['text']
                        or record_of(r.get('context_source')) != clause['source']['record']
                        or r['context_source'].get('sha256') != sha
                        or r['context_source'].get('evidence', '').strip() != r.get('context')):
                    issues.append('detail_proof_changed')
            if any(f.get(k) is not None for k in ('min_length', 'max_length', 'length_unit')):
                issues.append('shared_limit_applied_per_prompt')
            policy = f.get('history_reuse_policy', {})
            if policy.get('mode') != 'direct_copy_prohibited' or not any(
                    proof_matches(p, gold['copy_rule'], sha) for p in policy.get('sources', [])):
                issues.append('copy_policy_missing')
        checks.append({'record': prompt['record'], 'issues': sorted(set(issues))})
    shared = [g for g in groups if record_of(g.get('source')) == 1056]
    issues = []
    if len(shared) != 1:
        issues.append('shared_group_missing_or_duplicate')
    else:
        g = shared[0]
        if g.get('field_ids') != [f['id'] for f in selected] or any(g['id'] not in f.get('group_ids', []) for f in selected):
            issues.append('shared_members_changed')
        if g.get('constraints') != {'length_unit': 'pages', 'max_length': 3}:
            issues.append('shared_page_limit_changed')
        if not proof_matches(g.get('source', {}), gold['shared_rule'], sha) or g.get('evidence') != gold['shared_rule']['text']:
            issues.append('shared_source_changed')
        if [(r.get('kind'), r.get('evidence')) for r in g.get('format_rules', [])] != [tuple(r) for r in gold['format_rules']]:
            issues.append('shared_format_changed')
        if any(record_of(r.get('source')) != 1056 or r['source'].get('sha256') != sha
               or r['source'].get('evidence') != r.get('evidence') for r in g.get('format_rules', [])):
            issues.append('shared_format_source_changed')
    checks.append({'record': 1056, 'issues': issues})
    return {'checks': checks, 'unresolved_contracts': sum(bool(c['issues']) for c in checks),
            'scope': 'four_prompts_thirteen_explicit_clauses_one_shared_group',
            'compound_response_completeness_verified': False, 'whole_form_complete': False}
