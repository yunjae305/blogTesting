/**
 * 글 목적과 대상 연령 선택 — 작성 화면과 브랜드 글쓰기가 **같은 것**을 쓴다.
 *
 * 원래 `StepTopic` 안에만 있었다. 브랜드 글쓰기에도 같은 칸이 필요해지면서 복사하는
 * 대신 여기로 옮겼다 — 복사하면 아이콘 하나, 문구 하나가 어긋나기 시작하고, 목적
 * 목록이 바뀔 때 한쪽만 고치게 된다.
 *
 * 상태는 갖지 않는다. 고른 값과 바꾸는 함수를 부모가 준다.
 */

import { READER_AGE_RANGES, WRITING_PURPOSES } from "../../constants";

function PurposeIcon({ purpose }: { purpose: string }) {
  const common = {
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;

  if (purpose === "정보 전달") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 10.5v5M12 7.5h.01" />
      </svg>
    );
  }
  if (purpose === "입문·소개") {
    return (
      <svg {...common}>
        <path d="M4.5 5.5c2.8-.8 5.3-.2 7.5 1.7v11c-2.2-1.9-4.7-2.5-7.5-1.7v-11Z" />
        <path d="M19.5 5.5c-2.8-.8-5.3-.2-7.5 1.7v11c2.2-1.9 4.7-2.5 7.5-1.7v-11Z" />
      </svg>
    );
  }
  if (purpose === "일상·경험 공유") {
    return (
      <svg {...common}>
        <path d="M12 19s-7-4.4-7-9.2A3.8 3.8 0 0 1 12 7.7a3.8 3.8 0 0 1 7 2.1C19 14.6 12 19 12 19Z" />
      </svg>
    );
  }
  if (purpose === "사용법·가이드") {
    return (
      <svg {...common}>
        <path d="M6 19V5M6 7h11l-2.5 3L17 13H6" />
      </svg>
    );
  }
  if (purpose === "후기·리뷰 작성") {
    return (
      <svg {...common}>
        <path d="m12 4 2.4 4.8 5.3.8-3.8 3.7.9 5.3-4.8-2.5-4.8 2.5.9-5.3L4.3 9.6l5.3-.8L12 4Z" />
      </svg>
    );
  }
  if (purpose === "비교·추천") {
    return (
      <svg {...common}>
        <path d="M4 7h16M7 7l-3 5h6L7 7Zm10 0-3 5h6l-3-5ZM12 5v14M8 19h8" />
      </svg>
    );
  }
  if (purpose === "문제 해결") {
    return (
      <svg {...common}>
        <path d="m5 13 4 4L19 7" />
        <path d="M19 12a7 7 0 1 1-4-6.3" />
      </svg>
    );
  }
  if (purpose === "트렌드·이슈 소개") {
    return (
      <svg {...common}>
        <path d="M5 18V6M5 18h14M8 14l3-3 3 2 4-5" />
        <path d="M15 8h3v3" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M4 13h3l9 4V7l-9 4H4v2Z" />
      <path d="M7 13v5h3M19 9.5a4 4 0 0 1 0 5" />
    </svg>
  );
}

function AudienceIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="8" r="3" />
      <path d="M6.5 19c.4-4 2.2-6 5.5-6s5.1 2 5.5 6" />
    </svg>
  );
}

export function PurposeChoices({
  purpose,
  customPurpose,
  onPurpose,
  onCustomPurpose,
}: {
  purpose: string;
  customPurpose: string;
  onPurpose: (value: string) => void;
  onCustomPurpose: (value: string) => void;
}) {
  return (
    <div
      className="purpose-grid brief-purpose-grid"
      role="radiogroup"
      aria-label="글 목적"
      aria-required="true"
    >
      {WRITING_PURPOSES.map((item) => (
        <button
          key={item}
          className="purpose-option"
          type="button"
          role="radio"
          aria-checked={!customPurpose.trim() && purpose === item}
          data-purpose={item}
          onClick={() => {
            onPurpose(item);
            onCustomPurpose("");
          }}
        >
          <span className="brief-choice-icon" aria-hidden="true">
            <PurposeIcon purpose={item} />
          </span>
          <span className="brief-choice-label">{item}</span>
        </button>
      ))}
      {/* 기타는 목적 카드 다음 선택지다 — 카드를 누르면 바로 쓸 수 있고, 쓰는 순간
          선택된다. 별도 접이식·별도 구역을 두지 않는다. */}
      <label
        className="purpose-option purpose-option--custom"
        role="radio"
        aria-checked={Boolean(customPurpose.trim())}
        data-purpose="기타"
      >
        <span className="brief-choice-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" aria-hidden focusable={false}>
            <path d="M4 20l4.5-1L19 8.5a2.1 2.1 0 0 0-3-3L5.5 16 4 20z" />
            <path d="M13.5 6.5l3 3" />
          </svg>
        </span>
        <span className="brief-choice-label">기타</span>
        <input
          id="customPurpose"
          aria-label="글 목적 직접 입력"
          placeholder="예: 채용 브랜딩 콘텐츠로 활용"
          value={customPurpose}
          onChange={(event) => onCustomPurpose(event.target.value)}
        />
      </label>
    </div>
  );
}

export function AudienceChoices({
  ageRange,
  onChange,
}: {
  /** null은 "아직 고르지 않음", ""은 "전체"라는 실제 선택이다. 둘은 다르다. */
  ageRange: string | null;
  onChange: (value: string) => void;
}) {
  return (
    <div
      className="age-grid brief-audience-grid"
      role="radiogroup"
      aria-label="대상 연령"
      aria-required="true"
    >
      {READER_AGE_RANGES.map((item) => (
        <button
          key={item.value || "all"}
          className="age-option"
          type="button"
          role="radio"
          aria-checked={ageRange === item.value}
          data-reader-age-range={item.value}
          onClick={() => onChange(item.value)}
        >
          <span className="brief-choice-icon" aria-hidden="true">
            <AudienceIcon />
          </span>
          <span className="brief-choice-label">{item.label}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * 고른 연령이면 글이 어떻게 달라지는지.
 *
 * 예전에는 카드마다 한 줄씩 붙여 뒀는데, 다섯 개를 한꺼번에 깔면 고르기 전에 읽을
 * 것이 너무 많다. **고른 뒤에 그 하나만** 보여 준다(2026-08-07 사용자 요청).
 *
 * 아직 고르지 않았으면 아무것도 그리지 않는다 — 빈 칸을 잡아 두면 고르는 순간
 * 아래 내용이 밀려 올라간다.
 */
export function AudienceNote({ ageRange }: { ageRange: string | null }) {
  const chosen =
    ageRange === null ? undefined : READER_AGE_RANGES.find((item) => item.value === ageRange);

  // 아직 고르지 않았어도 **같은 크기의 한 줄**을 채워 둔다(2026-08-11 사용자 지적).
  //
  // 빈 상자로 자리만 잡아 봤지만 여전히 어긋났다 — 글자가 없는 상자와 한 줄이 든 상자의
  // 높이를 CSS로 맞추는 것은 글꼴·줄 간격에 기대는 일이라 화면마다 어긋난다. 실제 문장을
  // 넣으면 두 상태가 **같은 방식으로** 높이를 얻으므로 어긋날 수가 없다.
  //
  // 게다가 이 자리에 쓸모 있는 말이 생긴다: 고르기 전에는 무엇이 달라지는지 알려 준다.
  if (!chosen) {
    return (
      <p className="brief-audience-note is-empty">
        고른 세대에 맞춰 설명 수준과 사례가 달라집니다.
      </p>
    );
  }

  // 연령 이름은 적지 않는다(2026-08-11 사용자 요청) — 바로 아래 버튼에 이미 굵게
  // 눌려 있어 같은 말이 두 번이고, 그만큼 설명이 밀려 두 줄이 됐다. title은 한 줄로
  // 잘렸을 때 전체를 볼 수 있게 남긴다.
  return (
    <p className="brief-audience-note" role="status" title={chosen.description}>
      {chosen.description}
    </p>
  );
}
