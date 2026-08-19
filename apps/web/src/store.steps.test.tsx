import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureAuth } from "./api/client";
import { WRITE_STEP } from "./resume";
import type { AuthSession, BlogTask, BlogTaskStatus } from "./api/types";
import { StoreProvider, useStore } from "./store";

/**
 * 단계 이동의 바닥(floor) 규칙.
 *
 * 원고를 쓰기 시작한 뒤에는 그 앞 단계로 돌아가지 못한다 — 원고가 그때 고른 제목과
 * 방향으로 쓰이고 있어서, 뒤로 가 그것을 바꾸면 화면과 실제 원고가 다른 말을 하게 된다.
 * 그 **전에는 자유롭게 오갈 수 있어야 한다.**
 *
 * 2026-08-07 신고 — '제목 다시 고르기'가 아무 일도 하지 않았다. 바닥이 숫자 `2`로 박혀
 * 있었는데, 검증이 자기 단계(VERIFY=2)로 독립하면서 원고가 3으로 밀린 뒤에도 그대로였다.
 */

const ACTIVE_SESSION_KEY = "blog-it:session";

const SESSION: AuthSession = {
  user: {
    userId: "user_1",
    email: "a@example.com",
    nickname: "작성자",
    createdAt: "2026-07-28T00:00:00.000Z",
    updatedAt: "2026-07-28T00:00:00.000Z",
  },
  accessToken: "token-user_1",
  issuedAt: "2099-01-01T00:00:00.000Z",
  expiresAt: "2099-01-08T00:00:00.000Z",
};

function task(status: BlogTaskStatus, extra: Partial<BlogTask> = {}): BlogTask {
  return {
    postId: "post_1",
    userId: "user_1",
    status,
    version: 1,
    createdAt: "2026-08-07T00:00:00.000Z",
    updatedAt: "2026-08-07T00:00:00.000Z",
    statusHistory: [],
    input: { topic: "소재", keywords: [], referenceMaterials: [] },
    postingLogs: [],
    ...extra,
  } as BlogTask;
}

const TREND_SELECTION = {
  finalTopic: "고른 제목",
  selectedTrendKeywordIds: [],
  skipped: false,
  selectedAt: "2026-08-07T00:01:00.000Z",
};

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("작성 단계 이동의 바닥 규칙", () => {
  let container: HTMLDivElement;
  let root: Root;
  let store!: ReturnType<typeof useStore>;

  function Probe() {
    store = useStore();
    return <output data-step={store.step} />;
  }

  beforeEach(async () => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem(ACTIVE_SESSION_KEY, JSON.stringify(SESSION));
    location.hash = "#/";
    configureAuth(null, () => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const path = String(input);
        if (path === "/personas") return jsonResponse(200, []);
        if (path === "/posts?view=summary") return jsonResponse(200, []);
        return jsonResponse(404, { message: "not found" });
      }),
    );
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => {
      root.render(
        <StoreProvider>
          <Probe />
        </StoreProvider>,
      );
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    location.hash = "#/";
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("검증 단계에서 제목 단계로 돌아갈 수 있다", async () => {
    // 2026-08-07 신고 — '제목 다시 고르기'가 눌러도 아무 일도 하지 않았다.
    await act(async () => {
      store.setTask(task("SEARCH_ANALYZING", { trendSelection: TREND_SELECTION }));
      store.setStep(WRITE_STEP.VERIFY);
    });
    expect(store.step).toBe(WRITE_STEP.VERIFY);

    await act(async () => store.setStep(WRITE_STEP.TITLE));

    expect(store.step).toBe(WRITE_STEP.TITLE);
  });

  it("검증 단계에서 소재 단계로도 돌아갈 수 있다", async () => {
    await act(async () => {
      store.setTask(task("SEARCH_ANALYZING", { trendSelection: TREND_SELECTION }));
      store.setStep(WRITE_STEP.VERIFY);
    });

    await act(async () => store.setStep(WRITE_STEP.TOPIC));

    expect(store.step).toBe(WRITE_STEP.TOPIC);
  });

  it("원고를 쓰기 시작하면 그 앞 단계로는 돌아가지 못한다", async () => {
    // 원고가 그때의 제목·방향으로 쓰이고 있다. 뒤로 가 바꾸면 화면과 원고가 어긋난다.
    await act(async () => {
      store.setTask(
        task("GENERATING", {
          trendSelection: TREND_SELECTION,
          selectedIntent: {
            intentId: "i1",
            title: "방향",
            targetReader: "독자",
            rationale: "근거",
            sources: [],
          },
        }),
      );
      store.setStep(WRITE_STEP.DRAFT);
    });
    expect(store.step).toBe(WRITE_STEP.DRAFT);

    await act(async () => store.setStep(WRITE_STEP.TITLE));

    expect(store.step).toBe(WRITE_STEP.DRAFT);
  });
});
