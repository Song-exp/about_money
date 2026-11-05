import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  fetchAllLocalBenefits as fetchLocalBenefits,
  estimatePayment,
  fetchMapSourceStatus,
  fetchAllNearbyStores as fetchNearbyStores,
  fetchOptions,
  reportOffer,
  registerOwnedCard,
} from "../api";
import { getPersonaForProfile } from "../personas";
import type {
  BenefitProgram,
  AuthUser,
  MapSourceStatus,
  CardBenefit,
  CategoryOption,
  LocalBenefit,
  PayMethod,
  Store,
  StoreCategory,
  UserProfileLocal,
} from "../types";
import { useKakaoLoader } from "../useKakaoLoader";

const PAY_LABELS: Record<PayMethod, string> = {
  kakao: "카카오페이",
  toss: "토스페이",
  naver: "네이버페이",
};

const ALL_PAYS: PayMethod[] = ["kakao", "toss", "naver"];
const PROGRAM_LABELS: Record<BenefitProgram, string> = {
  goodprice: "착한가격업소",
  khu_alliance: "경희대 제휴",
  onnuri: "온누리",
  seoulpay: "서울Pay+",
  zeropay: "제로페이",
};
const PROGRAM_COLORS: Record<BenefitProgram, string> = {
  goodprice: "#167D8D",
  khu_alliance: "#A40F16",
  onnuri: "#C0A353",
  seoulpay: "#AD1D19",
  zeropay: "#987B32",
};
const DEFAULT_CENTER = { lat: 37.5938, lng: 127.0564 };

const CATEGORY_META: Record<
  StoreCategory,
  { label: string; color: string; icon: string }
> = {
  convenience: { label: "편의점", color: "#eab308", icon: "🏪" },
  cafe: { label: "카페", color: "#f97316", icon: "☕" },
  restaurant: { label: "음식점", color: "#ef4444", icon: "🍽" },
  mart: { label: "마트", color: "#22c55e", icon: "🛒" },
  bakery: { label: "베이커리", color: "#a855f7", icon: "🥐" },
  hnb: { label: "H&B", color: "#ec4899", icon: "💄" },
  other: { label: "기타", color: "#6b7280", icon: "•" },
};

function topRate(store: Store): number {
  const cardRates = store.card_benefits.map((benefit) => benefit.benefit_type === "percent" ? benefit.value_max ?? 0 : 0);
  return Math.max(store.max_discount_rate, store.max_card_discount_rate, ...cardRates);
}

/** 등록된 대표 메뉴 중 가장 낮은 가격. 없으면 Infinity. */
function menuPrice(store: Store): number {
  return Math.min(Infinity, ...(store.local_benefits ?? [])
    .filter((benefit) => benefit.discount_type === "menu_price")
    .map((benefit) => benefit.discount_value ?? Infinity));
}

/** 지도에 띄울 내 혜택이 있는가. 메뉴 가격은 할인이 아니라 가성비 탭에서 다룬다. */
function hasMyBenefit(store: Store): boolean {
  return store.offers.length > 0 || store.card_benefits.length > 0 || (store.card_programs?.length ?? 0) > 0 ||
    (store.local_benefits ?? []).some((benefit) => benefit.discount_type !== "menu_price");
}

/** 목록에 "어떤 수단의 혜택인지" 보여준다. 가장 높은 할인을 주는 수단을 고른다. */
function benefitSource(store: Store): string {
  const offer = [...store.offers].sort((a, b) => b.discount_rate - a.discount_rate)[0];
  if (offer && offer.discount_rate >= store.max_card_discount_rate) return PAY_LABELS[offer.pay_method];
  const card = [...store.card_benefits].sort((a, b) => (b.value_max ?? 0) - (a.value_max ?? 0))[0]
    ?? store.card_programs?.[0];
  if (card) return card.card_name;
  const local = store.local_benefits?.find((benefit) => benefit.discount_type !== "menu_price");
  return local ? local.programs.map((program) => PROGRAM_LABELS[program]).join(" · ") : "";
}

function benefitLabel(benefit: CardBenefit): string {
  if (benefit.benefit_text) return benefit.benefit_text;
  if (benefit.benefit_type === "percent" && benefit.value_min !== null) {
    return benefit.value_max && benefit.value_max !== benefit.value_min
      ? `${benefit.value_min}~${benefit.value_max}%`
      : `${benefit.value_min}%`;
  }
  if (benefit.benefit_type === "amount" && benefit.value_min !== null) {
    return `${benefit.value_min.toLocaleString()}원 할인`;
  }
  return "행사 진행";
}

function distanceMeters(
  a: { lat: number; lng: number },
  b: { lat: number; lng: number }
): number {
  const radius = 6371000;
  const toRad = (value: number) => (value * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * radius * Math.asin(Math.sqrt(h));
}

function localBenefitRate(_benefit: LocalBenefit): number {
  // Static confidence labels do not establish verified user eligibility.
  return 0;
}

function isCashDiscount(benefit: LocalBenefit): boolean {
  const text = [benefit.requires, benefit.conditions, benefit.benefit_text]
    .filter(Boolean)
    .join(" ");
  return text.includes("현금") || text.includes("계좌이체");
}

function localBenefitMarkerLabel(benefit: LocalBenefit): string {
  if (benefit.discount_type === 'menu_price') return `${(benefit.discount_value ?? 0).toLocaleString('ko-KR')}원`;
  if (isCashDiscount(benefit)) return "현금 할인";
  if (benefit.discount_type === "percent" && benefit.discount_value) {
    return `${benefit.discount_value}%`;
  }
  if (benefit.discount_type === "fixed" && (benefit.discount_value ?? 0) > 0) {
    return "정액 할인";
  }
  if (benefit.discount_type === "fixed") return "제휴가";
  if (benefit.discount_type === "event") return "제휴 혜택";
  if (benefit.discount_type === "currency") return "가맹";
  return "혜택";
}

function localBenefitValueLabel(benefit: LocalBenefit): string {
  const value = benefit.discount_value ?? 0;
  if (benefit.discount_type === 'menu_price') return `등록 가격 ${value.toLocaleString('ko-KR')}원`;
  if (isCashDiscount(benefit)) {
    const amount =
      benefit.discount_type === "percent" && value > 0
        ? ` · ${value}%`
        : benefit.discount_type === "fixed" && value > 0
          ? ` · ${value.toLocaleString("ko-KR")}원`
          : "";
    return `현금 할인${amount}`;
  }
  if (benefit.discount_type === "percent" && value > 0) return `${value}% 할인`;
  if (benefit.discount_type === "fixed" && value > 0) {
    return `${value.toLocaleString("ko-KR")}원 정액 할인`;
  }
  if (benefit.discount_type === "fixed") return "학생 제휴가";
  if (benefit.discount_type === "event") return "제휴 혜택";
  if (benefit.discount_type === "currency") return "사용 가능 가맹점";
  return "혜택 확인";
}

function localBenefitHasSaving(_benefit: LocalBenefit): boolean {
  return false;
}

function primaryProgram(benefit: LocalBenefit): BenefitProgram {
  return benefit.programs[0] ?? "khu_alliance";
}

interface Props {
  profile: UserProfileLocal | null;
  onUserChange: (user: AuthUser) => void;
}

export default function MapPage({ profile, onUserChange }: Props) {
  const registrationPending = useRef(false);
  const [savingCardId, setSavingCardId] = useState<number | null>(null);
  const [cardSaveNotice, setCardSaveNotice] = useState<string | null>(null);
  const [cardSaveError, setCardSaveError] = useState<string | null>(null);
  const [programInputs, setProgramInputs] = useState<import('../api').ProgramPaymentInputs>({
    program_modes: {}, program_effective_at: {}, program_daily_uses: {}, program_monthly_uses: {}, program_first_transaction: {},
  });
  const { ready, error: sdkError } = useKakaoLoader(
    import.meta.env.VITE_KAKAO_MAP_KEY
  );
  const mapRef = useRef<HTMLDivElement>(null);
  const mapObj = useRef<any>(null);
  const mapEl = useRef<HTMLDivElement | null>(null);
  const overlays = useRef<any[]>([]);
  const persona = getPersonaForProfile(profile);

  const configuredPays = useMemo<PayMethod[]>(
    () =>
      profile && profile.payMethods.length > 0 ? profile.payMethods : ALL_PAYS,
    [profile]
  );

  const [stores, setStores] = useState<Store[]>([]);
  const [localBenefits, setLocalBenefits] = useState<LocalBenefit[]>([]);
  const [localStores, setLocalStores] = useState<Store[]>([]);
  const [localLoading, setLocalLoading] = useState(false);
  const [resultMode, setResultMode] = useState<"all" | "budget">("all");
  const [maxPrice, setMaxPrice] = useState<number | null>(null);
  const [listLimit, setListLimit] = useState(8);
  const [unlocatedBudget, setUnlocatedBudget] = useState(0);
  const [sourceStatus, setSourceStatus] = useState<MapSourceStatus | null>(null);
  const sourceRevision = `${sourceStatus?.revision ?? ''}:${sourceStatus?.sources.map(s => `${s.key}:${s.last_success_at}:${s.status}:${s.stale}`).join(',') ?? ''}`;
  useEffect(() => {
    let cancelled = false;
    const refresh = () => fetchMapSourceStatus().then(status => {
      if (!cancelled) setSourceStatus(status);
    }).catch(() => { /* Keep the last known revision; source diagnostics belong in administration. */ });
    refresh();
    const timer = window.setInterval(refresh, 30000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);
  const [sort, setSort] = useState<"distance" | "discount">("distance");
  const [center, setCenter] = useState(DEFAULT_CENTER);
  const [paymentAmount, setPaymentAmount] = useState(10000);
  const [paymentPath, setPaymentPath] = useState("card");
  const [paymentResult, setPaymentResult] = useState<import("../api").PaymentEstimate | null>(null);
  const [paymentBusy, setPaymentBusy] = useState(false);
  const [priorSpend, setPriorSpend] = useState<Record<number, number>>({});
  const [remainingCaps, setRemainingCaps] = useState<Record<string, number>>({});
  const [priorSpendGroups, setPriorSpendGroups] = useState<Record<string, number>>({});
  const [graceStatusGroups, setGraceStatusGroups] = useState<Record<string, 'eligible' | 'not_eligible'>>({});
  const [excludedItemsAbsent, setExcludedItemsAbsent] = useState(false);
  const [otherConditionsConfirmed, setOtherConditionsConfirmed] = useState(false);
  const [cardNames, setCardNames] = useState<Record<number, string>>({});
  const paymentRevision = useRef(0);
  const [selected, setSelected] = useState<Store | null>(null);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);
  // 내가 제보한 혜택. 서버는 총 건수만 주므로 '내가 눌렀는지'는 여기서 기억한다.
  const [reportedOffers, setReportedOffers] = useState<Set<number>>(new Set());
  const [categories, setCategories] = useState<CategoryOption[]>([]);
  const [activeCat, setActiveCat] = useState<StoreCategory | null>(null);

  useEffect(() => {
    fetchOptions()
      .then((options) => { setCategories(options.categories); setCardNames(Object.fromEntries(options.cards.map(card => [card.id, card.card_name]))); })
      .catch(() => setCategories([]));
  }, []);

  useEffect(() => {
    setActiveCat(null);
  }, [persona.id]);

  const myCardIds = useMemo(
    () => (profile?.cardIds ?? []).slice().sort((a, b) => a - b),
    [profile]
  );
  async function saveOwnedCard(cardId: number, cardName: string) {
    if (registrationPending.current || myCardIds.includes(cardId)) return;
    registrationPending.current = true;
    setSavingCardId(cardId);
    setCardSaveNotice(null);
    setCardSaveError(null);
    try {
      const savedUser = await registerOwnedCard(cardId);
      onUserChange(savedUser);
      invalidatePayment();
      setCardSaveNotice(`${cardName}을 보유 카드로 등록했습니다. 마이페이지에서도 확인할 수 있습니다.`);
    } catch (error) {
      setCardSaveError(error instanceof Error ? error.message : '보유 카드를 등록하지 못했습니다.');
    } finally {
      registrationPending.current = false;
      setSavingCardId(null);
    }
  }

  function renderCardRegistration(cardId: number, cardName: string) {
    return myCardIds.includes(cardId)
      ? <small>등록된 보유 카드 · 적용 조건을 확인해 주세요.</small>
      : <button type="button" className="location-button" disabled={savingCardId !== null}
          aria-label={`${cardName} 보유 카드로 등록`} onClick={() => void saveOwnedCard(cardId, cardName)}>
          {savingCardId === cardId ? '등록 중…' : '이 카드를 갖고 있어요 · 보유 카드로 등록'}
        </button>;
  }
  // 등록한 카드와 결제수단은 필터 없이 자동으로 적용한다.
  const cardFilter = myCardIds.length > 0 ? myCardIds : [0];
  const cardFilterKey = cardFilter.join(",");
  const payKey = configuredPays.slice().sort().join(",");
  const benefitPrograms = profile?.benefitPrograms ?? [];
  const studentCredentials = profile?.studentCredentials ?? [];
  const benefitProgramKey = benefitPrograms.slice().sort().join(",");
  const credentialKey = studentCredentials.slice().sort().join(",");
  const visibleStores = useMemo(
    () => {
      const merged = new Map<string, Store>();
      for (const store of [...stores, ...localStores]) {
        const key = store.place_id ? `place:${store.place_id}` : `record:${store.id}`;
        const previous = merged.get(key);
        merged.set(key, previous ? {...previous, local_benefits: [...(previous.local_benefits ?? []), ...(store.local_benefits ?? [])]} : store);
      }
      return [...merged.values()].sort((a, b) => sort === 'distance' ? (a.distance_m ?? Infinity) - (b.distance_m ?? Infinity) : topRate(b) - topRate(a));
    },
    [stores, localStores, sort]
  );
  const resultStores = useMemo(() => {
    if (resultMode === "all") return visibleStores.filter(hasMyBenefit);
    return visibleStores
      .filter(store => store.category !== "cafe" && store.category !== "bakery" && menuPrice(store) !== Infinity && (maxPrice === null || menuPrice(store) <= maxPrice))
      .sort((a, b) => menuPrice(a) - menuPrice(b));
  }, [visibleStores, resultMode, maxPrice]);

  useEffect(() => { setListLimit(8); }, [resultMode, activeCat, sort, center]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      if (selected) document.getElementById("store-detail-title")?.focus({ preventScroll: true });
      mapObj.current?.relayout?.();
      if (selected && mapObj.current) mapObj.current.setCenter(new window.kakao.maps.LatLng(selected.lat, selected.lng));
    });
    return () => cancelAnimationFrame(frame);
  }, [selected?.id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setApiError(null);
    setSelected(null);
    setStores([]);

    fetchNearbyStores({
      lat: center.lat,
      lng: center.lng,
      radius_m: 3000,
      verified_locations_only: true,
      pay: configuredPays.length === ALL_PAYS.length ? undefined : configuredPays,
      card_ids: cardFilter,
      category: activeCat ? [activeCat] : undefined,
      sort,
    })
      .then((data) => {
        if (!cancelled) setStores(data);
      })
      .catch((error: Error) => {
        if (!cancelled) setApiError(error.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [center, payKey, sort, cardFilterKey, activeCat, sourceRevision, refreshRevision]);

  useEffect(() => {
    let cancelled = false;
    setLocalLoading(true);
    setLocalStores([]);
    setLocalBenefits([]);
    setSelected(null);
    const requests = [];
    const profilePrograms = benefitPrograms.filter(p => p !== 'goodprice');
    if (profilePrograms.length) requests.push(fetchLocalBenefits({
      programs: profilePrograms, credentials: studentCredentials,
      category: activeCat ? [activeCat] : undefined, limit: 200, lat: center.lat, lng: center.lng, radius_m: 3000,
    }));
    requests.push(fetchLocalBenefits({
      programs: ['goodprice'], category: activeCat ? [activeCat] : undefined, limit: 200, lat: center.lat, lng: center.lng, radius_m: 3000,
    }));
    Promise.all(requests)
      .then((pages) => {
        if (cancelled) return;
        setLocalBenefits([...new Map(pages.flatMap(page => page.items).map(item => [item.source_key, item])).values()]);
        setUnlocatedBudget(pages.reduce((sum, page) => sum + page.unlocated_count, 0));
      })
      .catch((error: Error) => {
        if (!cancelled) setApiError(error.message);
      })
      .finally(() => {
        if (!cancelled) setLocalLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [benefitProgramKey, credentialKey, activeCat, sourceRevision, center]);

  useEffect(() => {
    if (localBenefits.length === 0) {
      if (localBenefits.length === 0) { setLocalStores([]); }
      return;
    }
    let cancelled = false;
    const resolved = localBenefits.map(benefit => ({benefit,
      point: benefit.lat !== null && benefit.lng !== null ? {lat: benefit.lat, lng: benefit.lng} : null}));
    Promise.resolve().then(() => {
      if (cancelled) return;
      const mapped = resolved.flatMap(({ benefit, point }) => {
        if (!point) return [];
        const distance = distanceMeters(center, point);
        if (distance > 3000) return [];
        const program = primaryProgram(benefit);
        const rate = localBenefitRate(benefit);
        const store: Store = {
          id: -benefit.id,
          place_id: benefit.place_id,
          location_verified: true,
          brand: benefit.merchant_name,
          branch: benefit.programs.map((item) => PROGRAM_LABELS[item]).join(" · "),
          category: benefit.category,
          address: benefit.address,
          lat: point.lat,
          lng: point.lng,
          mark: PROGRAM_LABELS[program].slice(0, 1),
          color: PROGRAM_COLORS[program],
          offers: [],
          card_benefits: [],
          distance_m: Math.round(distance * 10) / 10,
          max_discount_rate: rate,
          max_card_discount_rate: 0,
          best_deal: null,
          local_benefits: [benefit],
        };
        return [store];
      });
      mapped.sort(
        sort === "discount"
          ? (a, b) => topRate(b) - topRate(a)
          : (a, b) => (a.distance_m ?? 0) - (b.distance_m ?? 0)
      );
      setLocalStores(mapped);
    });
    return () => {
      cancelled = true;
    };
  }, [ready, localBenefits, center, sort]);

  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const kakao = window.kakao;
    if (mapObj.current && mapEl.current === mapRef.current) return;

    mapObj.current = new kakao.maps.Map(mapRef.current, {
      center: new kakao.maps.LatLng(center.lat, center.lng),
      level: 4,
    });
    mapEl.current = mapRef.current;
    overlays.current = [];
  }, [ready]);

  useEffect(() => {
    if (!ready || !mapObj.current) return;
    const kakao = window.kakao;
    overlays.current.forEach((overlay) => overlay.setMap(null));
    overlays.current = [];

    resultStores.forEach((store) => {
      const rate = topRate(store);
      const meta = CATEGORY_META[store.category];
      const localBenefit = store.local_benefits?.[0];
      const markerColor = localBenefit ? store.color ?? meta.color : meta.color;
      const button = document.createElement("button");
      button.type = "button";
      button.className = `store-icon${selected?.id === store.id ? " is-selected" : ""}`;
      button.setAttribute(
        "aria-label",
        `${store.brand} ${store.branch}, ${
          localBenefit
            ? localBenefitValueLabel(localBenefit)
            : rate > 0
              ? `최대 ${rate}% 할인`
              : "혜택 확인"
        }`
      );

      const hasSaving = localBenefit
        ? localBenefitHasSaving(localBenefit)
        : rate > 0;
      const value = localBenefit
        ? localBenefitMarkerLabel(localBenefit)
        : rate > 0
          ? `${rate}%`
          : "혜택";
      const marker = document.createElement("span");
      marker.className = `map-pill${hasSaving ? " has-saving" : ""}`;
      marker.style.setProperty("--category-color", markerColor);
      marker.textContent = `${meta.icon} ${value}`;
      button.title = `${store.brand} ${store.branch}`;
      button.append(marker);

      button.addEventListener("click", (event) => {
        event.stopPropagation();
        openStore(store);
      });

      const overlay = new kakao.maps.CustomOverlay({
        map: mapObj.current,
        position: new kakao.maps.LatLng(store.lat, store.lng),
        content: button,
        yAnchor: 1.1,
        zIndex: selected?.id === store.id ? 8 : 3,
      });
      overlays.current.push(overlay);
    });
  }, [ready, resultStores, selected]);

  useEffect(() => {
    paymentRevision.current += 1;
    setPaymentResult(null);
    setPaymentBusy(false);
  }, [selected, paymentAmount, paymentPath, profile]);

  useEffect(() => {
    setExcludedItemsAbsent(false);
    setOtherConditionsConfirmed(false);
    setProgramInputs({program_modes: {}, program_effective_at: {}, program_daily_uses: {},
      program_monthly_uses: {}, program_first_transaction: {}});
  }, [selected, profile]);

  async function comparePayment(store: Store) {
    const revision = ++paymentRevision.current;
    setPaymentBusy(true);
    try {
      const result = await estimatePayment(store.id > 0 ? 'store' : 'local', Math.abs(store.id), paymentAmount, paymentPath, priorSpend, remainingCaps, priorSpendGroups, graceStatusGroups,
        {excluded_items_confirmed_absent: excludedItemsAbsent, other_conditions_confirmed: otherConditionsConfirmed}, programInputs);
      if (revision === paymentRevision.current) setPaymentResult(result);
    } catch (error) {
      if (revision === paymentRevision.current) setApiError(error instanceof Error ? error.message : '비교 실패');
    } finally {
      if (revision === paymentRevision.current) setPaymentBusy(false);
    }
  }

  function searchMapArea() {
    const point = mapObj.current?.getCenter();
    if (point) setCenter({lat: point.getLat(), lng: point.getLng()});
  }

  function updatePriorSpend(id: number, value: string) {
    paymentRevision.current += 1;
    setPaymentBusy(false);
    setPaymentResult(null);
    setPriorSpend(current => { const next = {...current}; if (!value) delete next[id]; else next[id] = Number(value); return next; });
  }

  function updateRemainingCap(key: string, value: string) {
    paymentRevision.current += 1;
    setPaymentBusy(false);
    setPaymentResult(current => current ? {...current, best: null, basis: '입력이 변경되었습니다. 다시 비교해 주세요.',
      candidates: current.candidates.map(candidate => ({...candidate, estimated_saving: null}))} : null);
    setRemainingCaps(current => { const next = {...current}; if (!value) delete next[key]; else next[key] = Number(value); return next; });
  }

  function updateSpendGroup(key: string, value: string) {
    paymentRevision.current += 1;
    setPaymentBusy(false);
    setPaymentResult(current => current ? {...current, best: null, basis: '입력이 변경되었습니다. 다시 비교해 주세요.',
      candidates: current.candidates.map(candidate => ({...candidate, estimated_saving: null}))} : null);
    setPriorSpendGroups(current => {const next = {...current}; if (!value) delete next[key]; else next[key] = Number(value); return next;});
  }

  function invalidatePayment() {
    paymentRevision.current += 1;
    setPaymentBusy(false);
    setPaymentResult(current => current ? {...current, best: null, basis: '입력이 변경되었습니다. 다시 비교해 주세요.',
      candidates: current.candidates.map(candidate => ({...candidate, estimated_saving: null}))} : null);
  }

  function setMapCenter(next: { lat: number; lng: number }, level = 4) {
    setCenter(next);
    if (mapObj.current && ready) {
      mapObj.current.setCenter(new window.kakao.maps.LatLng(next.lat, next.lng));
      mapObj.current.setLevel(level);
    }
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      setApiError("이 브라우저는 위치 조회를 지원하지 않아요.");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setMapCenter({
          lat: position.coords.latitude,
          lng: position.coords.longitude,
        });
      },
      () => setApiError("위치 권한이 거부되어 기본 위치를 표시합니다.")
    );
  }

  function openStore(store: Store) {
    setSelected(store);
    requestAnimationFrame(() => {
      document.getElementById("store-detail-title")?.focus({preventScroll: true});
      if (window.matchMedia('(max-width: 900px)').matches) {
        document.querySelector('.map-store-card')?.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    });
  }

  async function toggleReport(offerId: number) {
    try {
      const result = await reportOffer(offerId);
      setReportedOffers((current) => {
        const next = new Set(current);
        if (result.active) next.add(offerId);
        else next.delete(offerId);
        return next;
      });
      setSelected(null);
      setStores([]);
      setRefreshRevision(value => value + 1);
    } catch (error) {
      setApiError(
        error instanceof Error ? error.message : "제보에 실패했습니다."
      );
    }
  }

  function renderPaymentCalculator(store: Store) {
    return (
      <details className="payment-calculator" key={store.id}>
        <summary><span>예상 절약액 계산</span><small>금액·결제수단과 적용 조건 입력</small></summary>
        <div className="payment-form">
        <div className="payment-input-grid">
        <label>결제 예정 금액<input type="number" min="1" max="100000000" value={paymentAmount} onChange={e => setPaymentAmount(Number(e.target.value))} /></label>
        <label>결제 경로<select value={paymentPath} onChange={e => setPaymentPath(e.target.value)}>
          <option value="card">카드 직접 결제</option><option value="kakao">카카오페이</option><option value="naver">네이버페이</option><option value="toss">토스페이</option><option value="cash">현금</option><option value="currency">지역화폐</option>
        </select></label>
        </div>
        <p className="payment-hint">혜택의 제외 품목·지점·실적 조건을 읽고 해당하는 항목을 확인해 주세요. 모르는 값은 비워둘 수 있어요.</p>
        <label className="payment-check"><input type="checkbox" checked={excludedItemsAbsent} onChange={e => {invalidatePayment(); setExcludedItemsAbsent(e.target.checked);}} /><span>이번 결제는 안내된 할인 제외 품목에 해당하지 않습니다.</span></label>
        <label className="payment-check"><input type="checkbox" checked={otherConditionsConfirmed} onChange={e => {invalidatePayment(); setOtherConditionsConfirmed(e.target.checked);}} /><span>비교할 혜택의 나머지 지점·결제 경로·실적 산정 조건도 확인했습니다.</span></label>
        {myCardIds.map(id => <label key={id}>{cardNames[id] ?? '보유 카드'} 전월 실적 (모르면 비워두세요)<input type="number" min="0" value={priorSpend[id] ?? ''} onChange={e => updatePriorSpend(id, e.target.value)} /></label>)}
        <button className="payment-compare" type="button" disabled={paymentBusy || !Number.isInteger(paymentAmount) || paymentAmount <= 0} onClick={() => void comparePayment(store)}>{paymentBusy ? '비교 중…' : '예상 절약액 비교'}</button>
        {paymentResult && <div className="payment-result" role="status">
          <p>{paymentResult.basis}</p>
          {paymentResult.best ? <p>입력 조건 기준 최대 예상 절약액: {paymentResult.best.estimated_saving.toLocaleString('ko-KR')}원</p> : <p>적용 조건을 모두 확인한 할인 후보가 없습니다.</p>}
          {paymentResult.candidates.map(candidate => <div key={candidate.key}><p>{candidate.label} · {candidate.estimated_saving === null ? '계산 불가' : `${candidate.estimated_saving.toLocaleString('ko-KR')}원 예상`} · {candidate.reasons.join(' · ')}</p>
            {candidate.grace_group && <label>{candidate.label} 최초 발급·사용등록 실적 유예<select value={graceStatusGroups[candidate.grace_group] ?? ''} onChange={e => {invalidatePayment(); const key = candidate.grace_group!; const value = e.target.value; setGraceStatusGroups(current => {const next = {...current}; if (!value) delete next[key]; else next[key] = value as 'eligible' | 'not_eligible'; return next;});}}><option value="">확인하지 못함</option><option value="eligible">원문 기준 신규 발급 유예 대상</option><option value="not_eligible">유예 대상 아님</option></select></label>}
            {candidate.prior_spend_group && <label>{candidate.label} 동일계열 합산 전월 실적<input type="number" min="0" value={priorSpendGroups[candidate.prior_spend_group] ?? ''} onChange={e => updateSpendGroup(candidate.prior_spend_group!, e.target.value)} /></label>}
            {candidate.monthly_cap_limit !== undefined && candidate.monthly_cap_limit !== null && <label>{candidate.label} 이번 달 잔여 할인 한도<input type="number" min="0" value={remainingCaps[candidate.cap_key ?? candidate.key] ?? ''} onChange={e => updateRemainingCap(candidate.cap_key ?? candidate.key, e.target.value)} /></label>}
          </div>)}
        </div>}
        </div>
      </details>
    );
  }

  function renderDeals(store: Store) {
    const hasDiscount = store.offers.length > 0 || store.card_benefits.length > 0 || (store.card_programs ?? []).length > 0
      || store.local_benefits?.some(b => b.discount_type !== "menu_price");
    return (
      <div className="deal-details">
        {store.local_benefits?.map((benefit) => (
          <div key={benefit.source_key} className="local-benefit-deal">
            <div className="local-benefit-programs">
              {benefit.programs.map((program) => (
                <span
                  key={program}
                  style={{ "--program-color": PROGRAM_COLORS[program] } as CSSProperties}
                >
                  {PROGRAM_LABELS[program]}
                </span>
              ))}
            </div>
            <div className="local-benefit-title">
              <strong>{benefit.benefit_text || benefit.merchant_name}</strong>
              <span className={`local-benefit-value${isCashDiscount(benefit) ? " is-cash" : ""}`}>
                {localBenefitValueLabel(benefit)}
              </span>
            </div>
            {benefit.discount_type === 'menu_price' && <p className="budget-price-note">메뉴 가격 정보예요. 할인율과 별개이며 방문 전 현재 가격을 확인해 주세요.</p>}
            {benefit.requires && <p>이용 조건 · {benefit.requires}</p>}
            {benefit.discount_type !== 'menu_price' && <small>적용 조건·최신 정보 확인 필요</small>}
            {benefit.conditions && <small>{benefit.conditions}</small>}
            {benefit.valid_to && <small>유효기간 · {benefit.valid_to}</small>}
            {benefit.source_url && (
              <a href={benefit.source_url} target="_blank" rel="noreferrer">
                {benefit.source_type === 'goodprice' ? '공공데이터 출처 보기' : '혜택 근거 보기'}
              </a>
            )}
          </div>
        ))}
        {store.best_deal && (
          <div className={`best-deal ${store.best_deal.kind}`}>
            <span className="tag">BEST</span>
            <strong>{store.best_deal.label}</strong>
            <span className="rate">{store.best_deal.discount_rate}%</span>
            {store.best_deal.note && <small>{store.best_deal.note}</small>}
          </div>
        )}

        {store.offers.length > 0 && (
          <div className="deal-group">
            <p className="deal-title">간편결제</p>
            {store.offers.map((offer) => (
              <div
                key={offer.pay_method}
                className={offer.reported || offer.source_stale ? "offer-row stale" : "offer-row"}
              >
                <span className={`pay-badge ${offer.pay_method}`}>
                  {PAY_LABELS[offer.pay_method]}
                </span>
                <strong>{offer.discount_rate}%</strong>
                <small>{offer.condition_text}</small>
                <span className="warn-badge">적용 조건·최신 정보 확인 필요</span>
                {offer.source_url && <a href={offer.source_url} target="_blank" rel="noopener noreferrer">프로모션 원문</a>}
                {offer.source_checked_at && <small>정보 확인: {new Date(offer.source_checked_at).toLocaleDateString('ko-KR')}</small>}
                {offer.source_stale && <span className="warn-badge">이용 전 혜택 확인 필요</span>}
                {offer.reported && (
                  <span className="warn-badge">제보 {offer.report_count}건</span>
                )}
                {/* 확인창 없이 한 번에 보낸다. 물어보면 안 누르고, 안 누르면
                    지도 데이터가 낡은 채로 남는다. 다시 누르면 취소. */}
                <button
                  type="button"
                  className={
                    reportedOffers.has(offer.id)
                      ? "offer-report on"
                      : "offer-report"
                  }
                  onClick={() => void toggleReport(offer.id)}
                >
                  {reportedOffers.has(offer.id) ? "제보함 ✓" : "끝난 혜택이에요"}
                </button>
              </div>
            ))}
          </div>
        )}

        {store.card_benefits.length > 0 && (
          <div className="deal-group">
            <p className="deal-title">카드 할인</p>
            {store.card_benefits.map((benefit) => (
              <div key={benefit.id} className="offer-row card-row">
                <span className="card-name">{benefit.card_name}</span>
                {renderCardRegistration(benefit.card_id, benefit.card_name)}
                <strong>{benefitLabel(benefit)}</strong>
                <span className="warn-badge">{benefit.recommendation_status === 'approved' ? '원문 확인 완료 · 이용 조건 확인 필요' : '적용 조건·최신 정보 확인 필요'}</span>
                {benefit.min_payment_raw && <small>최소 결제금액 · {benefit.min_payment_raw}</small>}
                {!benefit.min_payment_raw && benefit.min_payment_krw !== null && <small>최소 결제금액 · {benefit.min_payment_krw.toLocaleString('ko-KR')}원</small>}
                {benefit.cap_text && <small>할인 한도 · {benefit.cap_text}</small>}
                {benefit.period_text && <small>적용 기간 · {benefit.period_text}</small>}
                {benefit.source_url && <a href={benefit.source_url} target="_blank" rel="noopener noreferrer">카드 혜택 원문</a>}
                {benefit.excludes_simple_pay && (
                  <span className="excl-badge">간편결제 제외</span>
                )}
                {benefit.conditions.length > 0 && (
                  <small>{benefit.conditions.join(" · ")}</small>
                )}
              </div>
            ))}
          </div>
        )}
        {(store.card_programs ?? []).map(program => (
          <div key={`${program.version_id}:${program.rule_key}`} className="deal-group">
            <p className="deal-title">{program.card_name} 캐시백</p>
            {renderCardRegistration(program.card_id, program.card_name)}
            {program.tiers.map(tier => <p key={tier.min_payment_krw}>
              {tier.min_payment_krw.toLocaleString('ko-KR')}원 이상
              {tier.max_payment_exclusive_krw !== null ? ` ${tier.max_payment_exclusive_krw.toLocaleString('ko-KR')}원 미만` : ''}
              {' · '}{tier.amount_krw.toLocaleString('ko-KR')}원 캐시백
            </p>)}
            <p>같은 영역에서 하루 {program.limit_group.daily_count_cap}회, 월 {program.limit_group.monthly_count_cap}회 · 월 최대 {program.limit_group.monthly_amount_cap_krw.toLocaleString('ko-KR')}원</p>
            {program.conditions.map(condition => <small key={condition}>{condition}</small>)}
            <a href={program.source_url} target="_blank" rel="noopener noreferrer">적용 조건 원문</a>
            <small>{new Date(new Date(program.valid_until_exclusive).getTime() - 1).toLocaleDateString('ko-KR', {timeZone: 'Asia/Seoul'})}까지</small>
            <details><summary>제외 조건 확인</summary><p>{program.exclusions}</p></details>
            {myCardIds.includes(program.card_id) && <>
              <label>현재 적용 중인 캐시백 모드
                <select value={programInputs.program_modes[program.program_key] ?? ''} onChange={event => {
                  invalidatePayment(); const value = event.target.value;
                  setProgramInputs(current => {const modes = {...current.program_modes};
                    if (value) modes[program.program_key] = value as 'offline' | 'online' | 'anywhere' | 'donation'; else delete modes[program.program_key];
                    return {...current, program_modes: modes};});
                }}><option value="">모름</option><option value="offline">오프라인</option><option value="online">온라인</option><option value="anywhere">어디서나</option><option value="donation">기부</option></select>
              </label>
              <label>이 모드가 적용된 날짜 (한국 시간 0시)
                <input type="date" value={programInputs.program_effective_at[program.program_key]
                  ? new Date(new Date(programInputs.program_effective_at[program.program_key]).getTime() + 9 * 3600000).toISOString().slice(0, 10) : ''}
                  onChange={event => {invalidatePayment(); const value = event.target.value;
                  setProgramInputs(current => {const times = {...current.program_effective_at};
                    if (value && !Number.isNaN(new Date(value).valueOf())) times[program.program_key] = new Date(`${value}T00:00:00+09:00`).toISOString(); else delete times[program.program_key];
                    return {...current, program_effective_at: times};});}} />
              </label>
              {(['program_daily_uses', 'program_monthly_uses'] as const).map(field => {
                const key = field === 'program_daily_uses' ? program.daily_key : program.monthly_key;
                return <label key={field}>{field === 'program_daily_uses' ? '오늘' : '이번 달'} 이 영역에서 받은 횟수 (모르면 비워두세요)
                  <input type="number" min="0" step="1" value={programInputs[field][key] ?? ''} onChange={event => {
                    invalidatePayment(); const value = event.target.value;
                    setProgramInputs(current => {const counts = {...current[field]}; if (value === '') delete counts[key]; else counts[key] = Number(value); return {...current, [field]: counts};});
                  }} /></label>;
              })}
              <label>오늘 이 영역의 첫 결제인가요?
                <select value={programInputs.program_first_transaction[program.daily_key] === undefined ? '' : String(programInputs.program_first_transaction[program.daily_key])}
                  onChange={event => {invalidatePayment(); const value = event.target.value;
                    setProgramInputs(current => {const first = {...current.program_first_transaction}; if (!value) delete first[program.daily_key]; else first[program.daily_key] = value === 'true'; return {...current, program_first_transaction: first};});
                  }}><option value="">모름</option><option value="true">예</option><option value="false">아니요</option></select>
              </label>
              <label>이번 달 이 영역의 남은 캐시백 금액 (모르면 비워두세요)
                <input type="number" min="0" value={remainingCaps[program.monthly_key] ?? ''} onChange={event => updateRemainingCap(program.monthly_key, event.target.value)} />
              </label>
            </>}
          </div>
        ))}
        {!hasDiscount && !store.local_benefits?.length && <p className="deal-empty">현재 등록된 상세 혜택이 없습니다.</p>}
        {(hasDiscount || !!store.local_benefits?.length) && renderPaymentCalculator(store)}
      </div>
    );
  }

  const detail = selected && (
    <section
      className="map-store-card"
      aria-labelledby="store-detail-title"
      onKeyDown={event => { if (event.key === "Escape") setSelected(null); }}
      style={{
        "--category-color": selected.local_benefits?.[0]
          ? selected.color ?? CATEGORY_META[selected.category].color
          : CATEGORY_META[selected.category].color,
      } as CSSProperties}
      aria-live="polite"
    >
      <div className="store-detail-header">
        <button className="map-detail-back" type="button" onClick={() => setSelected(null)}>← 목록으로</button>
        <p className="map-popup-brand">
          {selected.local_benefits?.[0]
            ? selected.local_benefits[0].programs.map((program) => PROGRAM_LABELS[program]).join(" · ")
            : CATEGORY_META[selected.category].label}
        </p>
        <h3 id="store-detail-title" tabIndex={-1}>
          {selected.brand}
          {!selected.local_benefits?.[0] && ` ${selected.branch}`}
        </h3>
        <p className="address">{selected.address ?? "장소검색으로 위치 확인"}</p>
        {selected.distance_m !== null && <span className="detail-distance">현재 지도 중심에서 {Math.round(selected.distance_m)}m</span>}
      </div>
      <div className="store-detail-body">{renderDeals(selected)}</div>
    </section>
  );

  const list = (
    <>
      <div className="map-sidebar-head">
        <div className="map-result-tabs" role="group" aria-label="주변 목록 보기">
          <button type="button" aria-pressed={resultMode === 'all'} onClick={() => setResultMode('all')}>혜택 매장</button>
          <button type="button" aria-pressed={resultMode === 'budget'} onClick={() => setResultMode('budget')}>가성비 식당</button>
        </div>
        <div className="map-sidebar-summary">
          <h2>주변 {resultMode === 'budget' ? '식당' : '혜택'} <span>{resultStores.length}곳</span></h2>
          <label>
            <span className="sr-only">정렬</span>
            <select value={sort} onChange={(event) => setSort(event.target.value as "distance" | "discount")}>
              <option value="distance">가까운순</option>
              <option value="discount">할인 높은순</option>
            </select>
          </label>
        </div>
        {resultMode === 'budget' && <div className="map-price-filter" role="group" aria-label="가격 필터">
          {[null, 6000, 8000, 10000].map((price) => (
            <button key={price ?? 'all'} type="button" aria-pressed={maxPrice === price} onClick={() => setMaxPrice(price)}>
              {price === null ? '전체' : `${price.toLocaleString('ko-KR')}원 이하`}
            </button>
          ))}
        </div>}
      </div>
      {apiError && <p className="notice error">{apiError}</p>}
      {(loading || localLoading) && <p className="loading-note">혜택을 불러오는 중…</p>}
      {resultMode === 'budget' && unlocatedBudget > 0 && <p className="budget-location-note" role="status">위치가 확인된 식당만 지도에 표시합니다.</p>}
      <div className="map-place-list" aria-label={resultMode === 'budget' ? '가성비 식당 목록' : '주변 혜택 목록'}>
        {resultStores.slice(0, listLimit).map(store => {
          const price = store.local_benefits?.filter(b => b.discount_type === 'menu_price').sort((a, b) => (a.discount_value ?? Infinity) - (b.discount_value ?? Infinity))[0];
          return <button key={store.id} type="button" className="map-place-item" onClick={() => {
            openStore(store);
            mapObj.current?.setCenter(new window.kakao.maps.LatLng(store.lat, store.lng));
          }}>
            <span className="place-list-icon" aria-hidden="true">{price ? '🍚' : CATEGORY_META[store.category].icon}</span>
            <span className="place-list-copy"><strong>{store.brand}{!store.local_benefits?.length && ` ${store.branch}`}</strong><small>{store.address || '등록 위치 확인'}</small>{store.distance_m !== null && <small className="place-list-distance">{Math.round(store.distance_m)}m</small>}</span>
            <span className="place-list-value">{price ? <><b>{(price.discount_value ?? 0).toLocaleString('ko-KR')}원</b><small>{price.benefit_text}</small></> : topRate(store) > 0 ? <><b>최대 {topRate(store)}%</b><small>{benefitSource(store)}</small></> : <><b>혜택 보기</b><small>{benefitSource(store)}</small></>}</span>
          </button>;
        })}
        {!loading && !localLoading && resultStores.length === 0 && <div className="map-list-empty"><strong>{resultMode === 'budget' ? '이 주변에 등록된 가성비 식당이 없어요' : '이 주변에는 내 카드·결제수단 혜택이 없어요'}</strong><p>{resultMode === 'budget' ? '지도를 이동해 이 지역에서 다시 검색해 보세요.' : '지도를 이동하거나 카드 혜택 탐색에서 보유 카드를 추가해 보세요.'}</p></div>}
      </div>
      {resultStores.length > listLimit && <button type="button" className="map-list-more" onClick={() => setListLimit(n => n + 8)}>더 보기 ({resultStores.length - listLimit})</button>}
      <p className="map-sidebar-notice">{resultMode === 'budget' ? '가격 정보 출처: 행정안전부 착한가격업소 현황(공공데이터포털). 대표 메뉴 기준이며 실제 가격은 매장에서 확인해 주세요.' : '실적·한도·지점 조건에 따라 적용 여부가 달라집니다. 실제 적용 조건은 각 운영기관 공지를 확인해 주세요.'}</p>
    </>
  );

  return (
    <div className="map-page">
      <h1 className="sr-only">혜택 지도</h1>
      <aside className="map-sidebar" aria-label="주변 혜택 탐색">
        {cardSaveNotice && <p className="notice" role="status">{cardSaveNotice}</p>}
        {cardSaveError && <p className="notice error" role="alert">{cardSaveError}</p>}
        {detail || list}
      </aside>
      <div className="map-stage">
        <div ref={mapRef} className="map-canvas" aria-label="주변 할인 지도" />
        {sdkError && <p className="notice error map-sdk-error">{sdkError}</p>}
        <div className="map-toolbar" role="group" aria-label="업종 필터">
          <button type="button" className="map-chip" aria-pressed={activeCat === null} onClick={() => setActiveCat(null)}>전체</button>
          {categories
            .filter((category) => category.value !== "other")
            .map((category) => (
              <button key={category.value} type="button" className="map-chip" aria-pressed={activeCat === category.value} onClick={() => setActiveCat(category.value)}>
                {CATEGORY_META[category.value].icon} {category.label}
              </button>
            ))}
        </div>
        <button className="map-area-search" type="button" onClick={searchMapArea}>이 지역 더 보기</button>
        <div className="map-side-controls">
          <button type="button" aria-label="지도 확대" title="확대" onClick={() => mapObj.current?.setLevel(mapObj.current.getLevel() - 1)}>+</button>
          <button type="button" aria-label="지도 축소" title="축소" onClick={() => mapObj.current?.setLevel(mapObj.current.getLevel() + 1)}>−</button>
          <button type="button" aria-label="내 위치로 이동" title="내 위치" onClick={useMyLocation}>◎</button>
          <button type="button" aria-label="학교 주변으로 이동" title="학교 주변" onClick={() => setMapCenter(DEFAULT_CENTER)}>⌂</button>
        </div>
      </div>
    </div>
  );
}
