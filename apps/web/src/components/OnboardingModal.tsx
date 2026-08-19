import { useEffect, useRef } from "react";

import { useStore } from "../store";

/**
 * 설정 화면에 **실제로 있는 칸**(2026-08-12 사용자 지시: "설정에 보면 내용을 선택 및
 * 입력을 할 수가 있어. 그거 반영해서 팝업창 ui 수정해").
 *
 * 번호·제목·설명을 설정 화면(`SettingsView`·`NaverConnect`·`ThreadsConnect`)에서 그대로
 * 가져왔다. 두 화면이 다른 말을 하면 안내가 안내를 못 한다 — 예전 팝업에는 설정에 없는
 * '포스팅 방식'이 적혀 있었고, 정작 계정 두 칸은 빠져 있었다.
 *
 * `detail`은 그 칸에서 **무엇을 고르고 적는지**를 적는다. 설명을 그대로 옮기면
 * "관리합니다"처럼 뭉뚱그린 말이 되어, 처음 온 사람이 할 일을 알 수 없다.
 */
const SETTINGS_STEPS = [
  {
    n: "01",
    title: "글 생성 기본값",
    detail: "해시태그 수 · 원고 길이 · 소재와 트렌드의 반영 비율",
  },
  { n: "02", title: "기본 페르소나", detail: "글 전체에 적용할 관점과 말투를 고릅니다" },
  { n: "03", title: "Naver 계정", detail: "아이디와 비밀번호 — 자동 발행에 씁니다" },
  { n: "04", title: "Threads 계정", detail: "함께 올릴 때만 필요합니다" },
  { n: "05", title: "커스텀 페르소나", detail: "직접 만들 때만 — 이름·설명·작성 지침" },
];

/**
 * Shown once, on the first visit of an account that has saved no settings yet.
 * Same shell as the verify popup, in sky blue rather than pink.
 */
export function OnboardingModal() {
  const { onboardingOpen, dismissOnboarding, setRoute } = useStore();
  const startRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!onboardingOpen) return;

    startRef.current?.focus();
    document.body.classList.add("onboarding-open");

    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismissOnboarding();
    };
    window.addEventListener("keydown", onEscape);

    return () => {
      document.body.classList.remove("onboarding-open");
      window.removeEventListener("keydown", onEscape);
    };
  }, [dismissOnboarding, onboardingOpen]);

  if (!onboardingOpen) return null;

  return (
    <div
      className="verify-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) dismissOnboarding();
      }}
    >
      <section
        className="verify-dialog onboarding-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboardingTitle"
      >
        <div className="verify-dialog-header">
          <div>
            <p className="verify-kicker">WELCOME TO BLOG·IT</p>
            <h2 id="onboardingTitle">글쓰기 기본 설정</h2>
          </div>
        </div>

        <div className="verify-dialog-body">
          <p className="onboarding-description">
            첫 글을 쓰기 전에 설정 화면에서 아래 다섯 칸을 채워 주세요. 한 번 정해 두면 앞으로
            만드는 모든 글에 그대로 적용됩니다.
          </p>
          <ol className="onboarding-benefits" aria-label="설정 화면의 항목">
            {SETTINGS_STEPS.map((step) => (
              <li key={step.n}>
                <span>{step.n}</span>
                <div>
                  <strong>{step.title}</strong>
                  <small>{step.detail}</small>
                </div>
              </li>
            ))}
          </ol>
          {/* 계정 정보를 어디에 두는지 먼저 말해 준다 — 처음 온 사람이 아이디·비밀번호를
              적어야 하는 화면에서 가장 먼저 묻는 것이다. */}
          <p className="onboarding-note">
            계정 정보는 서비스 DB에 저장하지 않고 이 PC에만 암호화해 보관합니다.
          </p>
        </div>

        <div className="verify-dialog-actions">
          {/* '나중에 할게요'는 없앴다(2026-08-12 사용자 지시). 설정이 비어 있으면 첫 글에서
              페르소나·해시태그 수가 정해지지 않은 채로 시작한다 — 미루라고 권할 자리가
              아니다. 닫을 길 자체가 사라지지는 않는다: 바깥을 누르거나 Esc를 누르면 닫힌다. */}
          <button
            className="button primary"
            type="button"
            ref={startRef}
            onClick={() => {
              dismissOnboarding();
              setRoute("settings");
            }}
          >
            설정하러 가기
          </button>
        </div>
      </section>
    </div>
  );
}
