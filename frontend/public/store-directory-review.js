const $ = id => document.getElementById(id);
let busy = false, offset = 0, items = [], selected = null, delta = null;
let checks = [], preview = null, targetOffset = 0, targetsMore = false;
const reasons = {
  kakao_rest_api_key_required: '서버용 KAKAO_REST_API_KEY 설정이 필요합니다.',
  no_unique_exact_place: '이름·주소가 정확히 일치하는 장소가 하나로 확인되지 않았습니다.',
  official_place_coordinates_disagree: '공식 자료와 카카오 좌표가 달라 보류되었습니다.',
  store_directory_review_stale: '공식 자료가 변경되었습니다. 변경 목록을 새로 불러오세요.',
  store_directory_target_version_mismatch: '매장 정보가 바뀌었습니다. 미리보기를 다시 확인하세요.',
  store_directory_official_binding_required: '이 매장과 공식 지점의 연결 근거가 부족합니다.',
  store_directory_place_check_expired: '장소 검증이 24시간을 넘겼습니다. 다시 조회하세요.',
  store_directory_place_check_not_current: '최신 검증 통과 결과가 필요합니다.',
  place_already_bound: '다른 매장에 이미 연결된 카카오 장소입니다.',
  place_lookup_failed: '카카오 장소 조회에 실패했습니다.',
};
const message = text => { $('message').textContent = text; };
const json = value => JSON.stringify(value, null, 2);
function clearPreview() {
  preview = null;
  $('preview-panel').hidden = true;
  $('confirm').checked = false;
  $('before').textContent = $('after').textContent = '';
}
function latestCheck() { return checks.find(row => row.official_id === delta?.official_id); }
function controls() {
  for (const button of document.querySelectorAll('button')) button.disabled = busy;
  for (const input of document.querySelectorAll('input,select,textarea')) input.disabled = busy;
  $('previous').disabled = busy || offset === 0;
  $('next').disabled = busy || items.length < 50;
  $('acknowledge').disabled = busy || !selected?.reviewable || !$('review-note').value.trim();
  $('check').disabled = busy || !selected?.current || !selected?.review || !delta?.after;
  $('history').disabled = busy || !delta;
  $('more-targets').disabled = busy || !targetsMore;
  $('preview').disabled = busy || !selected?.current || !selected?.review || latestCheck()?.status !== 'matched' || !$('target').value;
  $('apply').disabled = busy || !preview || !$('confirm').checked || !$('reviewer').value.trim() || !$('apply-note').value.trim();
}
async function request(path, body) {
  const response = await fetch($('base').value.replace(/\/$/, '') + path, {
    headers: {'X-Admin-Token': $('token').value, 'Content-Type': 'application/json'},
    ...(body ? {method: 'POST', body: JSON.stringify(body)} : {}),
  });
  const data = await response.json();
  if (!response.ok) {
    const reason = typeof data.detail === 'string' ? data.detail : json(data.detail);
    throw new Error(reasons[reason] || reason || `HTTP ${response.status}`);
  }
  return data;
}
async function run(task) {
  if (busy) return;
  busy = true; controls(); message('처리 중…');
  try { message(await task() || '완료'); }
  catch (error) { clearPreview(); message(error.message); }
  finally { busy = false; controls(); }
}
function resetSelection() {
  selected = delta = null; checks = []; targetsMore = false; clearPreview();
  $('detail').hidden = true;
  $('review-note').value = '';
}
async function loadQueue() {
  resetSelection();
  items = []; $('queue').replaceChildren();
  const data = await request(`/admin/map/store-directories/changes?limit=50&offset=${offset}`);
  items = data.items;
  $('queue').replaceChildren();
  $('page-status').textContent = items.length ? `${offset + 1}–${offset + items.length}번째 변경` : '변경 항목이 없습니다.';
  for (const row of items) {
    const li = document.createElement('li'), button = document.createElement('button');
    button.textContent = `#${row.id} ${row.provider} · ${row.source_key} · ${row.current ? '현재 버전' : '과거 버전'} · ${row.review ? '검토 완료' : '검토 대기'}`;
    button.onclick = () => run(() => selectChange(row)); li.append(button); $('queue').append(li);
  }
}
async function loadTargets(append = false) {
  if (!append) { targetOffset = 0; $('target').replaceChildren(new Option('매장을 선택하세요', '')); }
  const data = await request(`/admin/map/store-directories/targets?provider=${encodeURIComponent(selected.provider)}&limit=50&offset=${targetOffset}`);
  for (const row of data.items) $('target').add(new Option(`#${row.id} ${row.brand} ${row.branch} · ${row.address || '주소 없음'}`, row.id));
  targetOffset += data.items.length; targetsMore = data.has_more;
}
async function selectChange(row) {
  resetSelection(); selected = row;
  $('detail').hidden = false;
  $('selected-label').textContent = `변경 #${row.id} · ${row.source_key} · ${row.observed_at}`;
  $('review-state').textContent = `${row.current ? '현재 공식 버전' : '과거 버전: 새 변경을 선택하세요.'} / ${row.review ? `검토 완료: ${row.review.note}` : '검토 대기'}`;
  $('deltas').replaceChildren(); $('checks').textContent = ''; $('check-state').textContent = '';
  $('selected-identity').textContent = '재검증할 지점을 선택하세요.';
  for (const entry of row.changes) {
    const block = document.createElement('div'), button = document.createElement('button'), evidence = document.createElement('pre');
    button.textContent = `공식 ID ${entry.official_id} · ${entry.kind} · ${entry.after?.official_name || entry.before?.official_name || ''}`;
    evidence.textContent = json({before: entry.before, after: entry.after, fields: entry.fields});
    button.onclick = () => run(async () => {
      delta = entry; clearPreview();
      $('selected-identity').textContent = `선택: 공식 ID ${entry.official_id}${entry.after ? '' : ' · 목록 누락은 폐점 근거가 아니며 재검증할 수 없습니다.'}`;
      await loadHistory();
    });
    block.append(button, evidence); $('deltas').append(block);
  }
  await loadTargets();
}
async function loadHistory() {
  clearPreview();
  const data = await request(`/admin/map/store-directories/changes/${selected.id}/place-checks?limit=100`);
  checks = data.items;
  const current = latestCheck();
  $('check-state').textContent = !current ? '이 지점의 조회 기록이 없습니다.' : current.status === 'matched'
    ? `장소 대조 통과 · 검증 #${current.check_id} · ${current.at}` : `보류: ${reasons[current.reason] || current.reason}`;
  $('checks').textContent = json(checks.filter(row => row.official_id === delta?.official_id));
}
$('reload').onclick = () => run(async () => { offset = 0; await loadQueue(); });
$('previous').onclick = () => run(async () => { offset = Math.max(0, offset - 50); await loadQueue(); });
$('next').onclick = () => run(async () => { offset += 50; await loadQueue(); });
$('sources').onclick = () => run(async () => {
  const [directories, placeRevalidation] = await Promise.all([
    request('/admin/map/store-directories'), request('/admin/map/store-directories/place-revalidation')]);
  $('source-status').textContent = json({directories, placeRevalidation}); $('source-status').hidden = false;
});
$('acknowledge').onclick = () => run(async () => {
  const id = selected.id;
  await request(`/admin/map/store-directories/changes/${id}/review`, {expected_identity_hash: selected.after_identity_hash, note: $('review-note').value.trim()});
  await loadQueue();
  const row = items.find(row => row.id === id);
  if (row) await selectChange(row);
  return '공식 변경 검토를 기록했습니다. 재검증할 지점을 선택하세요.';
});
$('check').onclick = () => run(async () => {
  clearPreview();
  const result = await request(`/admin/map/store-directories/changes/${selected.id}/place-check`, {expected_identity_hash: selected.after_identity_hash, official_id: delta.official_id});
  await loadHistory();
  return result.status === 'matched' ? '장소 대조를 통과했습니다. 기존 매장을 선택해 미리보기를 확인하세요.' : `보류: ${reasons[result.reason] || result.reason}`;
});
$('history').onclick = () => run(loadHistory);
$('more-targets').onclick = () => run(() => loadTargets(true));
$('target').onchange = () => { clearPreview(); controls(); };
$('preview').onclick = () => run(async () => {
  clearPreview();
  const query = new URLSearchParams({check_id: latestCheck().check_id, store_id: $('target').value, expected_identity_hash: selected.after_identity_hash});
  preview = await request(`/admin/map/store-directories/changes/${selected.id}/publication-preview?${query}`);
  $('before').textContent = json(preview.before); $('after').textContent = json(preview.after);
  $('preview-panel').hidden = false;
  return '변경 전후를 확인한 뒤 검토자와 반영 메모를 작성하세요.';
});
$('apply').onclick = () => run(async () => {
  const body = {check_id: preview.check_id, store_id: preview.store_id, expected_identity_hash: preview.identity_hash,
    expected_store_version: preview.expected_store_version, reviewer: $('reviewer').value.trim(), note: $('apply-note').value.trim()};
  const result = await request(`/admin/map/store-directories/changes/${selected.id}/apply`, body);
  clearPreview();
  return `지도 반영 ${result.status === 'already_applied' ? '이전 처리 확인' : '완료'} · 매장 #${result.store_id} · 이력 #${result.event_id}. 할인 혜택은 별도 검토가 필요합니다.`;
});
for (const id of ['review-note', 'reviewer', 'apply-note', 'confirm']) $(id).addEventListener('input', controls);
for (const id of ['base', 'token']) $(id).addEventListener('input', () => {
  resetSelection(); items = []; $('queue').replaceChildren(); $('source-status').hidden = true; controls();
});
controls();
