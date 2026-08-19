import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getAccountSessions,
  saveAccountSession,
} from "./accountSessions";
import { configureAuth } from "./api/client";
import type {
  AuthSession,
  BlogTask,
  UserSettings,
} from "./api/types";
import {
  REMEMBERED_ACCOUNTS_STORAGE_KEY,
} from "./rememberedAccounts";
import { StoreProvider, useStore } from "./store";

const ACTIVE_SESSION_KEY = "blog-it:session";
const CREATED_AT = "2026-07-28T00:00:00.000Z";
const ISSUED_AT = "2099-01-01T00:00:00.000Z";
const EXPIRES_AT = "2099-01-08T00:00:00.000Z";

function authSession(userId: string, email: string, nickname: string): AuthSession {
  return {
    user: {
      userId,
      email,
      nickname,
      createdAt: CREATED_AT,
      updatedAt: CREATED_AT,
    },
    accessToken: `token-${userId}`,
    issuedAt: ISSUED_AT,
    expiresAt: EXPIRES_AT,
  };
}

function settings(userId: string): UserSettings {
  return {
    userId,
    hashtagCount: 5,
    articleLength: "medium",
    blendMode: "balanced",
    defaultPersona: "friendly",
    autoPostingEnabled: false,
    createdAt: CREATED_AT,
    updatedAt: CREATED_AT,
  };
}

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function noContent(): Response {
  return new Response(null, { status: 204 });
}

function authorization(options?: RequestInit): string {
  const headers = options?.headers as Record<string, string> | undefined;
  return headers?.authorization ?? "";
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

describe("StoreProvider multi-account isolation", () => {
  let container: HTMLDivElement;
  let root: Root;
  let currentStore!: ReturnType<typeof useStore>;

  function Probe() {
    currentStore = useStore();
    return <output data-user={currentStore.session?.user.userId ?? "signed-out"} />;
  }

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    window.sessionStorage.clear();
    location.hash = "#/";
    configureAuth(null, () => undefined);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
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

  it("keeps other logged-in accounts while switching and logging out the current one", async () => {
    const accountA = authSession("user-a", "a@example.com", "계정 A");
    const accountB = authSession("user-b", "b@example.com", "계정 B");
    window.sessionStorage.setItem(ACTIVE_SESSION_KEY, JSON.stringify(accountA));

    const fetchMock = vi.fn(
      async (input: string | URL | Request, _options?: RequestInit) => {
        const path = String(input);
        if (path === "/personas") {
          return jsonResponse(200, [
            {
              personaId: "friendly",
              kind: "preset",
              name: "친근한",
              description: "친근한 문체",
              prompt: "friendly",
            },
            {
              personaId: "custom",
              kind: "custom",
              name: "직접 입력",
              description: "직접 설정",
            },
          ]);
        }
        if (path.includes("/settings")) return jsonResponse(404, { message: "not found" });
        if (path === "/posts?view=summary") return jsonResponse(200, []);
        if (path === "/auth/logout") return noContent();
        throw new Error(`Unexpected request: ${path}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await renderStore();
    expect(currentStore.session?.user.userId).toBe("user-a");
    const staleSetTaskFromAccountA = currentStore.setTask;
    const activeSessionBeforeChooser = window.sessionStorage.getItem(ACTIVE_SESSION_KEY);
    expect(getAccountSessions().sessions.map((item) => item.user.userId)).toEqual([
      "user-a",
    ]);
    const accountADraft = {
      postId: "post-owned-by-a",
      status: "INPUT",
    } as BlogTask;
    await act(async () => currentStore.setTask(accountADraft));
    expect(currentStore.task?.postId).toBe("post-owned-by-a");

    await act(async () => currentStore.openAccountChooser());
    expect(currentStore.accountChooserOpen).toBe(true);
    expect(currentStore.session?.user.userId).toBe("user-a");
    expect(window.sessionStorage.getItem(ACTIVE_SESSION_KEY)).toBe(
      activeSessionBeforeChooser,
    );
    expect(getAccountSessions().sessions.map((item) => item.user.userId)).toEqual([
      "user-a",
    ]);
    expect(
      fetchMock.mock.calls.some(([path]) => String(path) === "/auth/logout"),
    ).toBe(false);
    expect(currentStore.task?.postId).toBe("post-owned-by-a");

    await act(async () => currentStore.closeAccountChooser());
    expect(currentStore.accountChooserOpen).toBe(false);
    expect(currentStore.session?.user.userId).toBe("user-a");
    expect(currentStore.task?.postId).toBe("post-owned-by-a");
    // 같은 글의 갱신은 계정 A가 로그인해 있는 동안 그대로 반영된다. postId를 바꾸지
    // 않는 이유는 작업실이 열어 둔 글과 다른 postId는 이제 화면을 덮어쓰지 못하기
    // 때문이다(글 A의 폴링이 방금 연 글 B를 덮어쓰던 문제). 여기서 확인하려는 것은
    // 계정 전환창을 여닫아도 예전에 잡아 둔 setTask가 계속 살아 있다는 것이다.
    await act(async () =>
      staleSetTaskFromAccountA({
        postId: "post-owned-by-a",
        status: "REFERENCE_PROCESSING",
      } as BlogTask),
    );
    expect(currentStore.task?.postId).toBe("post-owned-by-a");
    expect(currentStore.task?.status).toBe("REFERENCE_PROCESSING");

    await act(async () => currentStore.openAccountChooser());

    await act(async () => {
      await currentStore.signIn(accountB);
    });
    expect(currentStore.accountChooserOpen).toBe(false);
    expect(currentStore.session?.user.userId).toBe("user-b");
    expect(
      new Set(getAccountSessions().sessions.map((item) => item.user.userId)),
    ).toEqual(new Set(["user-a", "user-b"]));

    await act(async () => {
      staleSetTaskFromAccountA({
        postId: "post-from-account-a",
        status: "INPUT",
      } as BlogTask);
    });
    expect(currentStore.task).toBeNull();

    await act(async () => currentStore.signOut());
    await settleEffects();

    expect(currentStore.session).toBeNull();
    expect(getAccountSessions().sessions.map((item) => item.user.userId)).toEqual([
      "user-a",
    ]);
    const logoutCall = fetchMock.mock.calls.find(
      ([path]) => String(path) === "/auth/logout",
    );
    expect(authorization(logoutCall?.[1])).toBe("Bearer token-user-b");
    expect(
      JSON.parse(
        window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY) ?? "[]",
      ),
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ userId: "user-a" }),
        expect.objectContaining({ userId: "user-b" }),
      ]),
    );
  });

  it("does not let account A's late settings response overwrite account B", async () => {
    const accountA = authSession("user-a", "a@example.com", "계정 A");
    const accountB = authSession("user-b", "b@example.com", "계정 B");
    const accountASettings = deferred<Response>();
    window.sessionStorage.setItem(ACTIVE_SESSION_KEY, JSON.stringify(accountA));
    const seeded = saveAccountSession(accountA, new Date(ISSUED_AT));
    expect(seeded.ok).toBe(true);

    const fetchMock = vi.fn(
      async (input: string | URL | Request, options?: RequestInit) => {
        const path = String(input);
        if (path === "/personas") {
          return jsonResponse(200, [
            {
              personaId: "friendly",
              kind: "preset",
              name: "친근한",
              description: "친근한 문체",
              prompt: "friendly",
            },
            {
              personaId: "custom",
              kind: "custom",
              name: "직접 입력",
              description: "직접 설정",
            },
          ]);
        }
        if (path === "/users/user-a/settings") return accountASettings.promise;
        if (path === "/users/user-b/settings") {
          expect(authorization(options)).toBe("Bearer token-user-b");
          return jsonResponse(200, settings("user-b"));
        }
        if (path === "/posts?view=summary") return jsonResponse(200, []);
        throw new Error(`Unexpected request: ${path}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await renderStore();
    expect(
      fetchMock.mock.calls.some(([path]) => String(path) === "/users/user-a/settings"),
    ).toBe(true);

    await act(async () => {
      await currentStore.signIn(accountB);
    });
    expect(currentStore.session?.user.userId).toBe("user-b");
    expect(currentStore.settings?.userId).toBe("user-b");

    accountASettings.resolve(jsonResponse(200, settings("user-a")));
    await settleEffects();

    expect(currentStore.session?.user.userId).toBe("user-b");
    expect(currentStore.settings?.userId).toBe("user-b");
  });
});
