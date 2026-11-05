import { useEffect, useState } from "react";
import { downloadWriting, fetchWritingSession, sendWritingMessage, writingAction, writingHistory } from "../api";
import type { ApplicationField, WritingHistory, WritingSession } from "../types";

const roleLabel = (role?: string) => ({student: "학생 · ", guardian: "보호자 · ", applicant: "신청인 · ", recommender: "추천인 · "}[role ?? ""] ?? "");
const subjectLabel = (field?: ApplicationField) => field?.subject ? `${field.subject.label} · ` : roleLabel(field?.role);

const labels: Record<string, string> = {below_min_length: "최소 분량 부족", above_max_length: "최대 분량 초과", required_content_missing: "필수 내용 없음", unresolved_placeholder: "미완성 표현 있음", unsupported_characters_for_encoding: "사용할 수 없는 문자", original_layout_page_check_required: "원본 페이지 분량 확인 필요", byte_encoding_check_required: "원본 바이트 기준 확인 필요", manual_original_items_required: "사진·첨부·동의·서명은 원본에서 직접 처리", option_not_in_original: "원문 선택지를 확인하세요", missing_original_content_items: "원문에서 요구하는 항목 수 부족", duplicate_original_content_items: "같은 내용의 항목이 중복됨", original_group_format_check_required: "원본의 글꼴·크기·줄 간격 확인 필요", shared_group_requires_review: "문항 묶음의 규칙 검토 필요", shared_group_drafts_missing: "문항 묶음에 빠진 초안 있음", original_content_review_required: "원문에서 요청한 세부 내용이 초안에 반영됐는지 확인 필요"};
labels.original_paragraph_layout_check_required = "원본에 옮긴 뒤 작성 칸의 위치·표시·단위를 확인하세요";
labels.original_repeating_layout_check_required = "원본 표의 작성 행과 열에 맞게 옮겼는지 확인하세요";

export default function WritingChatPage({ sessionId, onBack }: { sessionId: string; onBack: () => void }) {
  const [session, setSession] = useState<WritingSession | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState<{path: string; text: string} | null>(null);
  const [history, setHistory] = useState<WritingHistory[] | null>(null);
  const [quote, setQuote] = useState<{document_id: number; text: string} | null>(null);
  const [historyConfirmed, setHistoryConfirmed] = useState(false);
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const [manualConfirmed, setManualConfirmed] = useState(false);
  const [reuse, setReuse] = useState(false);
  const [selectedOptions, setSelectedOptions] = useState<string[]>([]);
  useEffect(() => { setSelectedOptions([]); }, [session?.current_field?.id, session?.next_action?.slot_key, session?.version]);
  useEffect(() => {
    let active = true;
    setSession(null); setError(""); setEdit(null); setHistory(null); setQuote(null);
    fetchWritingSession(sessionId).then(s => { if (active) setSession(s); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [sessionId]);
  useEffect(() => { setReviewConfirmed(false); setManualConfirmed(false); setHistory(null); setQuote(null); setHistoryConfirmed(false); }, [session?.version]);
  async function run(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError("");
    try { await work(); } catch (e) {
      setError((e as Error).message);
      try { setSession(await fetchWritingSession(sessionId)); } catch { /* 수정 중인 입력 보존 */ }
    } finally { setBusy(false); }
  }
  async function action(path: string, body: Record<string, unknown> = {}, method = "POST") {
    if (session) await run(async () => { setSession(await writingAction(sessionId, path, session.version, body, method)); });
  }
  const enc = encodeURIComponent;
  const enabled = session && !["blocked", "stale"].includes(session.state);
  const warnings = (session?.drafts.some(d => d.validation.warnings.length) ?? false) || (session?.manual_items.length ?? 0) > 0 || (session?.group_validations?.some(g => g.warnings.length) ?? false);
  const invalid = (session?.drafts.some(d => d.validation.errors.length) ?? false) || (session?.group_validations?.some(g => g.errors.length) ?? false);
  return <div className="chat-page page-shell">
    <button type="button" className="text-button" onClick={onBack}>← 장학정보 대화로 돌아가기</button>
    <section className="writing-head">
      <h1>{session?.title ?? "신청서 작성"}</h1><p>공고 자격을 확인하고 실제 양식의 문항별로 본인 정보를 모읍니다.</p>
      {session && <a href={session.source_url} target="_blank" rel="noopener noreferrer">장학 공고 원문 확인 ↗</a>}
    </section>
    {error && <p role="alert" className="notice">{error}</p>}
    {!session && !error && <p className="notice">작성 대화를 불러오는 중입니다.</p>}
    {session && <>
      <section className="chat-panel">
        <header className="chat-panel-head"><div><strong>장학금 신청서 도우미</strong><span>자격 확인 → 정보 수집·확인 → 초안 검토</span></div></header>
        {session.state === "stale" && <p className="notice">공고 또는 양식이 변경되었습니다. 최신 공고를 다시 선택하세요. 이전 입력은 보관됩니다.</p>}
        {session.state === "blocked" && <p className="notice">실제 신청 양식 확인이 완료되면 작성을 시작할 수 있습니다.</p>}
        {session.state === "ineligible" && <p className="notice">현재 답변이 공고 조건과 맞지 않습니다. 잘못 입력한 정보가 있다면 대화에서 정정하세요.</p>}
        <div className="messages" aria-live="polite">
          {session.messages.map((m, i) => <div key={i} className={`row ${m.role}`}><div className={`bubble ${m.role}`}><div className="text">{m.text}</div></div></div>)}
          {busy && <p className="notice">요청을 처리하는 중입니다.</p>}
        </div>
        {enabled && session.next_action?.kind === "applicability" && <div>
          <button disabled={busy} onClick={() => void action(`conditions/${enc(session.next_action!.condition_id!)}`, {applies: true})}>이 작성 조건에 해당합니다</button>
          <button disabled={busy} onClick={() => void action(`conditions/${enc(session.next_action!.condition_id!)}`, {applies: false})}>이 작성 조건에 해당하지 않습니다</button>
        </div>}
        {enabled && session.next_action?.kind === "field" && session.next_action.slot_key === "value" && session.current_field?.choice_spec && <div>
          <p>{session.current_field.selection_mode === "single" ? "본인의 실제 상태를 선택한 뒤 보내세요." : "원본에 실제로 표시할 항목을 모두 선택한 뒤 보내세요."}</p>
          {session.current_field.options?.map(option => <label key={option}>
            <input type={session.current_field?.selection_mode === "single" ? "radio" : "checkbox"} name="original-choice" disabled={busy} checked={selectedOptions.includes(option)} onChange={e => setSelectedOptions(previous => session.current_field?.selection_mode === "single" ? [option] : e.target.checked ? [...previous, option] : previous.filter(value => value !== option))}/>{option}
          </label>)}
          <button disabled={busy || !selectedOptions.length} onClick={() => void run(async () => {
            setSession(await sendWritingMessage(sessionId, JSON.stringify(selectedOptions), session.version)); setInput("");
          })}>선택한 항목 보내기</button>
          {session.current_field.choice_spec.allow_empty && <button disabled={busy} onClick={() => void run(async () => {
            setSession(await sendWritingMessage(sessionId, "표시하지 않음", session.version)); setInput("");
          })}>이 항목들에는 표시하지 않음</button>}
        </div>}
        {enabled && session.next_action?.kind === "field" && session.next_action.slot_key === "value" && session.current_field?.field_type === "checkbox" && !session.current_field.choice_spec && <div>
          {session.current_field.options?.map(option => <button key={option} disabled={busy} onClick={() => void run(async () => {
            setSession(await sendWritingMessage(sessionId, option, session.version)); setInput("");
          })}>{option}</button>)}
          {session.current_field.selection_mode === "single_mark" && <button disabled={busy} onClick={() => void run(async () => {
            setSession(await sendWritingMessage(sessionId, "표시하지 않음", session.version)); setInput("");
          })}>표시하지 않음</button>}
        </div>}
        {enabled && <form className="input-bar" onSubmit={e => {
          e.preventDefault(); if (!input.trim()) return;
          void run(async () => { setSession(await sendWritingMessage(sessionId, input.trim(), session.version)); setInput(""); });
        }}><label className="message-input"><span className="sr-only">신청서에 필요한 본인 정보</span>
          <textarea value={input} onChange={e => setInput(e.target.value)} maxLength={10000} disabled={busy} placeholder={session.next_action ? "본인 정보와 경험을 알려주시거나, 문항에 대해 질문해 주세요." : "초안을 더 간결하게 다듬거나, 작성 내용에 대해 질문해 주세요."} rows={3}/>
        </label><button className="send-button" disabled={busy || !input.trim()}>보내기</button></form>}
        {session.state === "ready" && <button disabled={busy} onClick={() => void action("draft")}>확인한 정보로 초안 구성</button>}
      </section>
      {Object.keys(session.answers).length > 0 && <section className="chat-panel"><h2>신청서에 사용할 정보</h2>
        {Object.entries(session.answers).filter(([id]) => session.applicability_states[id] !== "not_applicable").map(([id, answer]) => <article key={id} className="draft-item"><h3>{subjectLabel(session.fields.find(f => f.id === id))}{answer.label}</h3>
          {answer.status === "skipped" && <p>선택 문항 생략</p>}
          {Object.entries(answer.facts).map(([key, fact]) => <div key={key}>
            <p>{session.requirements[id]?.find(r => r.key === key)?.question}</p><p style={{whiteSpace: "pre-wrap"}}>{fact.text}</p>
            <small>{fact.confirmed ? "사실 확인 완료" : "사실 확인 필요"}{fact.source.kind === "history" ? " · 과거 이력에서 선택" : ""}</small>
            <button disabled={busy || !enabled} onClick={() => setEdit({path: `fields/${enc(id)}/${enc(key)}`, text: fact.text})}>정보 수정</button>
          </div>)}
          {session.next_action?.kind === "confirm" && session.next_action.field_id === id && <button disabled={busy} onClick={() => void action(`fields/${enc(id)}/confirm`, {confirmed: true})}>{session.fields.find(f => f.id === id)?.semantic_kind === "consent" ? "원문 내용을 읽고 직접 선택했습니다" : session.fields.find(f => f.id === id)?.semantic_kind === "submission_checklist" ? "이번 신청의 서류 준비 상태를 확인했습니다" : "이 문항의 정보가 본인 사실과 맞습니다"}</button>}
        </article>)}
      </section>}
      {enabled && session.current_field && session.next_action?.kind === "field" && <section className="chat-panel">
        {session.current_field.required === false && <button disabled={busy} onClick={() => void action(`fields/${enc(session.current_field!.id)}/skip`)}>선택 문항 생략</button>}
        {session.current_field.field_type !== "checkbox" && session.current_field.semantic_kind !== "submission_checklist" && !session.current_field.history_reuse_policy && <button disabled={busy} onClick={() => void run(async () => { setHistory(await writingHistory(sessionId, session.current_field!.id)); })}>재사용에 동의한 내 과거 이력 찾기</button>}
        {session.current_field.history_reuse_policy?.sources.map((source, index) => <p key={index} className="notice">{source.evidence}</p>)}
        {history && history.length === 0 && <p>재사용 가능한 이력이 없습니다. 대화로 본인 내용을 알려주세요.</p>}
        {history?.map(h => <article key={h.document_id} className="draft-item"><h3>{h.scholarship_name} · {h.prompt_question}</h3><p style={{whiteSpace: "pre-wrap"}}>{h.content_text}</p>
          <button disabled={busy} onClick={() => { setQuote({document_id: h.document_id, text: h.content_text}); setHistoryConfirmed(false); }}>사용할 문장 선택</button>
        </article>)}
        {quote && <div><label>이번 질문에 사용할 원문의 정확한 문장<textarea rows={4} value={quote.text} maxLength={10000} onChange={e => { setQuote({...quote, text: e.target.value}); setHistoryConfirmed(false); }}/></label>
          <label><input type="checkbox" checked={historyConfirmed} onChange={e => setHistoryConfirmed(e.target.checked)}/>이번 문항에 맞는 본인의 실제 내용임을 확인했습니다.</label>
          <button disabled={busy || !historyConfirmed || !quote.text.trim()} onClick={() => void action("history", {field_id: session.current_field!.id, slot_key: session.next_action!.slot_key, document_id: quote.document_id, quote: quote.text, confirmed: true})}>확인한 문장 사용</button>
        </div>}
      </section>}
      {edit && <form className="chat-panel" onSubmit={e => { e.preventDefault(); void run(async () => {
        setSession(await writingAction(sessionId, edit.path, session.version, {message: edit.text}, "PUT")); setEdit(null);
      }); }}><label>내용 수정<textarea rows={6} maxLength={10000} value={edit.text} onChange={e => setEdit({...edit, text: e.target.value})}/></label>
        <button disabled={busy || !edit.text.trim()}>수정 저장</button><button type="button" disabled={busy} onClick={() => setEdit(null)}>취소</button>
      </form>}
      {session.drafts.length > 0 && <section className="chat-panel"><h2>문항별 초안</h2><p>확인한 본인 문장을 바탕으로 구성했습니다. 원본 양식에 옮기기 전 내용과 분량을 검토하세요.</p>
        {session.drafts.map(d => <article key={d.field_id} className="draft-item"><h3>{d.label}</h3><p style={{whiteSpace: "pre-wrap"}}>{d.text}</p>
          <small>{d.method === 'llm_grounded_prose' ? 'AI 문장 작성 · 확인된 사실 기반 · 본인 검토 필요' : d.method === 'confirmed_evidence_assembly' ? 'AI 작성 대신 확인한 원문을 조합한 초안입니다.' : '확인한 값과 원문 지침을 반영한 작성 자료입니다.'}</small>
          <p>분량: {d.validation.count ?? "원본 배치에서 확인"} {({characters: "글자", words: "단어", bytes: "바이트"} as Record<string, string>)[d.validation.unit] ?? ""}</p>
          {[...d.validation.errors, ...d.validation.warnings].map(v => <p key={v} className="notice">{labels[v] ?? v}</p>)}
          {!session.fields.find(f => f.id === d.field_id)?.choice_spec && !session.fields.find(f => f.id === d.field_id)?.input_spec && <button disabled={busy || !enabled} onClick={() => setEdit({path: `draft/${enc(d.field_id)}`, text: d.text})}>초안 수정</button>}
          {(session.fields.find(f => f.id === d.field_id)?.choice_spec || session.fields.find(f => f.id === d.field_id)?.input_spec) && <p>선택이나 값을 바꾸려면 수집한 정보를 수정한 뒤 초안을 다시 구성하세요.</p>}
        </article>)}
        {!session.draft_reviewed && enabled && <div>
          <label><input type="checkbox" checked={reviewConfirmed} onChange={e => setReviewConfirmed(e.target.checked)}/>전체 문항의 내용이 사실과 맞는지 검토했습니다.</label>
          {warnings && <label><input type="checkbox" checked={manualConfirmed} onChange={e => setManualConfirmed(e.target.checked)}/>원문이 요청한 세부 내용·전체 분량·서식과 원본에서 직접 작성·첨부·표시할 항목을 확인했습니다.</label>}
          <button disabled={busy || invalid || !reviewConfirmed || (warnings && !manualConfirmed)} onClick={() => void action("review", {confirmed: true, manual_checks_confirmed: manualConfirmed})}>전체 초안 검토 완료</button>
        </div>}
        {session.draft_reviewed && enabled && <div>
          <label><input type="checkbox" checked={reuse} onChange={e => setReuse(e.target.checked)}/>저장한 내용을 다음 신청서 작성에 재사용하도록 허용</label>
          <button disabled={busy} onClick={() => void action("save", {allow_reuse: reuse})}>내 신청서 이력에 저장</button>
          {session.state === "saved" && <p>내 신청서 이력에 저장했습니다.</p>}
          <button disabled={busy} onClick={() => void run(() => downloadWriting(sessionId, "docx"))}>DOCX 작성 자료</button>
          <button disabled={busy} onClick={() => void run(() => downloadWriting(sessionId, "txt"))}>TXT 작성 자료</button>
          <p>다운로드 파일은 문항별 작성 자료입니다. 공고의 원본 신청 양식에 옮기고 서명·첨부 서류를 준비하세요.</p>
        </div>}
      </section>}
      {(session.form_groups?.length ?? 0) > 0 && <section className="chat-panel"><h2>여러 문항에 함께 적용되는 작성 규칙</h2>
        {session.form_groups.map(group => <article key={group.id} className="draft-item"><h3>{group.label}</h3>
          <p style={{whiteSpace: "pre-wrap"}}>{group.evidence}</p>
          <p>적용 문항: {group.field_ids.map(id => session.fields.find(f => f.id === id)?.label).filter(Boolean).join(" / ")}</p>
          {session.group_validations?.filter(g => g.group_id === group.id).map(g => <div key={g.group_id}>
            {g.count !== null && session.drafts.length > 0 && <p>전체 분량: {g.count} {({characters: "글자", words: "단어", bytes: "바이트"} as Record<string, string>)[g.unit] ?? ""}</p>}
            {[...g.errors, ...g.warnings].map(value => <p key={value} className="notice">{labels[value] ?? value}</p>)}
          </div>)}
        </article>)}
      </section>}
      {session.manual_items.length > 0 && <section className="chat-panel"><h2>원본에서 직접 처리할 항목</h2>
        {session.manual_items.map(item => <article key={item.field_id} className="draft-item"><h3>{item.label}</h3>
          <p>{item.instructions}</p>{item.selection && <p>사용자가 확인한 내용: {item.selection}</p>}
          <p>{item.action ?? (item.field_type === "signature" ? "원본에서 직접 서명하세요." : item.field_type === "image" ? "사진을 준비하여 원본에 첨부하세요." : item.field_type === "attachment" ? "파일·서류를 준비하여 직접 첨부하세요." : "동의 내용을 확인하고 원본에서 직접 표시하세요.")}</p>
        </article>)}
      </section>}
      {session.conditions.length > 0 && <section className="chat-panel"><h2>작성 조건 확인</h2>
        {session.conditions.map(condition => <article key={condition.id}><p>{condition.evidence}</p>
          <p>{condition.state === "applicable" ? "작성 대상" : condition.state === "not_applicable" ? "이번 작성에서 제외" : "해당 여부 확인 필요"}</p>
          <button disabled={busy || !enabled} onClick={() => void action(`conditions/${enc(condition.id)}`, {applies: true})}>해당함으로 정정</button>
          <button disabled={busy || !enabled} onClick={() => void action(`conditions/${enc(condition.id)}`, {applies: false})}>해당하지 않음으로 정정</button>
        </article>)}
      </section>}
      <details className="chat-panel"><summary>공고 양식 항목과 이어받은 조건</summary>
        <ul>{session.fields.map(f => <li key={f.id}>{f.section?.label && `${f.section.label} · `}{subjectLabel(f)}{f.label}{f.field_type === "signature" ? " · 직접 서명 필요" : ["image", "attachment"].includes(f.field_type) ? " · 직접 첨부 필요" : ""}{f.required === true ? " · 필수" : ""}<p>{f.instructions || f.evidence}</p>{f.applicability && <p>작성 조건: {f.applicability.evidence}</p>}</li>)}</ul>
        <p>소득분위: {String(session.profile.income_bracket ?? "미확인")} · 학점: {String(session.profile.gpa ?? "미확인")} · 지역: {String(session.profile.region ?? "미확인")}</p>
      </details>
    </>}
  </div>;
}
