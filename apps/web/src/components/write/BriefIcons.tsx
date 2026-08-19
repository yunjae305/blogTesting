/**
 * 「언제, 몇 편, 어디에」 상자에 쓰는 그림(2026-08-12 사용자 시안).
 *
 * StepTopic 안에 두지 않는 이유는 길이뿐이다 — 그 파일은 이미 1,200줄이 넘는다.
 * 모두 같은 규격이다(24 격자·선만·굵기와 색은 CSS가 준다).
 */

const ATTRS = { viewBox: "0 0 24 24", "aria-hidden": true, focusable: "false" } as const;

/** 번개 — 지금 바로. */
export const boltIcon = (
  <svg {...ATTRS}>
    <path d="M13.2 3.2 5.6 13.4h5.1l-.9 7.4 7.6-10.2h-5.1z" />
  </svg>
);

/** 달력 — 예약 발행. */
export const calendarIcon = (
  <svg {...ATTRS}>
    <rect x="3.6" y="5.1" width="16.8" height="15.3" rx="2.6" />
    <path d="M3.6 9.6h16.8M8.2 3.4v3.2M15.8 3.4v3.2" />
  </svg>
);

/* 반짝임(sparkleIcon)은 요약 상자의 '발행 요약' 제목 줄에만 쓰였다 — 그 줄을 뺀
   2026-08-12에 함께 지웠다. 다시 필요하면 이 자리에 되살린다. */

/** 종이 — 만들 원고. */
export const docIcon = (
  <svg {...ATTRS}>
    <path d="M7 3.7h6.3L17 7.4v12.9H7z" />
    <path d="M13.3 3.7v3.7H17M9.6 11.4h4.8M9.6 14.2h4.8M9.6 17h3" />
  </svg>
);

/** 종이비행기 — 발행. */
export const sendIcon = (
  <svg {...ATTRS}>
    <path d="M20.6 3.6 3.8 10.4l6.4 2.6 2.6 6.4z" />
    <path d="m10.2 13 4-4" />
  </svg>
);

/** 시계 — 예상 소요 시간. */
export const clockIcon = (
  <svg {...ATTRS}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M12 7.3V12l3.3 2" />
  </svg>
);
