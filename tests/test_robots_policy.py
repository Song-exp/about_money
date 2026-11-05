import pytest
from app.services.robots_policy import prefix_robots_allowed


POLICY = 'User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n'


def allowed(text=POLICY, path='/wp-admin/admin-ajax.php'):
    return prefix_robots_allowed(text, 'PayPickMapSync/1.0', 'https://www.paris.co.kr' + path)


def test_specific_public_route_is_allowed_while_admin_stays_blocked():
    assert allowed()
    assert not allowed(path='/wp-admin/')
    assert not allowed(path='/wp-admin/users.php')


def test_rule_order_does_not_override_specificity():
    assert allowed('User-agent: *\nAllow: /wp-admin/admin-ajax.php\nDisallow: /wp-admin/')


def test_equal_path_prefers_allow_and_more_specific_disallow_wins():
    assert allowed(POLICY + 'Disallow: /wp-admin/admin-ajax.php\n')
    assert not allowed(POLICY + 'Disallow: /wp-admin/admin-ajax.php/private',
        '/wp-admin/admin-ajax.php/private')


def test_product_specific_group_overrides_default_and_merges_matching_groups():
    policy = POLICY + '\nUser-agent: PayPickMapSync\nDisallow: /wp-admin/\n'
    assert not allowed(policy)
    policy += '\nUser-agent: PayPickMapSync\nAllow: /wp-admin/admin-ajax.php\n'
    assert allowed(policy)


@pytest.mark.parametrize('pattern', ['/private/*', '/private$', '/%70rivate'])
def test_unsupported_rule_patterns_require_review(pattern):
    with pytest.raises(ValueError, match='requires_review'):
        allowed('User-agent: *\nDisallow: ' + pattern)
