"""Specificity matching for sources using simple ASCII robots path prefixes.

This deliberately rejects wildcard/end-anchor/encoded rule patterns rather than
pretending to be a complete RFC 9309 implementation.
"""
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


def prefix_robots_allowed(text, user_agent, url):
    for line in text.splitlines():
        key, separator, value = line.split('#', 1)[0].partition(':')
        if separator and key.strip().lower() in ('allow', 'disallow') and '%' in value:
            raise ValueError('unsupported_robots_rule_requires_review')
    policy = RobotFileParser()
    policy.parse(text.splitlines())
    groups = [entry for entry in policy.entries if entry.applies_to(user_agent)]
    if not groups and policy.default_entry:
        groups = [policy.default_entry]
    rules = [rule for entry in groups for rule in entry.rulelines]
    for rule in rules:
        if any(token in rule.path for token in ('*', '$', '%')) or not rule.path.isascii():
            raise ValueError('unsupported_robots_rule_requires_review')
    parsed = urlparse(url)
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    matches = [rule for rule in rules if rule.applies_to(path)]
    if not matches:
        return True
    most_specific = max(len(rule.path) for rule in matches)
    return any(rule.allowance for rule in matches if len(rule.path) == most_specific)
