import type { ReactNode } from "react";

import type { ScheduledLogEntry } from "../../api/types";
import { BarsIcon } from "./icons";

/**
 * '작업 현황' 카드의 **껍데기**. 진행률 막대와 로그 목록.
 *
 * 원래 예약 포스팅 안에만 있었다(`TaskStatusCard`). 브랜드 글쓰기도 같은 것을 보여
 * 주기로 해서(2026-08-06 사용자 요청 — "예약포스팅에서 쓰이는 작업현황을 그대로")
 * 껍데기만 여기로 빼고, 무엇을 진행률로 볼지·무엇을 로그로 볼지는 부르는 쪽이 정한다.
 *
 * 예약은 배치의 완료 개수로, 브랜드 글쓰기는 한 글의 상태 이력으로 채운다 — 세는
 * 대상이 다르니 계산을 공유할 수는 없지만, 보이는 것은 같아야 한다.
 */
export function TaskStatusPanel({
  titleId,
  progress,
  summary,
  logs,
  note,
  headerExtra,
  children,
}: {
  titleId: string;
  /** 0-100. 시작 전에는 0이어야 한다 — 100%가 먼저 보이면 안 된다. */
  progress: number;
  /**
   * 막대 바로 아래 한 줄. **진행률과 결과는 다른 말이다** — 작업이 전부 실패해도 진행은
   * 100%라, 막대만 두면 "다 발행됐다"로 읽힌다(2026-08-06 신고). 그래서 부르는 쪽이
   * 완료·실패 개수를 여기에 적는다. 브랜드 글쓰기처럼 셀 것이 없으면 넘기지 않는다.
   */
  summary?: ReactNode;
  /** 최신순으로 넘긴다(부르는 쪽이 정렬한다). */
  logs: ScheduledLogEntry[];
  note: string;
  headerExtra?: ReactNode;
  /** 로그 아래에 덧붙일 것(브랜드 글쓰기의 결과·버튼). */
  children?: ReactNode;
}) {
  return (
    <section className="panel scheduled-panel" aria-labelledby={titleId}>
      <div className="panel-header">
        <h2 className="panel-title" id={titleId}>
          <span className="scheduled-panel-icon" aria-hidden="true">
            <BarsIcon />
          </span>
          작업 현황
        </h2>
        {headerExtra}
        <span className="scheduled-progress-value">{progress}%</span>
      </div>
      <div className="panel-body">
        <div
          className="scheduled-progress"
          role="progressbar"
          aria-label="작업 진행률"
          aria-valuenow={progress}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <span className="scheduled-progress-fill" style={{ width: `${progress}%` }} />
        </div>

        {summary && <div className="scheduled-tally">{summary}</div>}

        <div className="scheduled-log">
          {logs.length > 0 && (
            <ul>
              {logs.map((log, index) => (
                <li key={`${log.at}-${index}`} className={`scheduled-log-line ${log.tone}`}>
                  <span className="scheduled-log-dot" aria-hidden="true" />
                  <span className="scheduled-log-time">{clock(log.at)}</span>
                  <span className="scheduled-log-message">{log.message}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="scheduled-log-note">{note}</p>
        </div>

        {children}
      </div>
    </section>
  );
}

/** 로그 시각은 서버가 준 ISO다. 화면에는 시:분:초만 보여 준다. */
function clock(at: string): string {
  const parsed = new Date(at);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString("ko-KR", { hour12: false });
}
