from types import SimpleNamespace
from app.scripts.resolve_map_locations import expected_identity, exact_matches
from app.scripts.resolve_map_locations import query_place_candidates


def store(address='휘경로 10', branch='외대앞점'):
    return SimpleNamespace(brand='GS25', branch=branch, address=address)


def candidate(name='GS25 외대앞점', address='서울 동대문구 휘경로 10', id='1'):
    return {'place_name': name, 'road_address_name': address, 'address_name': address, 'id': id}


def test_short_road_requires_explicit_region():
    assert expected_identity(store(), 'store', None)[1] is None
    assert expected_identity(store(), 'store', '서울 동대문구')[1] == '서울 동대문구 휘경로 10'


def test_campus_labels_and_ambiguous_addresses_cannot_match():
    for address in ('청운관', '회기로 정문 앞', '휘경로 10 뒤편', None):
        name, qualified, _ = expected_identity(store(address), 'store', '서울 동대문구')
        assert qualified is None
        assert exact_matches([candidate()], name, qualified) == []


def test_same_brand_other_branch_building_or_district_rejected():
    name, address, _ = expected_identity(store(), 'store', '서울 동대문구')
    documents = [candidate(), candidate(name='GS25 외대역점'),
                 candidate(address='서울 동대문구 휘경로 10-1'),
                 candidate(address='서울 중랑구 휘경로 10')]
    assert exact_matches(documents, name, address) == [documents[0]]


def test_duplicate_exact_matches_remain_ambiguous():
    name, address, _ = expected_identity(store(), 'store', '서울 동대문구')
    assert len(exact_matches([candidate(id='1'), candidate(id='2')], name, address)) == 2


class LookupPage:
    def __init__(self,responses):
        self.responses=iter(responses)
        self.queries=[]
    def wait_for_timeout(self,milliseconds):
        assert milliseconds==2000
    def evaluate(self,script,keyword):
        self.queries.append(keyword)
        return next(self.responses)


def test_zero_results_uses_distinct_name_fallback():
    page=LookupPage([{'status':'ZERO_RESULT','documents':[]},{'status':'OK','documents':[candidate()]}])
    rows,status,attempts=query_place_candidates(page,'GS25 외대앞점','GS25 외대앞점 서울 동대문구')
    assert status=='OK' and len(rows)==1
    assert page.queries==['GS25 외대앞점 서울 동대문구','GS25 외대앞점'] and len(attempts)==2


def test_error_or_timeout_is_not_a_successful_empty_lookup():
    for status in ('ERROR','TIMEOUT'):
        page=LookupPage([{'status':status,'documents':[]}])
        _,actual,attempts=query_place_candidates(page,'CU 경희대점','CU 경희대점 서울 동대문구')
        assert actual==status and len(attempts)==1


def test_identical_keyword_is_not_requested_twice():
    page=LookupPage([{'status':'ZERO_RESULT','documents':[]}])
    query_place_candidates(page,'CU 경희대점','CU 경희대점')
    assert page.queries==['CU 경희대점']
