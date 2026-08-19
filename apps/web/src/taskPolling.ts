import { request } from "./api/client";
import type { BlogTask } from "./api/types";

const DEFAULT_INTERVALS_MS = [1200, 2000, 5000] as const;
// Provider 재시도와 이미지 생성이 겹치면 한 작업이 5분을 넘을 수 있다. API가 계속
// 정상 응답하는 동안에는 예전의 5분 제한으로 폴링을 끊지 않고, 비정상 무한 폴링만
// 막는 넉넉한 상한을 별도로 둔다.
const DEFAULT_HARD_TIMEOUT_MS = 30 * 60 * 1000;
const DEFAULT_DISCONNECTED_TIMEOUT_MS = 5 * 60 * 1000;

export interface BlogTaskStatusSnapshot {
  postId: string;
  status: BlogTask["status"];
  version: number;
  progress?: BlogTask["progress"];
  hasIntentValidationResult: boolean;
  /** 생성 중 '작업 현황' 로그(2026-08-10). 없거나 빈 배열일 수 있다. */
  activityLog?: BlogTask["activityLog"];
}

type TaskPollingState = BlogTask | BlogTaskStatusSnapshot;

function hasIntentValidationResult(task: TaskPollingState): boolean {
  if ("hasIntentValidationResult" in task) return task.hasIntentValidationResult;
  return Boolean(task.intentValidationResult);
}

export function taskStillNeedsPolling(task: TaskPollingState): boolean {
  // 완료 상태가 먼저다. 최종 원고 저장과 progress 정리는 별도 쓰기라 아주 짧은 순간
  // READY_TO_PUBLISH + 옛 progress가 함께 보일 수 있고, progress 정리만 실패할 수도 있다.
  if (task.status === "GENERATING") return true;
  if (task.status === "SEARCH_ANALYZING" && !hasIntentValidationResult(task)) return true;
  return false;
}

interface TaskPollingOptions {
  /** Legacy/testing override. When set, polling uses this fixed interval. */
  intervalMs?: number;
  /** Poll intervals, capped at the last value. Defaults to 1.2s, 2s, then 5s. */
  intervalsMs?: readonly number[];
  hardTimeoutMs?: number;
  disconnectedTimeoutMs?: number;
  now?: () => number;
  pause?: (milliseconds: number) => Promise<void>;
  loadStatus?: (postId: string) => Promise<BlogTaskStatusSnapshot>;
  load?: (postId: string) => Promise<BlogTask>;
  /** Receives lightweight progress updates without replacing the full BlogTask. */
  onStatus?: (snapshot: BlogTaskStatusSnapshot) => void;
  shouldContinue?: () => boolean;
  /** 무엇을 '끝'으로 볼지. 기본은 한 단계짜리 흐름 기준(`taskStillNeedsPolling`)이다. */
  isSettled?: (task: TaskPollingState) => boolean;
}

/**
 * 백그라운드 작업이 끝날 때까지 최신 글을 가져온다.
 *
 * 성공적인 GET 응답이 이어지는 작업은 5분이 넘어도 계속 따라간다. 5분 제한은 이제
 * 서버 응답이 전혀 없을 때만 적용하고, 살아 있지만 끝나지 않는 작업에는 30분의 별도
 * 안전 상한을 적용한다.
 */
export async function pollTaskUntilSettled(
  postId: string,
  onUpdate: (task: BlogTask) => void,
  options: TaskPollingOptions = {},
): Promise<BlogTask | null> {
  const intervalsMs =
    options.intervalMs !== undefined
      ? [options.intervalMs]
      : options.intervalsMs?.length
        ? options.intervalsMs
        : DEFAULT_INTERVALS_MS;
  const hardTimeoutMs = options.hardTimeoutMs ?? DEFAULT_HARD_TIMEOUT_MS;
  const disconnectedTimeoutMs =
    options.disconnectedTimeoutMs ?? DEFAULT_DISCONNECTED_TIMEOUT_MS;
  const now = options.now ?? Date.now;
  const pause =
    options.pause ??
    ((milliseconds: number) =>
      new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds)));
  const load =
    options.load ??
    ((id: string) => request<BlogTask>(`/posts/${id}`));
  const loadStatus =
    options.loadStatus ??
    ((id: string) => request<BlogTaskStatusSnapshot>(`/posts/${id}/status`));
  const shouldContinue = options.shouldContinue ?? (() => true);
  const isSettled =
    options.isSettled ?? ((task: TaskPollingState) => !taskStillNeedsPolling(task));

  const startedAt = now();
  let lastSuccessfulPollAt = startedAt;
  let pollAttempt = 0;

  while (now() - startedAt < hardTimeoutMs) {
    if (!shouldContinue()) return null;
    const intervalMs = intervalsMs[Math.min(pollAttempt, intervalsMs.length - 1)]!;
    await pause(intervalMs);
    if (!shouldContinue()) return null;
    pollAttempt += 1;

    let snapshot: BlogTaskStatusSnapshot;
    try {
      snapshot = await loadStatus(postId);
    } catch {
      if (!shouldContinue()) return null;
      // 잠깐 끊긴 요청은 재시도하되, 서버 응답이 오래 없으면 호출자에게 제어를 돌려준다.
      if (now() - lastSuccessfulPollAt >= disconnectedTimeoutMs) return null;
      continue;
    }

    // The user may have switched accounts while this request was in flight. Never
    // apply an old account's task to the newly active workspace.
    if (!shouldContinue()) return null;
    lastSuccessfulPollAt = now();
    options.onStatus?.(snapshot);
    if (!isSettled(snapshot)) {
      continue;
    }

    let latest: BlogTask;
    try {
      latest = await load(postId);
    } catch {
      if (!shouldContinue()) return null;
      if (now() - lastSuccessfulPollAt >= disconnectedTimeoutMs) return null;
      continue;
    }

    if (!shouldContinue()) return null;
    onUpdate(latest);
    // status projection과 full 조회 사이에 사용자가 재생성을 시작할 수 있다. 첫 응답만
    // 믿고 멈추면 GENERATING full 객체를 "완료"로 반환한 채 추적이 영구 종료된다.
    if (isSettled(latest)) return latest;
  }

  return null;
}
