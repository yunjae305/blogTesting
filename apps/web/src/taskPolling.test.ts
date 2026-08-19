import { describe, expect, it } from "vitest";

import type { BlogTask, TaskProgress } from "./api/types";
import {
  pollTaskUntilSettled,
  taskStillNeedsPolling,
  type BlogTaskStatusSnapshot,
} from "./taskPolling";

function task(status: BlogTask["status"], extra: Partial<BlogTask> = {}): BlogTask {
  return { postId: "post_1", status, ...extra } as BlogTask;
}

function snapshot(
  status: BlogTask["status"],
  extra: Partial<BlogTaskStatusSnapshot> = {},
): BlogTaskStatusSnapshot {
  return {
    postId: "post_1",
    status,
    version: 1,
    hasIntentValidationResult: false,
    ...extra,
  };
}

describe("task polling", () => {
  it("keeps following a responsive generation after the old five-minute limit", async () => {
    let now = 0;
    let statusLoads = 0;
    let fullLoads = 0;
    const updates: BlogTask[] = [];
    const completed = task("READY_TO_PUBLISH", {
      finalPost: { title: "completed draft" } as BlogTask["finalPost"],
    });

    const settled = await pollTaskUntilSettled(
      "post_1",
      (latest) => updates.push(latest),
      {
        intervalMs: 60_000,
        hardTimeoutMs: 30 * 60_000,
        disconnectedTimeoutMs: 5 * 60_000,
        now: () => now,
        pause: async (milliseconds) => {
          now += milliseconds;
        },
        loadStatus: async () => {
          statusLoads += 1;
          return statusLoads <= 5
            ? snapshot("GENERATING")
            : snapshot("READY_TO_PUBLISH");
        },
        load: async () => {
          fullLoads += 1;
          return completed;
        },
      },
    );

    expect(now).toBe(6 * 60_000);
    expect(statusLoads).toBe(6);
    expect(fullLoads).toBe(1);
    expect(settled).toBe(completed);
    expect(updates).toEqual([completed]);
  });

  it("stops after five minutes only when every status request keeps failing", async () => {
    let now = 0;
    let calls = 0;

    const settled = await pollTaskUntilSettled("post_1", () => undefined, {
      intervalMs: 60_000,
      hardTimeoutMs: 30 * 60_000,
      disconnectedTimeoutMs: 5 * 60_000,
      now: () => now,
      pause: async (milliseconds) => {
        now += milliseconds;
      },
      loadStatus: async () => {
        calls += 1;
        throw new Error("offline");
      },
    });

    expect(settled).toBeNull();
    expect(calls).toBe(5);
  });

  it("keeps a hard upper bound for a responsive job that never settles", async () => {
    let now = 0;
    let statusLoads = 0;
    let fullLoads = 0;

    const settled = await pollTaskUntilSettled("post_1", () => undefined, {
      intervalMs: 60_000,
      hardTimeoutMs: 3 * 60_000,
      disconnectedTimeoutMs: 5 * 60_000,
      now: () => now,
      pause: async (milliseconds) => {
        now += milliseconds;
      },
      loadStatus: async () => {
        statusLoads += 1;
        return snapshot("GENERATING");
      },
      load: async () => {
        fullLoads += 1;
        return task("GENERATING");
      },
    });

    expect(settled).toBeNull();
    expect(statusLoads).toBe(3);
    expect(fullLoads).toBe(0);
  });

  it("accepts a completed task even if a stale progress value remains", () => {
    const staleProgress = {
      phase: "DRAFT",
      step: 4,
      totalSteps: 4,
      label: "final review",
      steps: ["outline", "draft", "images", "review"],
      startedAt: "2026-07-28T00:00:00Z",
      updatedAt: "2026-07-28T00:05:00Z",
    } satisfies TaskProgress;

    expect(
      taskStillNeedsPolling(
        task("READY_TO_PUBLISH", {
          finalPost: { title: "completed draft" } as BlogTask["finalPost"],
          progress: staleProgress,
        }),
      ),
    ).toBe(false);
  });

  it("stops before loading status when the active account changes during a pause", async () => {
    let active = true;
    let statusLoads = 0;

    const settled = await pollTaskUntilSettled("post_1", () => undefined, {
      intervalMs: 1,
      pause: async () => {
        active = false;
      },
      loadStatus: async () => {
        statusLoads += 1;
        return snapshot("GENERATING");
      },
      shouldContinue: () => active,
    });

    expect(settled).toBeNull();
    expect(statusLoads).toBe(0);
  });

  it("ignores a status response that finishes after the active account changes", async () => {
    let active = true;
    let fullLoads = 0;
    const updates: BlogTask[] = [];

    const settled = await pollTaskUntilSettled(
      "post_1",
      (latest) => updates.push(latest),
      {
        intervalMs: 1,
        pause: async () => undefined,
        loadStatus: async () => {
          active = false;
          return snapshot("READY_TO_PUBLISH");
        },
        load: async () => {
          fullLoads += 1;
          return task("READY_TO_PUBLISH");
        },
        shouldContinue: () => active,
      },
    );

    expect(settled).toBeNull();
    expect(fullLoads).toBe(0);
    expect(updates).toEqual([]);
  });

  it("backs off 1.2s to 2s to 5s and loads the full task only at terminal status", async () => {
    const pauses: number[] = [];
    const statuses: BlogTaskStatusSnapshot[] = [
      snapshot("GENERATING", { version: 2 }),
      snapshot("GENERATING", { version: 3 }),
      snapshot("GENERATING", { version: 4 }),
      snapshot("READY_TO_PUBLISH", { version: 5 }),
    ];
    const statusUpdates: BlogTaskStatusSnapshot[] = [];
    const full = task("READY_TO_PUBLISH", { version: 5 });
    let statusLoads = 0;
    let fullLoads = 0;
    const updates: BlogTask[] = [];

    const settled = await pollTaskUntilSettled(
      "post_1",
      (latest) => updates.push(latest),
      {
        pause: async (milliseconds) => {
          pauses.push(milliseconds);
        },
        loadStatus: async () => statuses[statusLoads++]!,
        load: async () => {
          fullLoads += 1;
          return full;
        },
        onStatus: (latest) => statusUpdates.push(latest),
      },
    );

    expect(pauses).toEqual([1200, 2000, 5000, 5000]);
    expect(statusLoads).toBe(4);
    expect(statusUpdates).toEqual(statuses);
    expect(fullLoads).toBe(1);
    expect(updates).toEqual([full]);
    expect(settled).toBe(full);
  });

  it("uses the lightweight intent-ready flag to settle search analysis", () => {
    expect(taskStillNeedsPolling(snapshot("SEARCH_ANALYZING"))).toBe(true);
    expect(
      taskStillNeedsPolling(
        snapshot("SEARCH_ANALYZING", { hasIntentValidationResult: true }),
      ),
    ).toBe(false);
  });

  it("keeps polling when a regeneration starts between status and full reads", async () => {
    const statuses = [
      snapshot("READY_TO_PUBLISH", { version: 2 }),
      snapshot("GENERATING", { version: 3 }),
      snapshot("READY_TO_PUBLISH", { version: 4 }),
    ];
    const fullTasks = [
      task("GENERATING", { version: 3 }),
      task("READY_TO_PUBLISH", { version: 4 }),
    ];
    const updates: BlogTask[] = [];
    let statusLoads = 0;
    let fullLoads = 0;

    const settled = await pollTaskUntilSettled(
      "post_1",
      (latest) => updates.push(latest),
      {
        intervalMs: 1,
        pause: async () => undefined,
        loadStatus: async () => statuses[statusLoads++]!,
        load: async () => fullTasks[fullLoads++]!,
      },
    );

    expect(statusLoads).toBe(3);
    expect(fullLoads).toBe(2);
    expect(updates.map((latest) => latest.status)).toEqual([
      "GENERATING",
      "READY_TO_PUBLISH",
    ]);
    expect(settled).toBe(fullTasks[1]);
  });
});
