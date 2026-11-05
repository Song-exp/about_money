"""Compatibility entry points; all interpretation now uses one typed turn request."""
from app.services.conversation_intent import FIELDS, effective_profile, interpret_turn, apply_request


def apply_search_changes(sess, text, updated=None):
    return apply_request(sess, interpret_turn(sess, text))
