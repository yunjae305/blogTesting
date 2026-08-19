import { describe, expect, it } from "vitest";

import type { ScheduledJob, ScheduledLogEntry } from "../../api/types";
import { mergeActivityLogs } from "./TaskStatusCard";
import type { JobPostState } from "./useScheduledPosting";

/**
 * '작업 현황' 로그 합치기(2026-08-10 사용자 요청).
 *
 * 예약 자신의 로그는 단계 경계에서만 한 줄씩 쌓인다. 그래서 원고를 만드는 5~8분 동안
 * 새 줄이 하나도 오지 않아 **백엔드는 실시간으로 도는데 화면은 멈춰 보였다.**
 * 새 글 작성 화면이 보여 주는 줄들을 같이 실어 그 사이를 채운다.
 */

function job(jobId: string, topic: string): ScheduledJob {
  // 합치기가 읽는 것은 jobId와 topic 둘뿐이다. 나머지는 타입을 맞추기 위한 자리다.
  return {
    jobId,
    batchId: "batch_1",
    userId: "user_1",
    platform: "naver",
    sequence: 0,
    variantIndex: 0,
    topic,
    status: "RUNNING",
    stage: "DRAFT_GENERATION",
    publishNaver: true,
    publishThreads: false,
    retryCount: 0,
    createdAt: "2026-08-10T05:00:00.000Z",
    updatedAt: "2026-08-10T05:00:00.000Z",
  } as ScheduledJob;
}

function batchLog(at: string, message: string): ScheduledLogEntry {
  return { at, message, tone: "info" };
}

describe("작업 현황 로그 합치기", () => {
  it("글마다의 진행 줄을 예약 로그 사이에 시각 순서로 끼운다", () => {
    const jobs = [job("job_0", "현대백화점"), job("job_1", "신세계백화점")];
    const posts: Record<string, JobPostState> = {
      job_0: {
        activityLog: [{ at: "2026-08-10T05:02:00.000Z", message: "본문을 쓰는 중이에요…" }],
      },
      job_1: {
        activityLog: [{ at: "2026-08-10T05:03:00.000Z", message: "사진을 고르는 중이에요…" }],
      },
    };

    const merged = mergeActivityLogs(
      [
        batchLog("2026-08-10T05:01:00.000Z", "'현대백화점'의 글 생성을 시작합니다."),
        batchLog("2026-08-10T05:04:00.000Z", "'현대백화점'의 네이버 발행을 시작합니다."),
      ],
      jobs,
      posts,
    );

    // 최신순. 소재 이름이 앞에 붙어 어느 글의 이야기인지 알 수 있다.
    expect(merged.map((entry) => entry.message)).toEqual([
      "'현대백화점'의 네이버 발행을 시작합니다.",
      "'신세계백화점'의 사진을 고르는 중이에요…",
      "'현대백화점'의 본문을 쓰는 중이에요…",
      "'현대백화점'의 글 생성을 시작합니다.",
    ]);
  });

  it("진행 줄은 흐리게 둔다 — 예약의 사건 기록이 더 굵은 이야기다", () => {
    const merged = mergeActivityLogs(
      [],
      [job("job_0", "현대백화점")],
      { job_0: { activityLog: [{ at: "2026-08-10T05:02:00.000Z", message: "쓰는 중" }] } },
    );

    expect(merged[0].tone).toBe("muted");
    expect(merged[0].jobId).toBe("job_0");
  });

  it("진행 줄이 없으면 예약 로그만 최신순으로 준다(예전과 같다)", () => {
    const merged = mergeActivityLogs(
      [
        batchLog("2026-08-10T05:01:00.000Z", "첫 줄"),
        batchLog("2026-08-10T05:02:00.000Z", "둘째 줄"),
      ],
      [job("job_0", "현대백화점")],
      {},
    );

    expect(merged.map((entry) => entry.message)).toEqual(["둘째 줄", "첫 줄"]);
  });

  it("같은 시각이면 예약의 사건 줄이 진행 줄보다 위에 온다", () => {
    const same = "2026-08-10T05:02:00.000Z";
    const merged = mergeActivityLogs(
      [batchLog(same, "'현대백화점'의 네이버 발행을 시작합니다.")],
      [job("job_0", "현대백화점")],
      { job_0: { activityLog: [{ at: same, message: "쓰는 중" }] } },
    );

    expect(merged[0].message).toBe("'현대백화점'의 네이버 발행을 시작합니다.");
  });
});

/**
 * 2026-08-12 사용자 신고 — 한 소재로 2편을 걸었더니 작업 현황에 **글자 하나 다르지 않은
 * 줄이 두 번씩** 찍혔다. 어느 편의 이야기인지 알 수 없고, 화면이 같은 줄을 두 번 그린
 * 것처럼 보인다.
 */
describe("같은 소재 여러 편의 로그", () => {
  it("편 번호를 앞에 붙여 두 줄을 가른다", () => {
    const jobs = [job("job_1", "롯데리아"), job("job_2", "롯데리아")];
    const posts: Record<string, JobPostState> = {
      job_1: { activityLog: [{ at: "2026-08-12T04:22:04.000Z", message: "원고를 만듭니다" }] },
      job_2: { activityLog: [{ at: "2026-08-12T04:22:05.000Z", message: "원고를 만듭니다" }] },
    };

    const merged = mergeActivityLogs([], jobs, posts);

    const messages = merged.map((entry) => entry.message);
    expect(messages).toContain("1편째 · '롯데리아'의 원고를 만듭니다");
    expect(messages).toContain("2편째 · '롯데리아'의 원고를 만듭니다");
  });

  it("예약 자신의 줄에도 그 번호를 붙인다", () => {
    const jobs = [job("job_1", "롯데리아"), job("job_2", "롯데리아")];
    const logs: ScheduledLogEntry[] = [
      { at: "2026-08-12T04:21:51.000Z", message: "'롯데리아'의 예약 시각이 되었습니다.", tone: "info", jobId: "job_2" },
    ];

    const merged = mergeActivityLogs(logs, jobs, {});

    expect(merged[0].message).toBe("2편째 · '롯데리아'의 예약 시각이 되었습니다.");
  });

  it("한 편뿐이면 예전 그대로 아무것도 붙이지 않는다", () => {
    const jobs = [job("job_1", "롯데리아")];
    const posts: Record<string, JobPostState> = {
      job_1: { activityLog: [{ at: "2026-08-12T04:22:04.000Z", message: "원고를 만듭니다" }] },
    };

    const merged = mergeActivityLogs([], jobs, posts);

    expect(merged[0].message).toBe("'롯데리아'의 원고를 만듭니다");
  });
});
