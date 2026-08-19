import type { ScheduledJob, ScheduledJobStatus, TaskProgress } from "../../api/types";
import { draftProgressPercent, elapsedSince } from "../../draftProgress";

/**
 * 소재 정리. **서버의 normalize_topics와 같은 규칙이어야 한다** — 화면이 세는 개수와
 * 실제로 만들어지는 작업 수가 다르면 목표량 검증이 어긋난다.
 *
 * - 줄바꿈으로 나눈다
 * - 앞뒤 공백을 없앤다
 * - 빈 줄을 버린다
 * - 같은 소재가 반복되면 첫 번째만 남긴다(입력 순서 유지)
 */
export function normalizeTopics(text: string): string[] {
  const seen = new Set<string>();
  const topics: string[] = [];
  for (const line of text.split("\n")) {
    const topic = line.trim();
    if (!topic || seen.has(topic)) continue;
    seen.add(topic);
    topics.push(topic);
  }
  return topics;
}

/** 표의 상태 칸에 쓰는 말. 진행 중인 작업은 지금 어느 단계인지까지 보여 준다. */
export function jobStatusLabel(job: ScheduledJob): string {
  if (job.status === "RUNNING") {
    switch (job.stage) {
      case "CREATE_POST":
        return "소재 준비 중";
      case "TREND_RECOMMENDATION":
        return "키워드 분석 중";
      case "TITLE_GENERATION":
        return "제목 생성 중";
      case "SEARCH_ANALYSIS":
      case "INTENT_SELECTION":
        return "자료 검증 중";
      case "DRAFT_GENERATION":
        return "원고 생성 중";
      default:
        return "진행중";
    }
  }
  if (job.status === "PUBLISHING") {
    // Naver 발행이 끝나면 같은 원고가 Threads로 넘어간다 — 어디를 발행 중인지 가른다.
    // 쓰레드 단독 예약은 Naver 칸을 아예 지나지 않으므로 단계를 볼 것도 없다.
    const threadsOnly = job.publishNaver === false;
    return threadsOnly || job.stage === "THREADS_PUBLISH"
      ? "Threads 발행 중"
      : "Naver 발행 중";
  }
  const labels: Record<ScheduledJobStatus, string> = {
    WAITING: "대기",
    RUNNING: "진행중",
    READY_TO_PUBLISH: "발행 대기",
    PUBLISHING: "Naver 발행 중",
    COMPLETED: "완료",
    FAILED: "실패",
    NEEDS_HUMAN: "인증 필요",
    CANCELED: "취소",
  };
  return labels[job.status] ?? job.status;
}

/** 상태 배지의 색 계열. app.css의 .scheduled-state 변형과 짝이다. */
export function jobStatusTone(status: ScheduledJobStatus): string {
  switch (status) {
    case "RUNNING":
    case "PUBLISHING":
    case "READY_TO_PUBLISH":
      return "running";
    case "COMPLETED":
      return "done";
    case "FAILED":
      return "failed";
    case "NEEDS_HUMAN":
      return "attention";
    case "CANCELED":
      return "canceled";
    default:
      return "waiting";
  }
}

/**
 * 진행 중인 작업이 글 하나 안에서 어디까지 왔는지의 몫(0~1).
 *
 * 값은 단계별 소요 시간의 어림이다 — 원고·이미지 생성이 가장 길고, 그 앞 단계들은
 * 몇 초~1분 안팎이다. 정확한 예측이 목적이 아니라, 글 단위로만 세면 첫 글이 끝날
 * 때까지 0%에 머무는 것(2026-08-04 사용자 보고)을 단계마다 움직이게 하는 것이 목적이다.
 */
const STAGE_FRACTION: Record<string, number> = {
  CREATE_POST: 0.05,
  TREND_RECOMMENDATION: 0.15,
  TITLE_GENERATION: 0.25,
  SEARCH_ANALYSIS: 0.35,
  INTENT_SELECTION: 0.5,
  DRAFT_GENERATION: 0.6,
  NAVER_PUBLISH: 0.9,
  THREADS_PUBLISH: 0.95,
  DONE: 1,
};

/**
 * 지금 돌고 있는(또는 발행 단계에 걸려 있는) 작업들의 단계 몫 합.
 *
 * 대기(WAITING) 작업은 세지 않는다 — stage가 기본값(CREATE_POST)이라 아직 시작도 안 한
 * 작업이 5%씩 보태져 진행률이 부풀는다. 끝난 작업도 세지 않는다(분자에서 따로 센다).
 */
export function runningStageFraction(
  jobs: ScheduledJob[],
  now = Date.now(),
  progressOf: (jobId: string) => TaskProgress | undefined = () => undefined,
): number {
  return jobs
    .filter(
      (job) =>
        job.status === "RUNNING" ||
        job.status === "READY_TO_PUBLISH" ||
        job.status === "PUBLISHING" ||
        job.status === "NEEDS_HUMAN",
    )
    .reduce((sum, job) => sum + jobFraction(job, now, progressOf(job.jobId)), 0);
}

/**
 * 이 작업이 **어딘가에 올리기는 하는가**.
 *
 * 발행 플랫폼을 하나도 고르지 않은 작업은 원고를 만들면 거기서 끝난다(2026-08-13,
 * 서버의 `models.publishes_anywhere`와 같은 판단). 옛 작업에는 `publishNaver`가 없고
 * 없으면 true다 — 그때는 네이버가 언제나 발행 대상이었다.
 */
export function publishesAnywhere(job: ScheduledJob): boolean {
  return (job.publishNaver ?? true) || (job.publishThreads ?? false);
}

/**
 * 작업 하나가 글 안에서 어디까지 왔는지(0~1).
 *
 * 원고 생성(DRAFT_GENERATION)은 5~8분짜리 한 칸이라, 단계 몫만 쓰면 그 시간 내내
 * 같은 숫자에 멈춰 있다(2026-08-11 사용자 요청: "진행율 바 이것도 실시간으로 퍼센트
 * 나눠"). 그 안에서는 새 글 작성 화면이 쓰는 계산기를 그대로 빌려, 네 칸(구조 설계 →
 * 본문 → 이미지 → 다듬기)의 가중치와 머문 시간으로 이 칸을 채운다.
 *
 * 진행 정보가 없으면(옛 작업·아직 안 온 폴링) 예전처럼 단계 몫만 쓴다.
 */
function jobFraction(
  job: ScheduledJob,
  now: number,
  progress: TaskProgress | undefined,
): number {
  const base = STAGE_FRACTION[job.stage] ?? 0;
  if (job.stage !== "DRAFT_GENERATION") return base;
  if (!progress || !progress.steps?.length) return base;
  const inner =
    draftProgressPercent({
      steps: progress.steps,
      // 서버는 1부터 세고(step), 계산기는 0부터 센다.
      stepIndex: Math.max(0, progress.step - 1),
      elapsedInStepMs: elapsedSince(progress.updatedAt, now),
      unitsDone: progress.unitsDone,
      unitsTotal: progress.unitsTotal,
      stepSeconds: progress.stepSeconds,
    }) / 100;
  // 이 칸이 차지하는 폭만큼만 채운다. 올릴 곳이 있으면 그 폭은 원고 생성 → 발행
  // 사이이고, **올릴 곳이 없으면 원고가 곧 끝이라 100%까지다**(2026-08-13 사용자
  // 지적: "원고만 생성 다 하면 작업이 완료가 되는 거잖아. 그러면 진행바도 100퍼가
  // 되어야겠지"). 발행 몫을 남겨 두면 그 작업은 영영 90%에서 멈춘 것처럼 보인다.
  const next = publishesAnywhere(job) ? (STAGE_FRACTION.NAVER_PUBLISH ?? 1) : 1;
  return base + inner * Math.max(0, next - base);
}

/**
 * 진행률(%). 끝난 작업(완료·실패·취소)에 진행 중인 작업의 단계 몫을 더해 전체로 나눈다.
 *
 * 배치가 아직 없으면 0이다 — 시작 전에 100%가 보이면 안 된다.
 *
 * **이 숫자는 '얼마나 진행됐는가'이지 '얼마나 발행됐는가'가 아니다.** 둘이 같다고
 * 읽히면 안 되므로, 카드는 이 막대 아래에 완료·실패·남은 수를 따로 적는다
 * (`countJobStatuses`). 작업 2건이 모두 실패한 배치가 100%로만 보여서 "다 발행됐다"로
 * 읽힌 자리다(2026-08-06 신고).
 */
export function batchProgress(
  totalCount: number,
  completedCount: number,
  failedCount: number,
  canceledCount: number,
  stageFraction = 0,
): number {
  if (totalCount <= 0) return 0;
  const terminal = completedCount + failedCount + canceledCount;
  return Math.min(100, Math.floor(((terminal + stageFraction) / totalCount) * 100));
}

type JobTally = {
  total: number;
  completed: number;
  failed: number;
  canceled: number;
  /**
   * 인증을 기다리며 멈춘 작업. 실패로 묶지 않는다 — 사용자가 인증을 마치고 재개하면
   * 그대로 이어지므로, '실패 N건'에 넣으면 끝난 일처럼 읽힌다.
   */
  needsHuman: number;
  /** 아직 남은 작업(대기·진행 중·발행 대기). */
  pending: number;
};

/**
 * 작업들을 상태별로 센다. **배치 문서의 집계 대신 실제 작업을 센다.**
 *
 * 배치의 completedCount·failedCount는 하나씩 더해 온 값이라 어긋날 수 있었다 —
 * 실패한 작업을 재시도해 성공하면 실패가 그대로 남아 같은 작업이 두 번 세어졌고,
 * 작업 2건짜리 배치에 `done=1 fail=2`가 저장돼 있었다(2026-08-06 실제 데이터).
 * 서버 쪽도 다시 세도록 고쳤지만(service._counts_of), 화면은 눈앞의 작업을 직접
 * 세는 편이 언제나 맞다.
 */
export function countJobStatuses(jobs: ScheduledJob[]): JobTally {
  const tally: JobTally = {
    total: jobs.length,
    completed: 0,
    failed: 0,
    canceled: 0,
    needsHuman: 0,
    pending: 0,
  };
  for (const job of jobs) {
    if (job.status === "COMPLETED") tally.completed += 1;
    else if (job.status === "FAILED") tally.failed += 1;
    else if (job.status === "CANCELED") tally.canceled += 1;
    else if (job.status === "NEEDS_HUMAN") tally.needsHuman += 1;
    else tally.pending += 1;
  }
  return tally;
}
