import { useEffect, useMemo, useState } from "react";
import { fetchOptions, updateProfile } from "../api";
import { issuerLabel, issuerLabels } from "../components/CardPicker";
import type { AuthUser, CardOption } from "../types";

const ALL = "전체";

interface Props {
  ownedIds: number[];
  onOwnedChange: (user: AuthUser) => void;
}

interface FilterProps {
  label: string;
  options: string[];
  value: string;
  onChange: (value: string) => void;
}

function FilterRow({ label, options, value, onChange }: FilterProps) {
  return (
    <div className="card-filter-row" role="group" aria-label={`${label} 필터`}>
      <strong>{label}</strong>
      <div>
        {[ALL, ...options].map((option) => (
          <button
            key={option}
            type="button"
            aria-pressed={option === value}
            onClick={() => onChange(option)}
          >
            {option}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 전체 카드를 카드사·혜택 업종으로 찾고, 내 보유 카드를 바로 켜고 끈다. */
export default function CardsPage({ ownedIds, onOwnedChange }: Props) {
  const [cards, setCards] = useState<CardOption[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issuer, setIssuer] = useState(ALL);
  const [category, setCategory] = useState(ALL);
  const [query, setQuery] = useState("");
  const [savingId, setSavingId] = useState<number | null>(null);

  useEffect(() => {
    fetchOptions()
      .then((options) => setCards(options.cards))
      .catch((fetchError: Error) => setError(fetchError.message));
  }, []);

  const issuers = useMemo(() => issuerLabels(cards ?? []), [cards]);
  const categories = useMemo(() => [...new Set((cards ?? []).flatMap((card) => card.categories))].sort(), [cards]);

  const q = query.trim().toLowerCase();
  const visible = (cards ?? []).filter((card) =>
    (issuer === ALL || issuerLabel(card.issuer) === issuer) &&
    (category === ALL || card.categories.includes(category)) &&
    (!q || `${card.card_name} ${card.issuer ?? ""}`.toLowerCase().includes(q))
  );

  async function toggle(id: number) {
    setSavingId(id);
    setError(null);
    try {
      const next = ownedIds.includes(id) ? ownedIds.filter((item) => item !== id) : [...ownedIds, id];
      onOwnedChange(await updateProfile({ card_ids: next }));
    } catch {
      setError("보유 카드를 저장하지 못했습니다. 다시 시도해 주세요.");
    } finally {
      setSavingId(null);
    }
  }

  return (
    <div className="page-shell cards-page">
      <header className="cards-head">
        <h1>카드 혜택 탐색</h1>
        <p>갖고 있는 카드를 추가하면 혜택 지도에 바로 반영돼요.</p>
      </header>

      <section className="card-filters" aria-label="카드 검색 조건">
        <input
          className="select-search"
          type="search"
          aria-label="카드명 검색"
          placeholder="카드명을 검색하세요"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <FilterRow label="카드사" options={issuers} value={issuer} onChange={setIssuer} />
        <FilterRow label="혜택" options={categories} value={category} onChange={setCategory} />
      </section>

      {error && <p className="notice error" role="alert">{error}</p>}
      {!cards && !error && <p className="notice">카드를 불러오는 중…</p>}

      {cards && (
        <>
          <p className="card-result-count" role="status">
            {visible.length}개 카드 · 보유 {ownedIds.length}장
          </p>
          {visible.length === 0 && <p className="select-empty">조건에 맞는 카드가 없어요.</p>}
          <ol className="card-rows">
            {visible.map((card) => {
              const owned = ownedIds.includes(card.id);
              return (
                <li key={card.id} className="card-row">
                  {/* ponytail: 이미지가 준비되기 전 자리표시. 파일이 생기면 <img>로 교체 */}
                  <span className="card-picker-face" aria-hidden="true">{issuerLabel(card.issuer)}</span>
                  <div className="card-row-body">
                    <h2>{card.card_name}</h2>
                    <p className={card.benefit_count ? "card-row-count" : "card-row-count is-empty"}>
                      {card.benefit_count ? `지도 혜택 ${card.benefit_count}건` : "지도에 표시되는 혜택 없음"}
                    </p>
                    {card.highlights.length > 0 && (
                      <ul>{card.highlights.map((text) => <li key={text}>{text}</li>)}</ul>
                    )}
                  </div>
                  <button
                    type="button"
                    className="card-row-toggle"
                    aria-pressed={owned}
                    aria-label={`${card.card_name} ${owned ? "보유 해제" : "보유 카드로 추가"}`}
                    disabled={savingId !== null}
                    onClick={() => toggle(card.id)}
                  >
                    {owned ? "✓ 보유 중" : "+ 내 카드"}
                  </button>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </div>
  );
}
