/**
 * 예약 포스팅 화면에서만 쓰는 인라인 SVG 모음. 이 저장소는 아이콘 라이브러리를 쓰지
 * 않고 컴포넌트 안에 svg를 직접 적는다(App.tsx의 NavIcon, HomeView의 StatIcon과 같은 방식).
 * stroke·크기는 app.css의 `.panel-title svg` 규칙이 잡아 주므로 여기서는 모양만 그린다.
 */

type IconProps = { className?: string };

const common = {
  viewBox: "0 0 24 24",
  "aria-hidden": true,
  focusable: false,
} as const;

/** 소재 입력 — 클립보드(메모장) */
export function ClipboardIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <rect x="5" y="4.5" width="14" height="16" rx="2.5" />
      <path d="M9 3.5h6v3H9zM8.5 11h7M8.5 15h4.5" />
    </svg>
  );
}

/** 스마트 스케줄링 — 시계 */
export function ClockIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 7.5V12l3 2" />
    </svg>
  );
}

/** 스케줄 시작 — 재생 */
export function PlayIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <path d="M8.5 5.8v12.4l10-6.2z" />
    </svg>
  );
}

/** 일시정지 */
export function PauseIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <path d="M9.5 5.5v13M14.5 5.5v13" />
    </svg>
  );
}

/** 정지 */
export function StopIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" />
    </svg>
  );
}

/** 작업 큐 / 작업 현황 — 막대그래프 */
export function BarsIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <path d="M5 19h14" />
      <path d="M7.5 19v-6M12 19V6.5M16.5 19v-9" />
    </svg>
  );
}

/** 미리보기 — 눈 */
export function EyeIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <path d="M2.8 12S6.4 6 12 6s9.2 6 9.2 6-3.6 6-9.2 6-9.2-6-9.2-6Z" />
      <circle cx="12" cy="12" r="2.6" />
    </svg>
  );
}

/** 작업 삭제 — 휴지통 */
export function TrashIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <path d="M4.5 6.5h15M9.5 6.5V4.8h5v1.7" />
      <path d="M6.5 6.5 7.4 20h9.2l.9-13.5" />
      <path d="M10.2 10v6.3M13.8 10v6.3" />
    </svg>
  );
}

/** 네이버 표식. 초록 사각형 안의 흰 N — 네이버 로고 형태를 글자로 흉내 낸다. */
export function NaverMark({ className }: IconProps) {
  return (
    <span className={`scheduled-mark scheduled-mark--naver ${className ?? ""}`.trim()} aria-hidden="true">
      N
    </span>
  );
}

/** 스레드 표식. 검은 사각형 안의 스레드 글리프. */
export function ThreadsMark({ className }: IconProps) {
  return (
    <span
      className={`scheduled-mark scheduled-mark--threads ${className ?? ""}`.trim()}
      aria-hidden="true"
    >
      <svg viewBox="0 0 24 24" focusable="false">
        <path d="M16.4 8.2c-.8-1.9-2.4-2.9-4.5-2.9C8.3 5.3 6.2 7.8 6.2 12s2.1 6.8 5.7 6.8c3.3 0 5.2-1.9 5.2-4.4 0-2.2-1.7-3.6-4.2-3.6-1.8 0-3 .9-3 2.1 0 1 .8 1.7 1.9 1.7 1.7 0 2.8-1.3 2.8-3.6" />
      </svg>
    </span>
  );
}

/** 소재 추가 — 더하기 */
export function PlusIcon({ className }: IconProps) {
  return (
    <svg {...common} className={className}>
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  );
}
