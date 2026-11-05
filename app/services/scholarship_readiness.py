"""공고의 모집 상태와 양식 검토 상태를 독립적으로 판단한다."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.models.scholarship import PostingStatus

VALIDATION_VERSION = "evidence-v1"


def form_fingerprint(scholarship):
    eligibility = scholarship.eligibility or {}
    payload = {"content_hash": scholarship.content_hash,
               "fields": eligibility.get("application_fields", []),
               "coverage": eligibility.get("form_coverage", []),
               "extractor_version": eligibility.get("extractor_version")}
    if eligibility.get("form_groups"):
        payload["form_groups"] = eligibility["form_groups"]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def readiness(scholarship):
    eligibility = scholarship.eligibility or {}
    reasons = []
    active = scholarship.status in (PostingStatus.OPEN, PostingStatus.CLOSING_SOON)
    if not active:
        reasons.append("posting_not_open")
    deadline = scholarship.deadline_at
    if deadline and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone(timedelta(hours=9)))
    if deadline and deadline < datetime.now(timezone.utc):
        active = False
        reasons.append("deadline_passed")
    from app.models.scholarship import Category
    if getattr(scholarship, 'category', None) == Category.GOV_BENEFIT or getattr(scholarship, 'source_platform', None) == 'onjungchoungnyeon':
        from app.services.policy_validation import is_verified
        checked = is_verified(scholarship)
        review = eligibility.get('policy_validation') or {}
        if not checked:
            reasons.append('policy_validation_pending' if review.get('status') == 'pending' else 'policy_validation_requires_review')
        return {'recommendation_ready': active and checked, 'writing_ready': False,
                'facts_status': 'api_text_checked' if checked else 'legacy_unverified',
                'form_status': 'not_applicable', 'reasons': reasons,
                'validation_status': review.get('status', 'unverified'), 'validation_scope': 'official_api_text'}
    issues = eligibility.get("extraction_issues", [])
    recommendation_ready = active and not issues
    checked = False
    document_validation = eligibility.get('document_validation')
    if eligibility.get('document_validation_required') or document_validation:
        from app.services.posting_validation import VERSION, candidate_fingerprint
        checked = bool(document_validation and document_validation.get('version') == VERSION and
                       document_validation.get('status') == 'passed' and not document_validation.get('issues') and
                       document_validation.get('requirements') and
                       document_validation.get('candidate_fingerprint') == candidate_fingerprint(scholarship) and
                       document_validation.get('content_hash') == scholarship.content_hash)
        recommendation_ready = active and checked
        if not checked:
            reasons.append('document_validation_requires_review')
    if issues:
        reasons.append("form_extraction_requires_review" if checked else "extraction_requires_review")
    fields = eligibility.get("application_fields", [])
    from app.services.form_structure import group_conflicts
    conflicts = any(f.get("attribute_conflicts") or f.get("merge_conflicts") or f.get("constraint_issues") or
                    f.get("validation_status") in ("conflict_requires_review", "constraints_require_review") for f in fields)
    conflicts = conflicts or group_conflicts(eligibility.get("form_groups", []), fields)
    conflicts = conflicts or any((audit := c.get(kind) or {}).get("unresolved", 0) or
                                 any(control.get("issues") or control.get("status") != "bound"
                                     for control in audit.get("controls", []))
                                 for c in eligibility.get("form_coverage", []) for kind in ("native_control_audit", "native_profile_audit", "native_family_audit", "native_care_support_audit", "native_external_history_audit", "native_reference_audit", "native_basic_input_audit", "native_grade_care_audit", "native_final_destination_audit", "native_submission_checklist_audit", "native_checklist_value_audit", "native_grid_condition_audit", "native_instruction_audit", "native_spreadsheet_repeat_audit", "native_spreadsheet_education_audit", "native_spreadsheet_recommendation_audit"))
    conflicts = conflicts or any(d.get("reason") in ("repeated_table_header_requires_review", "consent_control_requires_original_review", "repeated_table_reference_requires_review")
                                 for d in eligibility.get("form_exclusions", []))
    if conflicts:
        reasons.append("application_form_conflicts")
    review = eligibility.get("form_review") or {}
    grounded = bool(fields) and all(f.get("source", {}).get("sha256") and f.get("evidence") for f in fields)
    verified = (grounded and not conflicts and eligibility.get("validation_version") == VALIDATION_VERSION and
                review.get("fingerprint") == form_fingerprint(scholarship))
    if not fields:
        reasons.append("application_form_missing")
    elif not verified:
        reasons.append("application_form_unverified")
    return {"recommendation_ready": recommendation_ready,
            "writing_ready": recommendation_ready and verified and not issues,
            "facts_status": "evidence_checked" if checked or eligibility.get("validation_version") == VALIDATION_VERSION else "legacy_unverified",
            "form_status": "reviewed" if verified else "extracted_unverified" if fields else "missing",
            "reasons": reasons}
