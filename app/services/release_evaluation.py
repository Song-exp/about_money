"""Release scoring requires independently reviewed whole-document gold."""
from collections import Counter
from math import sqrt

FIELD_KEYS = ('label', 'role', 'field_type', 'required', 'min_length', 'max_length', 'length_unit', 'includes_spaces')


def wilson(correct, total):
    if not total:
        return None
    p, z = correct / total, 1.959963984540054
    center = p + z * z / (2 * total)
    radius = z * sqrt(p * (1-p) / total + z*z / (4*total*total))
    denominator = 1 + z*z / total
    return [max(0, (center-radius)/denominator), min(1, (center+radius)/denominator)]


def metric(correct, total):
    return {'correct': correct, 'total': total, 'rate': correct/total if total else None, 'wilson_95': wilson(correct, total)}


def signature(field):
    source = field.get('source', {})
    values = [''.join(str(field.get(k, '')).split()) if k == 'label' else field.get(k) for k in FIELD_KEYS]
    structures = {k: field.get(k) for k in ('input_spec', 'choice_spec', 'manual_spec', 'applicability', 'original_destination', 'subject', 'content_requirements')}
    return (source.get('sha256'), source.get('location'), *values,
            tuple(sorted(field.get('options', []))), field.get('instructions', ''), json_signature(structures))


def json_signature(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def evaluate_release(cases, target_notices=50, target_forms=20, threshold=.99):
    blockers, valid = [], []
    ids = [c['id'] for c in cases]
    if len(ids) != len(set(ids)):
        blockers.append('duplicate_notice_ids')
    for c in cases:
        gold = c.get('gold') or {}
        if not (gold.get('reviewed_by') and gold.get('created_by') and gold['reviewed_by'] != gold['created_by']
                and gold.get('whole_document_reviewed') is True and gold.get('source_hashes')
                and gold.get('source_hashes') == c.get('source_hashes')
                and all(isinstance(gold.get(k), list) for k in ('fields', 'facts', 'schedules'))):
            blockers.append(f"independent_gold_missing_or_invalid:{c['id']}")
        else:
            valid.append(c)
    forms = {s for c in valid for s in c['gold'].get('form_source_hashes', []) if s in c['source_hashes']}
    if len(cases) < target_notices:
        blockers.append('notice_sample_too_small')
    if len(forms) < target_forms:
        blockers.append('form_sample_too_small')
    # Unreviewed cases remain in sample counts and prevent release. They do not
    # receive fabricated zero or perfect semantic scores.
    metrics = None
    if len(valid) == len(cases) and cases and len(ids) == len(set(ids)):
        tp = predicted = expected = whole = fetched = 0
        semantic = {k: [0, 0, 0] for k in ('facts', 'schedules')}
        for c in cases:
            truth = Counter(signature(f) for f in c['gold']['fields'])
            actual = Counter(signature(f) for f in c.get('prediction', {}).get('fields', []))
            tp += sum((truth & actual).values())
            predicted += sum(actual.values())
            expected += sum(truth.values())
            acquired = c.get('source_acquisition_complete', c.get('fetch_complete')) is True
            success = acquired and c.get('prediction_complete') is True
            fetched += acquired
            whole_exact = truth == actual
            for key in semantic:
                a = Counter(json_signature(v) for v in c.get('prediction', {}).get(key, []))
                g = Counter(json_signature(v) for v in c['gold'][key])
                semantic[key][0] += sum((a & g).values())
                semantic[key][1] += sum(a.values())
                semantic[key][2] += sum(g.values())
                whole_exact = whole_exact and a == g
            whole += success and whole_exact
        metrics = {'notice_retrieval': metric(fetched, len(cases)), 'field_precision': metric(tp, predicted),
                   'field_recall': metric(tp, expected), 'whole_form_match': metric(whole, len(cases))}
        for key, (correct, predicted_count, expected_count) in semantic.items():
            if predicted_count or expected_count:
                metrics[key + '_precision'] = metric(correct, predicted_count)
                metrics[key + '_recall'] = metric(correct, expected_count)
        for name, value in metrics.items():
            if value['rate'] is None or value['rate'] < threshold:
                blockers.append('below_target:' + name)
    else:
        blockers.append('semantic_accuracy_not_measured')
    return {'release_ready': not blockers, 'blockers': blockers, 'notices': len(cases), 'independently_reviewed_notices': len(valid),
            'independently_reviewed_forms': len(forms), 'targets': {'notices': target_notices, 'forms': target_forms, 'rate': threshold},
            'metrics': metrics, 'scope': 'whole_document_gold_required_no_test_pass_rate_substitution'}
