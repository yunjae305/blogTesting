import { useId, type ReactNode } from "react";

/**
 * **도우미 카드** — 화면을 처음 여는 사람이 옆에 두고 읽는 자리.
 *
 * 자동 포스팅에서 먼저 만들었고(2026-08-12 사용자 시안), 새 글 작성이 같은 것을 쓰게
 * 되면서 껍데기만 여기로 옮겼다. 담는 내용은 화면마다 다르다 —
 * `scheduled/BulkHelperCard`, `write/WriteHelperCard`.
 *
 * 종이(`.summary-note`)·테이프(`.summary-tape`)는 새 글 작성의 「이 글 요약」과 같은 것을
 * 그대로 쓴다. 옆에 놓이는 카드가 같은 물건으로 읽혀야 하기 때문이다.
 *
 * 목록의 생김새는 이 카드만의 것이다. 칸마다 그림 하나를 왼쪽에 두고, 그 오른쪽에 이름과
 * 설명을 위아래로 놓는다. 요약 카드처럼 이름과 값을 좌우로 벌려 놓으면 설명이 길어질 때
 * 오른쪽에서 접혀 읽기 어려웠다.
 */

/**
 * 칸에 쓰는 그림. 모두 같은 규격이다(24 격자·선만·굵기와 색은 CSS가 준다).
 *
 * 한곳에 모아 두는 이유: 화면마다 따로 그리면 같은 뜻에 다른 그림이 붙는다. 예를 들어
 * '소재'는 두 화면 모두 전구다.
 */
export const HELPER_ICON = {
  /** 종이 한 장. */
  page: (
    <>
      <path d="M7 3.7h6.3L17 7.4v12.9H7z" />
      <path d="M13.3 3.7v3.7H17" />
      <path d="M9.6 11.2h4.8M9.6 14h4.8M9.6 16.8h3" />
    </>
  ),
  /** 재생 단추 — 일이 시작되는 자리. */
  play: (
    <>
      <circle cx="12" cy="12" r="8.4" />
      <path d="M10.4 9.1 15 12l-4.6 2.9z" />
    </>
  ),
  /** 전구 — 무엇을 쓸지 떠올리는 자리(소재). */
  bulb: (
    <>
      <path d="M12 3.8a5.2 5.2 0 0 1 3.1 9.4v1.6H8.9v-1.6A5.2 5.2 0 0 1 12 3.8z" />
      <path d="M9.9 17.2h4.2M10.7 19.6h2.6" />
    </>
  ),
  /** 이름표 — 분야·카테고리. */
  tag: (
    <>
      <path d="M11.6 3.9h7.2a1.3 1.3 0 0 1 1.3 1.3v7.2a1.3 1.3 0 0 1-.4.9l-6.4 6.4a1.3 1.3 0 0 1-1.8 0l-6.7-6.7a1.3 1.3 0 0 1 0-1.8l6.4-6.4a1.3 1.3 0 0 1 .9-.4z" />
      <circle cx="15.9" cy="8.1" r="1.5" />
    </>
  ),
  /** 지구 — 어디에 올릴까. */
  globe: (
    <>
      <circle cx="12" cy="12" r="8.4" />
      <path d="M3.6 12h16.8" />
      <path d="M12 3.6c2.2 2.3 3.4 5.3 3.4 8.4S14.2 18.1 12 20.4c-2.2-2.3-3.4-5.3-3.4-8.4S9.8 5.9 12 3.6z" />
    </>
  ),
  /** 요술봉 — 알아서 만든다. */
  wand: (
    <>
      <path d="M4.6 19.9 13.2 11.3" />
      <path d="m16 4.7.9 2.4 2.4.9-2.4.9-.9 2.4-.9-2.4-2.4-.9 2.4-.9z" />
      <path d="m7.4 4.6.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5z" />
      <path d="m18.9 15.1.4 1.1 1.1.4-1.1.4-.4 1.1-.4-1.1-1.1-.4 1.1-.4z" />
    </>
  ),
  /** 오르는 막대 — 진행 상황. */
  chart: (
    <>
      <path d="M4.4 19.8h15.2" />
      <rect x="5.6" y="13.6" width="3.2" height="4.3" rx="0.9" />
      <rect x="10.4" y="11" width="3.2" height="6.9" rx="0.9" />
      <rect x="15.2" y="14.8" width="3.2" height="3.1" rx="0.9" />
      <path d="M13.4 8.4 19 4.6" />
      <path d="M15.9 4.4H19v3" />
    </>
  ),
  /** 계단 — 걸음을 차례로 밟는다. */
  steps: (
    <>
      <path d="M3.8 19.8h4.3v-4.2h4.3v-4.2h4.3V7.2h3.5" />
      <path d="M17.1 4.6h3.1v3.1" />
    </>
  ),
  /** 과녁 — 이 글로 무엇을 이룰까(글 목적). */
  target: (
    <>
      <circle cx="12" cy="12" r="8.4" />
      <circle cx="12" cy="12" r="4.6" />
      <circle cx="12" cy="12" r="1.3" />
    </>
  ),
  /** 사람 둘 — 누가 읽을까(대상 연령). */
  people: (
    <>
      <circle cx="9.4" cy="8.6" r="3.1" />
      <path d="M3.9 19.5c0-3.1 2.5-5 5.5-5s5.5 1.9 5.5 5" />
      <path d="M15.4 6.1a3.1 3.1 0 0 1 0 5.9" />
      <path d="M16.7 14.9c2 .6 3.4 2.2 3.4 4.6" />
    </>
  ),
  /** 클립 — 곁들여 붙이는 것(브랜드·참고 자료). */
  clip: (
    <path d="M18.4 11.3 12 17.7a3.7 3.7 0 0 1-5.2-5.2l7.1-7.1a2.5 2.5 0 0 1 3.5 3.5l-7.1 7.1a1.2 1.2 0 0 1-1.8-1.8l6.4-6.4" />
  ),
  /** 시계 — 언제 만들까. */
  clock: (
    <>
      <circle cx="12" cy="12" r="8.4" />
      <path d="M12 7.3V12l3.3 2" />
    </>
  ),
  /** 되돌아가는 화살표 — 다시 시도한다. */
  retry: (
    <>
      <path d="M20.1 12a8.1 8.1 0 1 1-2.6-6" />
      <path d="M20.4 4.3v4.3h-4.3" />
    </>
  ),
  /** 확인 목록 — 후보 중에서 고른다. */
  checklist: (
    <>
      <path d="M4.4 7.6 6.2 9.4 9.5 6.1" />
      <path d="M4.4 15.6 6.2 17.4 9.5 14.1" />
      <path d="M12.4 7.8h7.2M12.4 15.8h7.2" />
    </>
  ),
} satisfies Record<string, ReactNode>;

/** 칸 하나: 이름 → 그 칸이 하는 일 → 곁에 둘 그림. */
export type HelperRow = { key: string; value: string; icon: ReactNode };

export function HelperCard({ rows }: { rows: HelperRow[] }) {
  // 한 화면에 도우미가 둘 이상 놓일 수 있다(새 글 작성은 요약 카드와 나란히 선다).
  const titleId = useId();

  return (
    <section className="panel summary-note helper-note" aria-labelledby={titleId}>
      {/* 포스트잇을 붙여 둔 테이프. 자리를 알려 주는 것이라 읽어 주지 않는다. */}
      <span className="summary-tape" aria-hidden="true" />
      <div className="panel-header helper-note-header">
        <div className="panel-heading-copy">
          <p className="panel-kicker helper-note-kicker">GUIDE</p>
          <h2 className="panel-title helper-note-title" id={titleId}>
            도우미
          </h2>
        </div>
        <span className="helper-note-badge" aria-hidden="true">
          <svg viewBox="0 0 24 24" focusable="false">
            {HELPER_ICON.bulb}
            <path d="M18.6 4.2v2.2M17.5 5.3h2.2M4.6 9.4v1.6M3.8 10.2h1.6" />
          </svg>
        </span>
      </div>
      <div className="panel-body">
        <ul className="helper-note-list">
          {rows.map((row) => (
            <li className="helper-note-row" key={row.key}>
              <span className="helper-note-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" focusable="false">{row.icon}</svg>
              </span>
              <p className="helper-note-key">{row.key}</p>
              <p className="helper-note-value">{row.value}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
