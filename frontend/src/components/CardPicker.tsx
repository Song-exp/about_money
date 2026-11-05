import { useMemo, useState } from "react";
import type { MetaOptions } from "../types";

type Card = MetaOptions["cards"][number];

interface Props {
  cards: Card[];
  selected: string[];
  onChange: (ids: string[]) => void;
}

/** "KB국민카드" -> "KB국민", "IBK기업은행(BC 발급망)" -> "IBK기업은행". 발급사 미상은 "기타". */
export const issuerLabel = (issuer: string | null) =>
  issuer?.replace(/\(.*\)/, "").replace(/카드$/, "").trim() || "기타";

/** 카드사 이름 목록. 발급사 미상인 "기타"는 맨 뒤로 보낸다. */
export const issuerLabels = (cards: Card[]) => [...new Set(cards.map((card) => issuerLabel(card.issuer)))]
  .sort((left, right) => Number(left === "기타") - Number(right === "기타"));

/** 카드사 탭으로 고르고, 검색은 탭과 무관하게 전체 카드를 찾는다. */
export default function CardPicker({ cards, selected, onChange }: Props) {
  const issuers = useMemo(() => issuerLabels(cards), [cards]);
  const [issuer, setIssuer] = useState(issuers[0]);
  const [query, setQuery] = useState("");

  const q = query.trim().toLowerCase();
  const visible = q
    ? cards.filter((card) => `${card.card_name} ${card.issuer ?? ""}`.toLowerCase().includes(q))
    : cards.filter((card) => issuerLabel(card.issuer) === issuer);

  function toggle(id: string) {
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  }

  return (
    <div className="card-picker">
      <div className="card-picker-tabs" role="group" aria-label="카드사 선택">
        {issuers.map((name) => (
          <button
            key={name}
            type="button"
            className="card-picker-tab"
            aria-pressed={!q && name === issuer}
            onClick={() => { setIssuer(name); setQuery(""); }}
          >
            {name}
          </button>
        ))}
      </div>

      <input
        className="select-search"
        type="search"
        aria-label="카드명 검색"
        placeholder="카드명을 검색하세요"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />

      {visible.length === 0 && <p className="select-empty">검색 결과가 없어요.</p>}
      <ul className="card-picker-grid">
        {visible.map((card) => {
          const id = String(card.id);
          return (
            <li key={id}>
              <button
                type="button"
                className="card-picker-item"
                aria-pressed={selected.includes(id)}
                onClick={() => toggle(id)}
              >
                {/* ponytail: 이미지가 준비되기 전 자리표시. 파일이 생기면 <img>로 교체 */}
                <span className="card-picker-face" aria-hidden="true">{issuerLabel(card.issuer)}</span>
                <span className="card-picker-name">{card.card_name}</span>
                <span className="card-picker-sub">혜택 {card.benefit_count}건</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
