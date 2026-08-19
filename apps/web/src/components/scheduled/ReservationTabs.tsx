import { BarsIcon, ClockIcon } from "./icons";

export type ReservationTab = "queue" | "history";

type Props = {
  current: ReservationTab;
  onSelect: (tab: ReservationTab) => void;
  /** 작업 큐에 들어 있는 작업 수. 0이어도 보여 준다 — 비어 있다는 것도 정보다. */
  queueCount: number;
};

/**
 * 예약 화면의 두 탭.
 *
 * '새 예약 만들기'는 없앴다(2026-08-11) — 예약은 이제 새 글 작성에서 작업 시각을 적어
 * 거는 것이고, 이 화면은 **걸어 둔 일을 보는 곳**이다.
 */
export function ReservationTabs({ current, onSelect, queueCount }: Props) {
  const tabs = [
    { id: "queue" as const, label: "작업 큐", icon: <BarsIcon />, count: queueCount },
    { id: "history" as const, label: "발행 내역", icon: <ClockIcon />, count: null },
  ];

  return (
    <div className="reservation-tabs" role="tablist" aria-label="예약작업 관리 화면">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={current === tab.id}
          className={`reservation-tab ${current === tab.id ? "is-current" : ""}`.trim()}
          onClick={() => onSelect(tab.id)}
        >
          <span className="reservation-tab-icon" aria-hidden="true">
            {tab.icon}
          </span>
          {tab.label}
          {tab.count !== null && <span className="reservation-tab-count">{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}
