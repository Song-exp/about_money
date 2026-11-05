"""Synthetic nonempty rows use the schema observed from the official UI."""
import json
from types import SimpleNamespace
import pytest
from app.services.issuer_merchant_matching import parse_shinhan_directory, merchant_match


def response(rows=None, more='N'):
    rows = rows if rows is not None else [('00001','스타벅스 경희대점','서울 동대문구 경희대로 16-1','커피전문점')]
    keys = ('MCT_N','MCT_NM','MCT_AR','MCT_RY_NM')
    return {'mbw_result':'S','mbw_json':{'isNext':more,'nxtQyKey':'next' if more=='Y' else '',
        'mchtList':{key:[row[i] for row in rows] for i,key in enumerate(keys)}}}


def parse(data):
    return parse_shinhan_directory(json.dumps(data,ensure_ascii=False).encode())


def store(**changes):
    return SimpleNamespace(**(dict(id=1,brand='스타벅스',branch='경희대점',
        address='서울특별시 동대문구 경희대로 16-1 (회기동)',is_active=True)|changes))


def test_unique_exact_identity_is_only_a_review_candidate():
    parsed = parse(response())
    result = merchant_match(store(),parsed)
    assert result['status']=='unique_exact_match'
    assert result['candidates'][0]['issuer_category']=='커피전문점'
    assert result['review_required']
    assert not result['issuer_classification_confirmed'] and not result['public_approval']


@pytest.mark.parametrize('changes', [dict(branch='경희대정문점'),dict(address='서울 동대문구 경희대로 16-2'),
    dict(address='서울 중랑구 경희대로 16-1'),dict(address='서울 동대문구 경희대로 16-1 2층'),
    dict(brand='다른카페')])
def test_similar_names_roads_and_different_floors_cannot_match(changes):
    result = merchant_match(store(**changes),parse(response()))
    assert result['status']=='pending' and result['reason']=='no_exact_name_and_address_match'


def test_paging_and_ambiguous_merchant_ids_are_not_unique():
    assert merchant_match(store(),parse(response(more='Y')))['reason']=='directory_pagination_incomplete'
    rows = [('00001','스타벅스 경희대점','서울 동대문구 경희대로 16-1','커피전문점'),
            ('00002','스타벅스 경희대점','서울 동대문구 경희대로 16-1','커피전문점')]
    assert merchant_match(store(),parse(response(rows)))['reason']=='ambiguous_exact_match'
    assert merchant_match(store(),parse(response([])))['reason']=='no_exact_name_and_address_match'


def test_inactive_and_incomplete_store_addresses_stay_pending():
    parsed = parse(response())
    assert merchant_match(store(is_active=False),parsed)['reason']=='inactive_store'
    assert merchant_match(store(address='경희대로 16-1'),parsed)['reason']=='store_address_not_specific'


@pytest.mark.parametrize('change', ['column_missing','column_length','bad_result','point_response','bad_paging',
    'empty_category','duplicate_conflict','non_list_column','non_string_value'])
def test_invalid_or_different_endpoint_schema_cannot_be_used_as_classification(change):
    data = response()
    columns = data['mbw_json']['mchtList']
    if change=='column_missing': del columns['MCT_RY_NM']
    if change=='column_length': columns['MCT_AR']=[]
    if change=='bad_result': data['mbw_result']='F'
    if change=='point_response': data['mbw_json']['mchtList']={'joinname':['스타벅스'],'dname':['커피']}
    if change=='bad_paging': data['mbw_json']['nxtQyKey']='leftover'
    if change=='empty_category': columns['MCT_RY_NM']=['']
    if change=='duplicate_conflict':
        for key in columns: columns[key].append(columns[key][0])
        columns['MCT_RY_NM'][1]='편의점'
    if change=='non_list_column': columns['ETK_N']='text'
    if change=='non_string_value': columns['MCT_N']=[123]
    with pytest.raises(ValueError): parse(data)


def test_business_registration_numbers_are_not_in_normalized_output():
    data = response()
    data['mbw_json']['mchtList']['ETK_N']=['synthetic-business-number']
    result = parse(data)
    assert 'synthetic-business-number' not in json.dumps(result)


def test_identical_repeated_rows_deduplicate_but_different_ids_do_not():
    row = ('00001','스타벅스 경희대점','서울 동대문구 경희대로 16-1','커피전문점')
    assert len(parse(response([row,row]))['records'])==1


def test_other_issuer_or_point_scope_cannot_supply_card_classification():
    parsed = parse(response())
    assert merchant_match(store(),parsed|{'issuer_key':'other'})['reason']=='issuer_directory_identity_unresolved'
    assert merchant_match(store(),parsed|{'scope':'point_merchants'})['reason']=='issuer_directory_identity_unresolved'


def test_partial_page_requires_continuation_key():
    data = response(more='Y')
    data['mbw_json']['nxtQyKey']=''
    with pytest.raises(ValueError,match='issuer_directory_paging_conflict'): parse(data)
