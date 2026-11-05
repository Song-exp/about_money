import type { MetaOptions } from "../types";
import CardPicker from "./CardPicker";

interface Props {
  cards: MetaOptions["cards"];
  selected: string[];
  onChange: (ids: string[]) => void;
}

export default function CardRegistration({ cards, selected, onChange }: Props) {
  const selectedCards = cards.filter((card) => selected.includes(String(card.id)));

  return <section className="card-registration" aria-labelledby="card-registration-title">
    <div className="select-head">
      <h3 id="card-registration-title">보유 카드</h3>
      <span className="select-count">{selected.length ? `${selected.length}개 선택` : "미선택"}</span>
    </div>
    <p>갖고 있는 카드를 모두 선택하세요. 선택한 카드의 혜택이 지도에 표시됩니다.</p>
    <CardPicker cards={cards} selected={selected} onChange={onChange} />
    {selectedCards.length > 0 && <div className="chips">
      {selectedCards.map((card) => <button key={card.id} type="button" className="chip"
        aria-label={`${card.card_name} 선택 해제`}
        onClick={() => onChange(selected.filter((id) => id !== String(card.id)))}>
        {card.card_name} <span aria-hidden="true">×</span>
      </button>)}
    </div>}
  </section>;
}
