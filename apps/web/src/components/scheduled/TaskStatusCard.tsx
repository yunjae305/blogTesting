import { useEffect, useState } from "react";

import type { ScheduledBatch, ScheduledJob, ScheduledLogEntry } from "../../api/types";
import { draftLabels, withDraftLabel } from "./draftLabels";
import { TaskStatusPanel } from "./TaskStatusPanel";
import {
  batchProgress,
  countJobStatuses,
  publishesAnywhere,
  runningStageFraction,
} from "./topics";
import type { JobPostState } from "./useScheduledPosting";

/** 목록에 남길 줄 수. 원고 한 편이 수십 줄을 흘리므로 상한이 없으면 화면이 무거워진다. */
const MAX_VISIBLE_LOGS = 120;

type Props = {
  batch: ScheduledBatch | null;
  /** 진행 중인 작업의 단계까지 진행률에 반영하기 위한 작업 목록. */
  jobs: ScheduledJob[];
  /** jobId → 그 작업이 만든 글의 상태. 여기서는 '작업 현황' 줄만 쓴다. */
  jobPosts?: Record<string, JobPostState>;
};

/**
 * 예약 자신의 로그와 **글마다의 작업 현황 줄**을 한 목록으로 합친다(2026-08-10 요청).
 *
 * 예약 로그는 단계 경계에서만 한 줄씩 쌓인다. 그래서 원고를 만드는 5~8분 동안 화면에
 * 새 줄이 하나도 오지 않아 멈춘 것처럼 보였다 — 백엔드는 그동안 실시간으로 돌고 있었다.
 * 새 글 작성 화면이 보여 주는 그 줄들(단계 시작·내레이션)을 같이 실어 그 사이를 채운다.
 *
 * 소재 이름을 앞에 붙인다. 예약은 여러 글이 동시에 도는 자리라, 소재가 없으면 그 줄이
 * 어느 글의 이야기인지 알 수 없다 — 예약 자신의 로그가 이미 그렇게 적고 있다.
 */
export function mergeActivityLogs(
  batchLogs: ScheduledLogEntry[],
  jobs: ScheduledJob[],
  jobPosts: Record<string, JobPostState>,
): ScheduledLogEntry[] {
  // 같은 소재로 여러 편이면 편 번호를 앞에 붙인다(2026-08-12 사용자 요청). 안 붙이면
  // 글자 하나 다르지 않은 줄이 두 번씩 찍혀, 어느 편의 이야기인지도 알 수 없고 화면이
  // 같은 줄을 두 번 그린 것처럼 보인다.
  const labels = draftLabels(jobs);
  const merged: ScheduledLogEntry[] = batchLogs.map((entry) =>
    entry.jobId && labels[entry.jobId]
      ? { ...entry, message: withDraftLabel(entry.message, labels[entry.jobId]) }
      : entry,
  );
  for (const job of jobs) {
    for (const entry of jobPosts[job.jobId]?.activityLog ?? []) {
      merged.push({
        at: entry.at,
        // 진행 내레이션은 예약의 사건 기록보다 뒤에 있는 이야기다 — 흐리게 둔다.
        tone: "muted",
        message: withDraftLabel(`'${job.topic}'의 ${entry.message}`, labels[job.jobId]),
        jobId: job.jobId,
      });
    }
  }
  // 최신순. 같은 시각이면 예약 자신의 줄을 위에 둔다(사건이 내레이션보다 굵은 이야기다).
  merged.sort((left, right) => {
    if (left.at === right.at) return (left.tone === "muted" ? 1 : 0) - (right.tone === "muted" ? 1 : 0);
    return left.at < right.at ? 1 : -1;
  });
  return merged.slice(0, MAX_VISIBLE_LOGS);
}

/** nextRunAt까지 남은 초. 값이 없거나 못 읽으면 null. */
function secondsLeft(nextRunAt: string | null): number | null {
  if (!nextRunAt) return null;
  const due = new Date(nextRunAt).getTime();
  if (Number.isNaN(due)) return null;
  return Math.max(0, Math.ceil((due - Date.now()) / 1000));
}

/**
 * 다음 원고 작업까지 남은 시간을 1초마다 다시 센다.
 *
 * 서버 폴링(2초)과 별개로 화면의 시계로 센다 — 폴링에 맞추면 숫자가 2초씩 건너뛴다.
 * 서버와 브라우저가 같은 PC라 시계 차이는 없다.
 */
function useSecondsLeft(nextRunAt: string | null): number | null {
  const [left, setLeft] = useState<number | null>(() => secondsLeft(nextRunAt));
  useEffect(() => {
    setLeft(secondsLeft(nextRunAt));
    if (!nextRunAt) return;
    const timer = window.setInterval(() => setLeft(secondsLeft(nextRunAt)), 1000);
    return () => window.clearInterval(timer);
  }, [nextRunAt]);
  return left;
}

/** 남은 시간 안내문. 사용자 요청(2026-08-04): 분과 초 단위로 보여 준다. */
function countdownMessage(seconds: number): string {
  if (seconds <= 0) return "다음 원고 생성이 곧 시작됩니다";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  const left = minutes > 0 ? `${minutes}분 ${rest}초` : `${rest}초`;
  return `다음 원고 생성까지 ${left} 남음`;
}

export function TaskStatusCard({ batch, jobs, jobPosts = {} }: Props) {
  // 원고 생성이 도는 동안 막대가 1초마다 다시 차오르게 한다(2026-08-11). 폴링(2초)에만
  // 맡기면 숫자가 2초씩 건너뛰고, 그마저 단계가 바뀔 때만 움직인다.
  const [tick, setTick] = useState(() => Date.now());
  const generating = jobs.some(
    (job) => job.status === "RUNNING" && job.stage === "DRAFT_GENERATION",
  );
  useEffect(() => {
    if (!generating) return;
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [generating]);

  /**
   * 개수는 **눈앞의 작업에서 직접 센다.** 배치 문서의 집계는 하나씩 더해 온 값이라
   * 어긋난 채로 저장돼 있을 수 있다 — 실제로 작업 2건짜리 배치에 `완료 1 · 실패 2`가
   * 들어 있었고, 그 배치의 진행률이 100%로 보였다(2026-08-06 신고). 서버 쪽 집계도
   * 다시 세도록 고쳤지만, 화면은 받은 작업을 그대로 세는 편이 언제나 맞다.
   *
   * 작업 목록이 비어 있을 때만(아직 안 왔다) 배치의 값을 쓴다.
   */
  const tally = countJobStatuses(jobs);
  const total = jobs.length > 0 ? tally.total : (batch?.totalCount ?? 0);
  /**
   * 완료 개수를 뭐라고 부를 것인가(2026-08-13).
   *
   * 발행 플랫폼을 하나도 고르지 않은 작업은 원고를 만들면 거기서 끝난다 — 올라간 적이
   * 없으므로 '발행 완료'라고 세면 거짓말이다. 그런 작업이 하나라도 섞이면 이름을
   * '완료'로 낮춘다. 전부 어딘가에 올리는 배치는 예전 그대로 '발행 완료'다.
   */
  const publishesEverything = jobs.length === 0 || jobs.every(publishesAnywhere);
  const progress = batch
    ? jobs.length > 0
      ? batchProgress(
          total,
          tally.completed,
          tally.failed,
          tally.canceled,
          runningStageFraction(jobs, tick, (jobId) => jobPosts[jobId]?.progress),
        )
      : batchProgress(
          batch.totalCount,
          batch.completedCount,
          batch.failedCount,
          batch.canceledCount,
          runningStageFraction(jobs, tick, (jobId) => jobPosts[jobId]?.progress),
        )
    : 0;
  // 예약 자신의 로그(단계 경계)에 글마다의 진행 줄을 합쳐 최신순으로 보여 준다.
  // 합치지 않으면 원고를 만드는 5~8분 동안 새 줄이 하나도 오지 않아 멈춘 것처럼 보인다.
  const logs = batch ? mergeActivityLogs(batch.logs, jobs, jobPosts) : [];

  // 다음 작업을 **기다리는 중**일 때만 센다. 작업이 돌고 있으면(currentJobId) 이미
  // 시작한 것이고, 멈춘 배치(일시정지·인증 대기)는 재개 전까지 시작되지 않는다 —
  // 그때 숫자를 보여 주면 거짓 약속이 된다.
  //
  // **아직 만들 원고가 남아 있을 때만** 센다(2026-08-11 사용자 신고: "담 원고 생성이
  // 없는데 이건 왜 이래"). 원고를 다 만들고 발행만 기다리는 중이면 currentJobId가
  // 비어 있어서, 그 조건만으로는 있지도 않은 '다음 원고'를 향해 숫자가 돌았다.
  // 목록이 비어 있으면 아직 안 온 것이다 — 모른다고 숫자를 감추면 첫 화면에서
  // 카운트다운이 깜빡인다. 받은 작업이 있는데 그중 대기가 없을 때만 감춘다.
  const hasWaitingJob =
    jobs.length === 0 || jobs.some((job) => job.status === "WAITING");
  const waitingForNext =
    batch !== null &&
    (batch.status === "RUNNING" || batch.status === "READY") &&
    hasWaitingJob &&
    !batch.currentJobId &&
    !batch.pauseRequested &&
    !batch.stopRequested;
  const remaining = useSecondsLeft(waitingForNext ? (batch.nextRunAt ?? null) : null);

  return (
    <TaskStatusPanel
      titleId="scheduled-status-title"
      progress={progress}
      /* 막대만으로는 '다 됐다'와 '다 실패했다'가 구분되지 않는다. 실제로 두 건 모두
         원고 생성에서 실패한 배치가 100%로 보였고, 사용자는 발행된 줄 알았다. */
      summary={
        batch && total > 0 ? (
          <>
            <span className="scheduled-tally-item done">
              {publishesEverything ? "발행 완료" : "완료"}{" "}
              <strong>{tally.completed}</strong>건
            </span>
            {tally.failed > 0 && (
              <span className="scheduled-tally-item failed">
                실패 <strong>{tally.failed}</strong>건
              </span>
            )}
            {tally.needsHuman > 0 && (
              <span className="scheduled-tally-item attention">
                인증 필요 <strong>{tally.needsHuman}</strong>건
              </span>
            )}
            {tally.canceled > 0 && (
              <span className="scheduled-tally-item">
                취소 <strong>{tally.canceled}</strong>건
              </span>
            )}
            <span className="scheduled-tally-item">
              남은 작업 <strong>{tally.pending}</strong>건
            </span>
            <span className="scheduled-tally-total">전체 {total}건</span>
          </>
        ) : null
      }
      logs={logs}
      note={
        batch
          ? "예약이 진행되면 이 목록이 갱신됩니다."
          : "작업을 시작하면 진행 상황이 여기에 표시됩니다."
      }
      headerExtra={
        remaining !== null ? (
          <span className="scheduled-countdown" role="timer">
            {countdownMessage(remaining)}
          </span>
        ) : null
      }
    />
  );
}
