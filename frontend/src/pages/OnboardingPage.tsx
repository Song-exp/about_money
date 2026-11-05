import { useEffect, useState } from "react";
import { fetchOptions } from "../api";
import SearchSelect from "../components/SearchSelect";
import CardRegistration from "../components/CardRegistration";
import type {
  BenefitProgram,
  Gender,
  MetaOptions,
  PayMethod,
  StudentCredential,
  UserProfileLocal,
} from "../types";

interface Props {
  initial: UserProfileLocal | null;
  onComplete: (profile: UserProfileLocal) => void;
}

const GENDER_LABELS: Record<Gender, string> = {
  male: "남성",
  female: "여성",
};

const STUDENT_CREDENTIAL_OPTIONS: Array<{ value: StudentCredential; label: string }> = [
  { value: "student_card", label: "학생증" },
  { value: "student_tok", label: "톡학생증" },
];

const BENEFIT_PROGRAM_OPTIONS: Array<{ value: BenefitProgram; label: string }> = [
  { value: "khu_alliance", label: "경희대 제휴혜택" },
  { value: "onnuri", label: "온누리상품권" },
  { value: "seoulpay", label: "서울Pay+" },
  { value: "zeropay", label: "제로페이" },
];

export default function OnboardingPage({ initial, onComplete }: Props) {
  const [opts, setOpts] = useState<MetaOptions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [gender, setGender] = useState<Gender | null>(initial?.gender ?? null);
  const [cardIds, setCardIds] = useState<string[]>(
    initial ? initial.cardIds.map(String) : []
  );
  const [telecom, setTelecom] = useState<string[]>(
    initial?.telecom ? [initial.telecom] : []
  );
  const [pays, setPays] = useState<string[]>(initial ? initial.payMethods : []);
  const [studentCredentials, setStudentCredentials] = useState<StudentCredential[]>(
    initial?.studentCredentials ?? []
  );
  const [benefitPrograms, setBenefitPrograms] = useState<BenefitProgram[]>(
    initial?.benefitPrograms ?? []
  );

  useEffect(() => {
    fetchOptions()
      .then(setOpts)
      .catch((fetchError: Error) => setError(fetchError.message));
  }, []);

  useEffect(() => {
    if (!initial) return;
    setGender(initial.gender);
    setCardIds(initial.cardIds.map(String));
    setTelecom(initial.telecom ? [initial.telecom] : []);
    setPays(initial.payMethods);
    setStudentCredentials(initial.studentCredentials);
    setBenefitPrograms(initial.benefitPrograms);
  }, [initial]);

  const selectedMethodCount =
    cardIds.length + pays.length + studentCredentials.length + benefitPrograms.length;
  const canSubmit = gender !== null && selectedMethodCount > 0;

  function toggleCredential(value: StudentCredential) {
    setStudentCredentials((current) =>
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
    );
  }

  function toggleProgram(value: BenefitProgram) {
    setBenefitPrograms((current) =>
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
    );
  }

  function submit() {
    if (!gender || !canSubmit) return;
    const personalProfile = {
      gender,
      cardIds: cardIds.map((value) => Number(value)),
      telecom: telecom[0] ?? null,
      payMethods: pays as PayMethod[],
      studentCredentials,
      benefitPrograms,
    };
    onComplete({
      userId: initial?.userId || crypto.randomUUID(),
      personaId: "me",
      ...personalProfile,
      personalProfile: {
        ...personalProfile,
        cardIds: [...personalProfile.cardIds],
        payMethods: [...personalProfile.payMethods],
        studentCredentials: [...personalProfile.studentCredentials],
        benefitPrograms: [...personalProfile.benefitPrograms],
      },
    });
  }

  return (
    <div className="onboarding" aria-labelledby="onboarding-title">
      <div className="onboarding-inner">
        <header className="onboarding-head">
          <h1 id="onboarding-title" className="sr-only">개인정보와 결제수단 선택</h1>
          <p className="onboarding-sub">
            선택한 내용은 계정에 저장되며 맞춤 화면 구성에 사용됩니다.
          </p>
        </header>

        <section className="demo-connect-card personal-profile-card" aria-labelledby="personal-profile-title">
          <div className="demo-connect-copy">
            <strong id="personal-profile-title">개인정보 입력</strong>
            <p>성별을 선택하면 내 프로필에 저장되어 서비스 전반에서 동일하게 사용됩니다.</p>
          </div>

          <fieldset className="gender-fieldset">
            <legend>성별</legend>
            <div className="gender-options" role="group" aria-label="성별 선택">
              {(["male", "female"] as Gender[]).map((value) => (
                <button
                  key={value}
                  type="button"
                  className="gender-option"
                  aria-pressed={gender === value}
                  onClick={() => setGender(value)}
                >
                  <span aria-hidden="true">{value === "male" ? "♂" : "♀"}</span>
                  {GENDER_LABELS[value]}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="profile-benefit-fieldset">
            <legend>학생 인증</legend>
            <p>제휴 혜택 확인에 사용할 학생 인증 방법을 선택하세요.</p>
            <div className="profile-choice-grid credential-grid" role="group" aria-label="학생 인증 선택">
              {STUDENT_CREDENTIAL_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className="profile-choice"
                  aria-pressed={studentCredentials.includes(option.value)}
                  onClick={() => toggleCredential(option.value)}
                >
                  <span aria-hidden="true">{option.value === "student_card" ? "▣" : "T"}</span>
                  {option.label}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="profile-benefit-fieldset">
            <legend>제휴·지역화폐</legend>
            <p>지도에서 확인할 가맹점과 혜택 프로그램을 선택하세요.</p>
            <div className="profile-choice-grid program-grid" role="group" aria-label="제휴 및 지역화폐 선택">
              {BENEFIT_PROGRAM_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className="profile-choice"
                  aria-pressed={benefitPrograms.includes(option.value)}
                  onClick={() => toggleProgram(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </fieldset>

        </section>

        <div className="method-guide">
          <strong>내 결제수단</strong>
          <span><i className="method-status-dot imported" />혜택 연결됨</span>
          <span><i className="method-status-dot manual" />직접 선택</span>
        </div>

        {error && <p className="notice error">{error}</p>}
        {!opts && !error && <p className="notice">결제수단을 불러오는 중…</p>}

        {opts && (
          <div className="onboarding-groups">
            <CardRegistration cards={opts.cards} selected={cardIds} onChange={setCardIds} />

            <SearchSelect
              title="통신사"
              items={opts.telecoms.map((item) => ({ key: item, label: item }))}
              selected={telecom}
              onChange={setTelecom}
              multiple={false}
              placeholder="통신사 검색"
            />

            <SearchSelect
              title="온라인 결제(간편결제)"
              items={opts.pay_methods.map((method) => ({
                key: method.value,
                label: method.label,
              }))}
              selected={pays}
              onChange={setPays}
              multiple
              placeholder="결제수단 검색"
            />
          </div>
        )}

        {!canSubmit && opts && (
          <p className="onboarding-validation" role="status">
            {!gender
              ? "성별을 선택해 주세요."
              : "카드·간편결제 또는 학생·지역 혜택을 하나 이상 선택해 주세요."}
          </p>
        )}
      </div>

      <footer className="onboarding-footer">
        <button
          className="primary-button wide"
          type="button"
          onClick={submit}
          disabled={!canSubmit || !opts}
        >
          선택 완료 · 맞춤 할인 지도 보기
          {selectedMethodCount > 0 && <small>{selectedMethodCount}개 선택</small>}
        </button>
      </footer>
    </div>
  );
}
