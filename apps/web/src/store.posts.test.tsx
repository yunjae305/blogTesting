import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureAuth } from "./api/client";
import { WRITE_STEP } from "./resume";
import type {
  AuthSession,
  BlogTask,
  BlogTaskListItem,
  BlogTaskStatus,
  FinalPost,
} from "./api/types";
import { StoreProvider, useStore } from "./store";

/**
 * 글 작업 상태는 postId별로 완전히 갈라져 있어야 한다.
 *
 * 여기서 고정하는 것은 사용자가 겪은 문제 그대로다: 글 A를 만드는 동안 글 B의
 * '이어서 쓰기'를 누르면 B의 저장된 단계가 열려야 하고, A의 폴링 결과가 B 화면을
 * 덮어써서는 안 되며, A의 생성은 백그라운드에서 계속돼야 한다.
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

const FINAL_POST = {
  title: "완성된 글 B",
  body: "본문",
  hashtags: [],
  htmlContent: "<p>본문</p>",
} as FinalPost;

function post(postId: string, status: BlogTaskStatus, extra: Partial<BlogTask> = {}): BlogTask {
  return {
    postId,
    userId: "user_1",
    status,
    version: 1,
    createdAt: "2026-07-29T00:00:00.000Z",
    updatedAt: "2026-07-29T00:00:00.000Z",
    statusHistory: [],
    input: { topic: `${postId} 소재`, keywords: [], referenceMaterials: [] },
    postingLogs: [],
    ...extra,
  } as BlogTask;
}

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

async function settleEffects(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
  });
}

const PERSONAS = [
  { personaId: "friendly", kind: "preset", name: "친근한", description: "d", prompt: "p" },
  { personaId: "custom", kind: "custom", name: "직접 입력", description: "d" },
];

describe("postId별 작업 상태 분리", () => {
  let container: HTMLDivElement;
  let root: Root;
  let currentStore!: ReturnType<typeof useStore>;

  const POST_A = post("post_a", "GENERATING");
  const POST_B = post("post_b", "READY_TO_PUBLISH", { finalPost: FINAL_POST });
  const LIST_A: BlogTaskListItem = {
    postId: POST_A.postId,
    userId: POST_A.userId,
    status: POST_A.status,
    version: POST_A.version,
    createdAt: POST_A.createdAt,
    updatedAt: POST_A.updatedAt,
    title: POST_A.input.topic,
    topic: POST_A.input.topic,
    purposes: [],
    hasFinalPost: false,
  };
  const LIST_B: BlogTaskListItem = {
    postId: POST_B.postId,
    userId: POST_B.userId,
    status: POST_B.status,
    version: POST_B.version,
    createdAt: POST_B.createdAt,
    updatedAt: POST_B.updatedAt,
    title: FINAL_POST.title,
    topic: POST_B.input.topic,
    purposes: [],
    hasFinalPost: true,
  };

  function Probe() {
    currentStore = useStore();
    return <output data-post={currentStore.activePostId ?? "none"} />;
  }

  /** 열려는 글의 GET을 시험이 직접 제어할 수 있게 하는 fetch 스텁. */
  function stubFetch(
    overrides: Record<string, () => Promise<Response> | Response> = {},
  ) {
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      calls.push(path);
      const override = overrides[path];
      if (override) return override();
      if (path === "/personas") return jsonResponse(200, PERSONAS);
      if (path.includes("/settings")) return jsonResponse(404, { message: "not found" });
      if (path === "/posts?view=summary") return jsonResponse(200, [LIST_A, LIST_B]);
      if (path === "/posts/post_a/status") {
        return jsonResponse(200, {
          postId: POST_A.postId,
          status: POST_A.status,
          version: POST_A.version,
          hasIntentValidationResult: false,
        });
      }
      if (path === "/posts/post_b/status") {
        return jsonResponse(200, {
          postId: POST_B.postId,
          status: POST_B.status,
          version: POST_B.version,
          hasIntentValidationResult: false,
        });
      }
      if (path === "/posts/post_a") return jsonResponse(200, POST_A);
      if (path === "/posts/post_b") return jsonResponse(200, POST_B);
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return { fetchMock, calls };
  }

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem(ACTIVE_SESSION_KEY, JSON.stringify(SESSION));
    location.hash = "#/";
    configureAuth(null, () => undefined);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    vi.useRealTimers();
    await act(async () => root.unmount());
    container.remove();
    location.hash = "#/";
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function renderStore(): Promise<void> {
    await act(async () => {
      root.render(
        <StoreProvider>
          <Probe />
        </StoreProvider>,
      );
    });
    await settleEffects();
  }

  it("'이어서 쓰기'는 그 카드의 postId로 서버에 다시 물어보고 저장된 단계를 연다", async () => {
    const { calls } = stubFetch();
    await renderStore();

    await act(async () => {
      await currentStore.openPost("post_b");
    });

    // 목록 사본이 아니라 그 글의 최신 상태를 서버에서 가져온다.
    expect(calls).toContain("/posts/post_b");
    expect(currentStore.activePostId).toBe("post_b");
    expect(currentStore.task?.postId).toBe("post_b");
    // 원고가 준비된 글이므로 검증(제목)이 아니라 발행 단계다.
    expect(currentStore.step).toBe(WRITE_STEP.PUBLISH);
    expect(currentStore.route).toBe("write");
    // 새로고침해도 같은 글로 돌아오도록 주소에 postId가 남는다.
    expect(location.hash).toBe("#/write/post_b");
  });

  it("글을 못 열면 빈 '새 글 작성'이 아니라 실패했다는 사실을 남긴다", async () => {
    // 2026-08-06 사용자 신고 — 목록에서 '발행하기'를 눌렀는데 아무것도 없는 소재 입력
    // 폼이 떴다. 서버 로그를 보니 그 순간 GET /posts/{id}가 Mongo 시간 초과로 500이었다.
    // 실패하면 task가 null인 채로 남았고, 화면은 그것을 '새 글'로 읽었다.
    stubFetch({
      "/posts/post_b": () => jsonResponse(500, { message: "server error" }),
    });
    await renderStore();

    await act(async () => {
      await currentStore.openPost("post_b");
    });

    // 그 글을 열려던 중이라는 사실은 남는다 — 새 글을 시작한 것이 아니다.
    expect(currentStore.activePostId).toBe("post_b");
    expect(currentStore.task).toBeNull();
    expect(currentStore.postLoading).toBe(false);
    expect(currentStore.postLoadError).not.toBeNull();
  });

  it("다시 열기에 성공하면 실패 표시가 사라진다", async () => {
    let fail = true;
    stubFetch({
      "/posts/post_b": () =>
        fail ? jsonResponse(500, { message: "server error" }) : jsonResponse(200, POST_B),
    });
    await renderStore();

    await act(async () => {
      await currentStore.openPost("post_b");
    });
    expect(currentStore.postLoadError).not.toBeNull();

    fail = false;
    await act(async () => {
      await currentStore.openPost("post_b");
    });

    expect(currentStore.postLoadError).toBeNull();
    expect(currentStore.task?.postId).toBe("post_b");
    // 원고가 준비된 글이니 발행 단계로 열린다 — 사용자가 누른 '발행하기' 그대로.
    expect(currentStore.step).toBe(WRITE_STEP.PUBLISH);
  });

  it("생성 중인 글 A의 갱신이 방금 연 글 B의 화면을 덮어쓰지 않는다", async () => {
    stubFetch();
    await renderStore();

    await act(async () => {
      await currentStore.openPost("post_a");
    });
    expect(currentStore.task?.postId).toBe("post_a");

    await act(async () => {
      await currentStore.openPost("post_b");
    });
    expect(currentStore.task?.postId).toBe("post_b");

    // 글 A의 폴링이 살아 있다가 결과를 들고 돌아온 상황.
    const finishedA = post("post_a", "READY_TO_PUBLISH", { finalPost: FINAL_POST });
    await act(async () => currentStore.setTask(finishedA));

    // 화면은 여전히 글 B다.
    expect(currentStore.activePostId).toBe("post_b");
    expect(currentStore.task?.postId).toBe("post_b");
    expect(currentStore.task?.input.topic).toBe("post_b 소재");
    expect(currentStore.step).toBe(WRITE_STEP.PUBLISH);
    // 그래도 글 A의 진행은 버려지지 않는다 — 목록에는 최신 상태가 반영된다.
    expect(currentStore.posts.find((item) => item.postId === "post_a")?.status).toBe(
      "READY_TO_PUBLISH",
    );
  });

  it("늦게 도착한 글 A의 응답을 글 B 화면에 적용하지 않는다", async () => {
    const slowA = deferred<Response>();
    stubFetch({ "/posts/post_a": () => slowA.promise });
    await renderStore();

    let openingA!: Promise<void>;
    await act(async () => {
      openingA = currentStore.openPost("post_a");
    });
    // A 응답을 기다리는 동안 B로 옮긴다.
    await act(async () => {
      await currentStore.openPost("post_b");
    });
    expect(currentStore.task?.postId).toBe("post_b");

    slowA.resolve(jsonResponse(200, POST_A));
    await act(async () => {
      await openingA;
    });
    await settleEffects();

    expect(currentStore.activePostId).toBe("post_b");
    expect(currentStore.task?.postId).toBe("post_b");
    expect(currentStore.step).toBe(WRITE_STEP.PUBLISH);
    expect(currentStore.postLoading).toBe(false);
  });

  it("같은 글을 두 번 따라가도 폴링은 하나만 돈다", async () => {
    const { fetchMock } = stubFetch();
    await renderStore();

    let first!: Promise<BlogTask | null>;
    let second!: Promise<BlogTask | null>;
    await act(async () => {
      first = currentStore.followTask("post_b");
      second = currentStore.followTask("post_b");
    });
    // 새로고침하거나 생성 중인 글로 다시 들어와도 같은 추적을 넘겨받는다.
    expect(second).toBe(first);

    await act(async () => {
      await first;
    });
    // READY_TO_PUBLISH는 이미 끝난 상태라 한 번 확인하고 멈춘다.
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === "/posts/post_b")).toHaveLength(
      1,
    );
  });

  it("진행 status는 full 원고 재조회 없이 열린 글과 목록에 즉시 합친다", async () => {
    let statusCalls = 0;
    const completed = post("post_a", "READY_TO_PUBLISH", {
      version: 3,
      finalPost: FINAL_POST,
    });
    const progress = {
      phase: "DRAFT" as const,
      step: 2,
      totalSteps: 4,
      label: "이미지를 그리는 중이에요…",
      steps: ["본문", "이미지", "검수", "저장"],
      startedAt: "2026-07-29T00:00:01.000Z",
      updatedAt: "2026-07-29T00:00:02.000Z",
    };
    const { calls } = stubFetch({
      "/posts/post_a/status": () => {
        statusCalls += 1;
        return jsonResponse(
          200,
          statusCalls === 1
            ? {
                postId: "post_a",
                status: "GENERATING",
                version: 2,
                progress,
                hasIntentValidationResult: true,
              }
            : {
                postId: "post_a",
                status: "READY_TO_PUBLISH",
                version: 3,
                hasIntentValidationResult: true,
              },
        );
      },
      "/posts/post_a": () =>
        jsonResponse(200, statusCalls >= 2 ? completed : POST_A),
    });
    await renderStore();
    await act(async () => {
      await currentStore.openPost("post_a");
    });

    vi.useFakeTimers();
    let tracked!: Promise<BlogTask | null>;
    await act(async () => {
      tracked = currentStore.followTask("post_a");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });

    expect(currentStore.task).toEqual(
      expect.objectContaining({
        postId: "post_a",
        status: "GENERATING",
        version: 2,
        progress,
      }),
    );
    expect(currentStore.posts.find((item) => item.postId === "post_a")).toEqual(
      expect.objectContaining({ status: "GENERATING", version: 2 }),
    );
    // openPost의 최초 상세 조회뿐이다. 진행 중에는 /status만 읽는다.
    expect(calls.filter((path) => path === "/posts/post_a")).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      await tracked;
    });

    expect(currentStore.task?.status).toBe("READY_TO_PUBLISH");
    expect(currentStore.task?.finalPost?.title).toBe(FINAL_POST.title);
    // 끝났을 때만 최종 full 객체를 한 번 더 읽는다.
    expect(calls.filter((path) => path === "/posts/post_a")).toHaveLength(2);
  });

  it("'새 글로 시작'한 뒤 이전 글의 늦은 응답이 빈 작업실을 채우지 않는다", async () => {
    stubFetch();
    await renderStore();

    await act(async () => {
      await currentStore.openPost("post_a");
    });
    await act(async () => currentStore.restartWriting());
    expect(currentStore.activePostId).toBeNull();
    expect(currentStore.task).toBeNull();

    await act(async () => currentStore.setTask(post("post_a", "READY_TO_PUBLISH")));
    expect(currentStore.task).toBeNull();
    expect(currentStore.step).toBe(0);
  });

  it("주소에 남은 postId로 새로고침하면 그 글의 저장된 단계에서 복구된다", async () => {
    location.hash = "#/write/post_b";
    stubFetch();
    await renderStore();
    await settleEffects();

    expect(currentStore.route).toBe("write");
    expect(currentStore.activePostId).toBe("post_b");
    expect(currentStore.task?.postId).toBe("post_b");
    expect(currentStore.step).toBe(WRITE_STEP.PUBLISH);
  });

  it("순차 배포 중 구버전 API가 full 목록을 줘도 summary 카드로 정규화한다", async () => {
    stubFetch({
      "/posts?view=summary": () => jsonResponse(200, [POST_A, POST_B]),
    });
    await renderStore();

    expect(currentStore.posts[0]).toEqual(
      expect.objectContaining({
        postId: "post_a",
        title: "post_a 소재",
        hasFinalPost: false,
      }),
    );
    expect(currentStore.posts[1]).toEqual(
      expect.objectContaining({
        postId: "post_b",
        title: FINAL_POST.title,
        hasFinalPost: true,
      }),
    );
  });

  it("설정 응답을 기다리지 않고 summary 목록을 시작하며 로딩 상태를 정확히 끝낸다", async () => {
    const slowSettings = deferred<Response>();
    const slowPosts = deferred<Response>();
    const { calls } = stubFetch({
      "/users/user_1/settings": () => slowSettings.promise,
      "/posts?view=summary": () => slowPosts.promise,
    });

    await renderStore();

    expect(calls).toContain("/users/user_1/settings");
    expect(calls).toContain("/posts?view=summary");
    expect(currentStore.postsLoading).toBe(true);

    slowPosts.resolve(jsonResponse(200, [LIST_A, LIST_B]));
    await settleEffects();

    expect(currentStore.posts.map((item) => item.postId)).toEqual(["post_a", "post_b"]);
    expect(currentStore.postsLoading).toBe(false);
    // 설정은 아직 끝나지 않았지만 목록은 이미 사용할 수 있다.
    expect(currentStore.settings).toBeNull();

    slowSettings.resolve(jsonResponse(404, { message: "not found" }));
    await settleEffects();
  });

  it("늦은 summary가 그 사이 갱신된 글과 상세 fallback을 덮어쓰지 않는다", async () => {
    const slowPosts = deferred<Response>();
    stubFetch({
      "/posts?view=summary": () => slowPosts.promise,
      "/posts/post_a": () => Promise.reject(new Error("offline")),
    });
    await renderStore();

    const newerA = post("post_a", "READY_TO_PUBLISH", {
      version: 2,
      finalPost: FINAL_POST,
    });
    await act(async () => currentStore.setTask(newerA));
    expect(currentStore.posts.find((item) => item.postId === "post_a")?.version).toBe(2);

    slowPosts.resolve(jsonResponse(200, [LIST_A, LIST_B]));
    await settleEffects();

    expect(currentStore.posts.find((item) => item.postId === "post_a")?.version).toBe(2);

    await act(async () => currentStore.openPost("post_a"));
    expect(currentStore.task?.version).toBe(2);
    expect(currentStore.task?.finalPost?.title).toBe(FINAL_POST.title);
  });
});
