import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  clearSession,
  configureAuth,
  friendlyError,
  loadSession,
  request,
  requestPublic,
  requestWithSessionToken,
  requestWithToken,
  storeSession,
} from "./client";
import type { AuthSession } from "./types";

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

describe("API authentication isolation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    configureAuth(null, () => undefined);
  });

  it("rejects a malformed legacy active session before it can enter the account vault", () => {
    window.localStorage.setItem(
      "blog-it:session",
      JSON.stringify({
        user: { userId: "spoofed-user" },
        accessToken: "token",
        issuedAt: "2099-01-01T00:00:00.000Z",
        expiresAt: "2099-01-08T00:00:00.000Z",
      }),
    );

    expect(loadSession()).toBeNull();
    expect(window.localStorage.getItem("blog-it:session")).toBeNull();
    expect(window.sessionStorage.getItem("blog-it:session")).toBeNull();
  });

  it("stores active authentication only for the current tab", () => {
    const session: AuthSession = {
      user: {
        userId: "user-a",
        email: "a@example.com",
        nickname: "A",
        createdAt: "2099-01-01T00:00:00.000Z",
        updatedAt: "2099-01-01T00:00:00.000Z",
      },
      accessToken: "sensitive-token",
      issuedAt: "2099-01-01T00:00:00.000Z",
      expiresAt: "2099-01-08T00:00:00.000Z",
    };

    expect(storeSession(session)).toBe(true);
    expect(window.localStorage.getItem("blog-it:session")).toBeNull();
    expect(loadSession()).toEqual(session);
    expect(window.sessionStorage.getItem("blog-it:session")).toContain("sensitive-token");

    expect(clearSession()).toBe(true);
    expect(window.sessionStorage.getItem("blog-it:session")).toBeNull();
  });

  it("moves a valid legacy active session and erases its persistent copy", () => {
    const legacy: AuthSession = {
      user: {
        userId: "legacy-user",
        email: "legacy@example.com",
        nickname: "Legacy",
        createdAt: "2099-01-01T00:00:00.000Z",
        updatedAt: "2099-01-01T00:00:00.000Z",
      },
      accessToken: "legacy-token",
      issuedAt: "2099-01-01T00:00:00.000Z",
      expiresAt: "2099-01-08T00:00:00.000Z",
    };
    window.localStorage.setItem("blog-it:session", JSON.stringify(legacy));

    expect(loadSession()).toEqual(legacy);
    expect(window.localStorage.getItem("blog-it:session")).toBeNull();
    expect(window.sessionStorage.getItem("blog-it:session")).not.toBeNull();
  });

  it("does not let account A's late 401 sign out account B", async () => {
    const oldResponse = deferred<Response>();
    const oldUnauthorized = vi.fn();
    const newUnauthorized = vi.fn();
    const fetchMock = vi.fn().mockReturnValueOnce(oldResponse.promise);
    vi.stubGlobal("fetch", fetchMock);

    configureAuth("token-a", oldUnauthorized);
    const pending = request("/posts");
    configureAuth("token-b", newUnauthorized);
    oldResponse.resolve(jsonResponse(401, { message: "expired" }));

    await expect(pending).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledWith(
      "/posts",
      expect.objectContaining({
        headers: expect.objectContaining({ authorization: "Bearer token-a" }),
      }),
    );
    expect(oldUnauthorized).not.toHaveBeenCalled();
    expect(newUnauthorized).not.toHaveBeenCalled();
  });

  it("invokes the current account's unauthorized handler exactly once", async () => {
    const unauthorized = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { message: "expired" })));

    configureAuth("token-b", unauthorized);

    await expect(request("/posts")).rejects.toBeInstanceOf(ApiError);
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });

  it("validates a candidate token without replacing or signing out the active account", async () => {
    const unauthorized = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(401, { message: "candidate expired" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    configureAuth("active-token", unauthorized);

    await expect(requestWithToken("/auth/me", "candidate-token")).rejects.toBeInstanceOf(
      ApiError,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/me",
      expect.objectContaining({
        headers: expect.objectContaining({ authorization: "Bearer candidate-token" }),
      }),
    );
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("signs out when pinned work for the current account gets a 401", async () => {
    const unauthorized = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(401, { message: "expired" })),
    );
    configureAuth("active-token", unauthorized);

    await expect(
      requestWithSessionToken("/posts/post-a/draft", "active-token", {
        method: "PUT",
        body: { title: "제목", html: "<p>본문</p>" },
      }),
    ).rejects.toBeInstanceOf(ApiError);

    expect(unauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not let pinned work from the previous account sign out the new account", async () => {
    const unauthorized = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(401, { message: "expired" })),
    );
    configureAuth("token-b", unauthorized);

    await expect(
      requestWithSessionToken("/posts/post-a/draft", "token-a", {
        method: "PUT",
        body: { title: "제목", html: "<p>본문</p>" },
      }),
    ).rejects.toBeInstanceOf(ApiError);

    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("does not attach or invalidate the active account during a public login request", async () => {
    const unauthorized = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(401, { message: "wrong password" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    configureAuth("active-account-token", unauthorized);

    await expect(
      requestPublic("/auth/login", {
        method: "POST",
        body: { email: "other@example.com", password: "wrong" },
      }),
    ).rejects.toBeInstanceOf(ApiError);

    const requestOptions = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(requestOptions.headers).not.toHaveProperty("authorization");
    expect(unauthorized).not.toHaveBeenCalled();
  });
});

describe("실패 사유를 뭉뜽그리지 않는다", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    configureAuth(null, () => {});
  });

  it("서버가 사유를 말해 주면 그 말을 그대로 쓴다", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(400, { success: false, message: "글 목적을 하나 이상 선택해 주세요." }),
      ),
    );

    await expect(request("/posts")).rejects.toThrow("글 목적을 하나 이상 선택해 주세요.");
  });

  it("설정 화면의 항목별 오류는 이어 붙인다", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(400, {
          success: false,
          errors: [{ message: "닉네임이 너무 깁니다." }, { message: "해시태그 수가 큽니다." }],
        }),
      ),
    );

    await expect(request("/users/u1/settings")).rejects.toThrow(
      "닉네임이 너무 깁니다. 해시태그 수가 큽니다.",
    );
  });

  it("본문 없는 5xx는 '서버가 꺼져 있다'로 읽는다", async () => {
    // API 서버가 안 떠 있으면 개발 서버의 프록시가 대신 500을 돌려주고 본문은 비어 있다.
    // 예전에는 이것이 "요청에 실패했습니다"로 뭉개져, 서버가 꺼진 줄 알 수 없었다.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 500 })));

    await expect(request("/personas")).rejects.toThrow(/서버와의 연결이 끊어졌습니다/);
    await expect(request("/personas")).rejects.toThrow(/HTTP 500/);
  });

  it("상태 코드마다 다른 말을 한다", async () => {
    const cases: [number, RegExp][] = [
      [401, /로그인이 만료/],
      [403, /권한이 없습니다/],
      [404, /없는 요청/],
      [413, /너무 큽니다/],
      [429, /너무 잦습니다/],
      [418, /HTTP 418/],
    ];
    for (const [status, expected] of cases) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status })));
      await expect(request("/personas")).rejects.toThrow(expected);
    }
  });

  it("연결 자체가 안 되면 그렇게 말한다", async () => {
    // 화면이 보여 주는 것은 friendlyError를 거친 문장이다(store.reportError).
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await request("/personas").catch((caught) => caught);

    expect(friendlyError(error)).toMatch(/서버와의 연결이 끊어졌습니다/);
  });

  it("화면 코드가 터진 것은 그 내용을 그대로 보여 준다", () => {
    // "예상치 못한 오류가 발생했습니다"만 보여 주면 개발자 도구를 열기 전까지 아무것도
    // 알 수 없다 — 실제로 그것 때문에 원인을 엉뚱한 곳에서 찾았다.
    const message = friendlyError(new TypeError("Cannot read properties of undefined"));

    expect(message).toContain("Cannot read properties of undefined");
  });
});
