"""Allowlisted, code-owned search tools. Model output never executes SQL or writes data."""
from app.models.scholarship import Scholarship
from app.services.scholarship_readiness import readiness
from app.services.consultation_scope import in_scope


def execute(db, sess, request):
    from app.services.information_chat import candidates, candidate_detail, validate_selection
    name = request.tool.name
    if sess.consultation == 'youth_policy' and name == 'select':
        name = 'detail'
    selection = None
    if name == 'search':
        found = candidates(db, sess)
    else:
        ids = request.tool.ids if name in ('detail', 'compare', 'select') else sess.candidates
        if name in ('detail', 'compare', 'select') and not set(ids).issubset(set(sess.candidates)):
            raise ValueError('unknown tool candidate')
        if name == 'select':
            validate_selection(db, sess.session_id, sess.user_id, ids[0])
            selection = {'scholarship_id': ids[0]}
        found = []
        for row_id in ids:
            row = db.get(Scholarship, row_id)
            if row is not None and in_scope(row, sess.consultation) and readiness(row)['recommendation_ready']:
                found.append(candidate_detail(row, sess, sess.candidates.index(row_id) + 1,
                                               include_body=name in ('detail', 'compare')))
    sess.last_tool_result = {'tool': name, 'requested_ids': request.tool.ids,
                            'returned_ids': [row['id'] for row in found], 'returned_count': len(found),
                            'scope': 'collected_database', 'executed': True}
    return found, selection
