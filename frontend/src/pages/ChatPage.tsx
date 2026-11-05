import { useEffect, useRef, useState } from "react";
import { sendMessage, uploadChatFile, selectScholarship, resumeInformationConversation, newInformationConversation } from "../api";
import WritingChatPage from "./WritingChatPage";
import type { Candidate, Draft, ChatResponse, ConsultationType } from "../types";

interface Msg {
  role: "user" | "bot";
  text: string;
  candidates?: Candidate[];
  draft?: Draft | null;
  file?: string;
  answerSource?: "llm" | "fallback" | "system";
  fallbackReason?: string | null;
  search?: ChatResponse['search'];
  citations?: number[];
}

const GREETING =
  "어떤 장학금을 찾으세요? 학점·소득분위·거주 지역을 알려주시면 맞춤 검색을 할 수 있어요. ‘모집 중인 생활비 장학금 10개 보여줘’처럼 전체 공고를 둘러볼 수도 있습니다. 검색 범위는 서비스에 등록된 공고 기준이에요.";

const ACCEPT = ".pdf,.docx,.hwpx,.hwp,.txt";

export default function ChatPage() {
  const [consultation, setConsultation] = useState<ConsultationType>(() => new URLSearchParams(window.location.search).get('consultation') === 'youth_policy' ? 'youth_policy' : 'scholarship');
  const [busy, setBusy] = useState(true);
  function switchConsultation(next: ConsultationType) {
    if (busy || next === consultation) return;
    const url = new URL(window.location.href);
    url.searchParams.set('consultation', next); url.searchParams.delete('writing_session');
    window.history.replaceState(window.history.state, '', url);
    setBusy(true); setConsultation(next);
  }
  return <>
    <div className="consultation-switcher page-shell">
      <div className="consultation-tabs" role="tablist" aria-label="상담 종류">
        <button role="tab" type="button" aria-label="장학금 상담" aria-selected={consultation === 'scholarship'} disabled={busy} onClick={() => switchConsultation('scholarship')}>
          <span className="consultation-tab-icon" aria-hidden="true">▤</span><span><strong>장학금 상담</strong><small>장학 공고 탐색 · 신청서 작성</small></span>
        </button>
        <button role="tab" type="button" aria-label="청년정책 상담" aria-selected={consultation === 'youth_policy'} disabled={busy} onClick={() => switchConsultation('youth_policy')}>
          <span className="consultation-tab-icon" aria-hidden="true">◎</span><span><strong>청년정책 상담</strong><small>지원 대상 · 신청 방법 확인</small></span>
        </button>
      </div>
    </div>
    <ConsultationChat key={consultation} consultation={consultation} onBusyChange={setBusy}/>
  </>;
}

function ConsultationChat({consultation, onBusyChange}: {consultation: ConsultationType; onBusyChange: (busy: boolean) => void}) {
  const policy = consultation === 'youth_policy';
  const greeting = policy ? '주거·취업·교육·생활 지원 등 청년정책을 함께 찾아볼게요. 거주 지역과 필요한 지원을 알려주세요. 정책의 지원 대상과 신청 방법을 안내하며, 현재 접수 여부는 공식 공고에서 확인해야 합니다.' : GREETING;
  const [writingId, setWritingId] = useState<string | null>(() => policy ? null : new URLSearchParams(window.location.search).get("writing_session"));
  const [messages, setMessages] = useState<Msg[]>([
    { role: "bot", text: greeting },
  ]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [restoreError, setRestoreError] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, loading, uploading]);

  const busy = loading || uploading || restoring;
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);

  useEffect(() => {
    let active = true;
    resumeInformationConversation(consultation).then(async saved => {
      if (active && policy && (!saved || !saved.messages.length)) saved = await newInformationConversation(consultation);
      if (!active || !saved) return;
      setSessionId(saved.session_id);
      if (saved.messages.length) setMessages(saved.messages.map(m => ({...m,
        answerSource: m.answer_source, fallbackReason: m.fallback_reason})));
    }).catch(error => { if (active) setRestoreError(error.message); })
      .finally(() => { if (active) setRestoring(false); });
    return () => { active = false; };
  }, []);

  async function startNewConversation() {
    if (busy) return;
    setLoading(true);
    try {
      const saved = await newInformationConversation(consultation);
      setSessionId(saved.session_id);
      setMessages(saved.messages.length ? saved.messages.map(m => ({...m, answerSource: m.answer_source, fallbackReason: m.fallback_reason})) : [{role: 'bot', text: greeting}]); setInput(''); setRestoreError('');
    } catch(error) { setRestoreError((error as Error).message); }
    finally { setLoading(false); }
  }

  useEffect(() => {
    const pop = () => setWritingId(policy ? null : new URLSearchParams(window.location.search).get("writing_session"));
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);

  async function choose(scholarshipId: number, informationId = sessionId) {
    if (policy) return;
    if (!informationId) return;
    const session = await selectScholarship(informationId, scholarshipId);
    const url = new URL(window.location.href); url.searchParams.set("writing_session", session.id);
    window.history.pushState({...window.history.state, app: true, tab: "chat", writingFromInformation: true}, "", url);
    setWritingId(session.id);
  }

  function backToInformation() {
    if (window.history.state?.writingFromInformation) window.history.back();
    else {
      const url = new URL(window.location.href); url.searchParams.delete("writing_session");
      window.history.replaceState({...window.history.state, tab: "chat"}, "", url); setWritingId(null);
    }
  }

  async function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;

    setMessages((current) => [
      ...current,
      { role: "user", text: trimmed },
    ]);
    setInput("");
    setLoading(true);

    try {
      const response = await sendMessage({
        session_id: sessionId,
        message: trimmed,
      }, consultation);
      setSessionId(response.session_id);
      if (response.selection) await choose(response.selection.scholarship_id, response.session_id);
      setMessages((current) => [
        ...current,
        {
          role: "bot",
          text: response.message,
          candidates: response.candidates,
          draft: response.draft,
          answerSource: response.answer_source,
          fallbackReason: response.fallback_reason,
          search: response.search,
          citations: response.citations,
        },
      ]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          role: "bot",
          text: `오류가 발생했어요: ${(error as Error).message}`,
        },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function handleFile(file: File) {
    if (busy) return;

    setMessages((current) => [
      ...current,
      { role: "user", text: `신청서 첨부 · ${file.name}`, file: file.name },
    ]);
    setUploading(true);

    try {
      const response = await uploadChatFile(file, sessionId);
      setSessionId(response.session_id);
      setMessages((current) => [
        ...current,
        { role: "bot", text: response.message },
      ]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          role: "bot",
          text: `첨부 실패: ${(error as Error).message}`,
        },
      ]);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  if (writingId) return <WritingChatPage key={writingId} sessionId={writingId} onBack={backToInformation}/>;

  return (
    <div className={`chat-page page-shell consultation-chat${policy ? ' is-policy' : ''}`}>
      <h1 className="sr-only">{policy ? '청년정책 상담' : '장학금 상담'}</h1>

      <section className="chat-panel" aria-label={policy ? '청년정책 상담 채팅' : '장학금 상담 채팅'}>
        {restoreError && <p className="notice" role="alert">{restoreError}</p>}
        <header className="chat-panel-head">
          <div className="assistant-avatar" aria-hidden="true">T</div>
          <div>
            <strong>{policy ? 'TMI 청년정책 도우미' : 'TMI 장학 도우미'}</strong>
            <span><i /> 상담 가능</span>
          </div>
          <button type="button" className="text-button" disabled={busy} onClick={() => void startNewConversation()}>새 대화</button>
        </header>
        {policy && <p className="policy-chat-note">정책 안내 자료를 바탕으로 상담합니다. 신청 기간이 확인되지 않은 정책을 현재 모집 중으로 단정하지 않습니다.</p>}

        <div className="messages" aria-live="polite">
          {messages.map((message, index) => (
            <div key={index} className={`row ${message.role}`}>
              {message.role === "bot" && (
                <span className="message-avatar" aria-hidden="true">T</span>
              )}
              <div className={`bubble ${message.role}`}>
                {message.file && <span className="file-label">첨부 파일</span>}
                <div className="text">{message.text}</div>
                {message.answerSource === 'llm' && <small className="chat-answer-source">AI 답변 · 검색 공고 기반</small>}
                {message.answerSource === 'fallback' && <small className="chat-answer-source is-fallback">{message.fallbackReason === 'no_policy_sources' ? '안내할 수 있는 자료를 준비하고 있어요.' : message.fallbackReason === 'not_configured' ? '검색 결과로 안내해 드려요.' : '검색 결과로 안내해 드려요.'}</small>}
                {message.search && <div className="chat-search-scope" aria-label="이 답변의 검색 범위">
                  <span>{message.search.mode === 'policy_intake' ? '공고 조건 기반 상담' : message.search.mode === 'browse' ? '전체 공고 둘러보기' : '맞춤 조건 검색'}</span>
                  {message.search.benefit_type && <span>{message.search.benefit_type === 'living' ? '생활비성' : '등록금'}</span>}
                  {message.search.ignored_fields?.includes('region') && <span>거주 지역 조건 제외</span>}
                  {message.search.query && <span>검색어: {message.search.query}</span>}
                </div>}

                {message.candidates && message.candidates.length > 0 && (
                  <div className="scholarship-cards">
                    {message.candidates.map((candidate) => (
                      <div key={candidate.id}>
                      <button
                        key={candidate.id}
                        className="scholarship-card"
                        type="button"
                        onClick={async () => {
                          if (busy) return;
                          if (policy) { await submit(`${candidate.index}번 정책의 상세 내용과 신청 방법을 알려줘`); return; }
                          setLoading(true);
                          try { await choose(candidate.id); }
                          catch(e) { setMessages(m => [...m, {role: "bot", text: (e as Error).message}]); }
                          finally { setLoading(false); }
                        }}
                        disabled={busy || (!policy && candidate.eligibility_status === 'not_eligible')}
                      >
                        <span className="card-index">{candidate.index}</span>
                        <span className="scholarship-card-copy">
                          <strong>{candidate.title}</strong>
                          <small>{candidate.eligibility_status === "satisfied" ? "확인한 정형 자격 충족" : candidate.eligibility_status === 'not_eligible' ? '입력한 개인 자격과 불일치 · 원문 둘러보기' : "추가 자격 확인 필요"}{policy ? ' · 신청 정보 확인' : ` · ${candidate.readiness.writing_ready ? '신청서 작성 가능' : '양식 확인 대기'}`}</small>
                          {policy && <small>{candidate.application_period?.label || '신청 기간 확인 필요'}</small>}
                          {candidate.reasons.length > 0 && (
                            <small>{candidate.reasons.join(" · ")}</small>
                          )}
                          {candidate.checks_needed.length > 0 && <small>{candidate.checks_needed.join(" · ")}</small>}
                          {typeof candidate.benefit.amount_desc === "string" && <small>{candidate.benefit.amount_desc}</small>}
                        </span>
                        {(candidate.deadline || policy) && (
                          <span className="deadline">{policy ? candidate.application_period?.label || '신청 기간 확인 필요' : `마감 ${candidate.deadline}`}</span>
                        )}
                        <span className="card-arrow" aria-hidden="true">→</span>
                      </button>
                      <a href={candidate.source_url} target="_blank" rel="noopener noreferrer">공고 원문 확인 ↗</a>
                      </div>
                    ))}
                  </div>
                )}

                {message.draft && (
                  <div className="draft">
                    <div className="draft-head">
                      초안 {message.draft.used_history ? "· 과거 이력 기반" : "· 자기소개 기반"}
                    </div>
                    {message.draft.answers.map((answer, answerIndex) => (
                      <article key={answerIndex} className="draft-item">
                        <div className="q">{answer.question}</div>
                        <div className="a">{answer.draft_text}</div>
                      </article>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}

          {busy && (
            <div className="row bot">
              <span className="message-avatar" aria-hidden="true">T</span>
              <div className="bubble bot loading-bubble">
                {uploading ? "파일을 읽고 있어요" : "답변을 준비하고 있어요"}
                <span className="typing-dots" aria-hidden="true"><i /><i /><i /></span>
              </div>
            </div>
          )}
          <div ref={endRef} />
        </div>

        <form
          className="input-bar"
          onSubmit={(event) => {
            event.preventDefault();
            submit(input);
          }}
        >
          {!policy && <><input
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) handleFile(file);
            }}
          />
          <button
            type="button"
            className="attach-button"
            onClick={() => fileRef.current?.click()}
            disabled={busy}
            aria-label="과거 신청서 파일 첨부"
            title="과거 신청서 첨부 (PDF/DOCX/HWPX/HWP/TXT)"
          >
            <span aria-hidden="true">＋</span>
          </button>
          </>}
          <label className="message-input">
            <span className="sr-only">{policy ? '청년정책 조건 입력' : '장학금 조건 입력'}</span>
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={policy ? '예: 서울에 살고 주거비 지원 정책을 찾고 있어요' : '예: 소득 3분위, 학점 3.8이고 서울에 살아요'}
              disabled={busy}
            />
          </label>
          <button className="send-button" type="submit" disabled={busy || !input.trim()}>
            보내기 <span aria-hidden="true">→</span>
          </button>
        </form>
        <p className="chat-footnote">
          {policy ? '지원 대상·신청 기간·신청 방법은 운영기관의 최신 공고를 확인해 주세요.' : 'AI가 작성한 내용은 제출 전 반드시 공고 원문과 함께 확인해 주세요.'}
        </p>
      </section>
    </div>
  );
}
