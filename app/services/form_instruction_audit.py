"""같은 문서의 입증된 적용 범위 안에서 다른 원문 지시를 보존한다."""
import re

from app.services.document_layout import compact
from app.services.form_semantics import block_source
from app.services.form_submission_checklist import native_submission_checklists

VERSION = 'native-instruction-audit-v1'


def audit_instructions(blocks, groups, source):
    checks = []
    native = [b for b in blocks if b.get('method') == 'native']
    for group in groups:
        if '자기소개서' not in compact(group.get('label', '')):
            continue
        limit = re.search(r'총(\d+)장이내', compact(group.get('evidence', '')))
        proof = group.get('source') or {}
        rule = next((b for b in native if b['location'] == proof.get('location') and
                     b['start'] == proof.get('text_start') and b['end'] == proof.get('text_end') and
                     b['text'] == proof.get('evidence') and proof.get('sha256') == (source or {}).get('sha256')), None)
        if not limit or rule is None:
            continue
        covers = [b for b in native if re.search(r'(?:자소서|자기소개서)(?:는|의)?(?:A4)?\d+장내외', compact(b['text']))
                  and b['location'] != rule['location']]
        for cover in covers:
            checks.append({'kind':'essay_page_rule_difference', 'sources':[block_source(cover,source),block_source(rule,source)],
                           'field_ids':group['field_ids'], 'group_id':group['id'],
                           'status':'requires_original_review', 'issues':['essay_about_and_within_page_rules_require_review'],
                           'automatic_resolution':False})
    # 다른 표의 택1을 빌려오지 않는다. 지원되는 체크리스트의 인접 지시로만 연결한다.
    for checklist in native_submission_checklists(blocks,source):
        fields = checklist['fields']
        economic = [f for f in fields if f['preparation_spec']['economic_rule_review_required']]
        proofs = [p for f in economic for p in f['instruction_sources']]
        single = {p['location']:p for p in proofs if compact(p['evidence']) == '택1'}
        multiple = {p['location']:p for p in proofs if compact(p['evidence']).startswith('※가정경제형편을증명하는서류인5번')
                    and '1가지이상제출' in compact(p['evidence'])}
        if len(single) == len(multiple) == 1:
            checks.append({'kind':'economic_document_rule_difference', 'sources':[*single.values(),*multiple.values()],
                           'field_ids':[f['id'] for f in economic], 'table':fields[0]['layout']['table'],
                           'status':'requires_original_review', 'issues':['economic_one_and_at_least_one_rules_require_review'],
                           'automatic_resolution':False})
    return {'version':VERSION,'controls':checks,'unresolved':len(checks),
            'scope':'supported_self_introduction_and_economic_checklist_instruction_differences_not_all_rules',
            'independent_human_review_complete':False}
