import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthSession } from "./api/types";
import {
  ACCOUNT_SESSIONS_STORAGE_KEY,
  clearAccountSessions,
  findAccountSession,
  getAccountSessions,
  MAX_ACCOUNT_SESSIONS,
  removeAccountSession,
  saveAccountSession,
} from "./accountSessions";

const NOW = new Date("2026-07-29T07:00:00.000Z");

function authSession(
  userId: string,
  email: string,
  overrides: Partial<AuthSession> = {},
): AuthSession {
  return {
    user: {
      userId,
      email,
      nickname: `name-${userId}`,
      createdAt: "2026-07-01T00:00:00.000Z",
      updatedAt: "2026-07-29T00:00:00.000Z",
    },
    accessToken: `token-${userId}`,
    issuedAt: "2026-07-28T07:00:00.000Z",
    expiresAt: "2026-08-05T07:00:00.000Z",
    ...overrides,
  };
}

function stored(
  session: AuthSession,
  lastUsedAt = "2026-07-29T07:00:00.000Z",
): Record<string, unknown> {
  return { ...session, lastUsedAt };
}

function setRaw(value: unknown): void {
  window.sessionStorage.setItem(ACCOUNT_SESSIONS_STORAGE_KEY, JSON.stringify(value));
}

describe("account session vault", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("returns an explicit successful empty result when the vault does not exist", () => {
    expect(getAccountSessions(NOW)).toEqual({
      ok: true,
      sessions: [],
      changed: false,
    });
  });

  it("moves a legacy persistent vault into tab storage and erases the source", () => {
    const legacy = [stored(authSession("legacy-user", "legacy@example.com"))];
    window.localStorage.setItem(ACCOUNT_SESSIONS_STORAGE_KEY, JSON.stringify(legacy));

    const result = getAccountSessions(NOW);

    expect(result).toMatchObject({ ok: true, sessions: legacy });
    expect(window.localStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).not.toBeNull();
  });

  it("stores the exact AuthSession projection plus lastUsedAt", () => {
    const input = {
      ...authSession("user-a", "a@example.com"),
      password: "do-not-store",
      refreshToken: "do-not-store-either",
      user: {
        ...authSession("user-a", "a@example.com").user,
        passwordHash: "also-secret",
        role: "admin",
      },
    } as AuthSession & {
      password: string;
      refreshToken: string;
      user: AuthSession["user"] & { passwordHash: string; role: string };
    };

    const result = saveAccountSession(input, NOW);

    expect(result.ok).toBe(true);
    expect(result.sessions).toEqual([
      {
        user: {
          userId: "user-a",
          email: "a@example.com",
          nickname: "name-user-a",
          createdAt: "2026-07-01T00:00:00.000Z",
          updatedAt: "2026-07-29T00:00:00.000Z",
        },
        accessToken: "token-user-a",
        issuedAt: "2026-07-28T07:00:00.000Z",
        expiresAt: "2026-08-05T07:00:00.000Z",
        lastUsedAt: "2026-07-29T07:00:00.000Z",
      },
    ]);

    const raw = window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) ?? "";
    expect(raw).not.toContain("do-not-store");
    expect(raw).not.toContain("passwordHash");
    expect(raw).not.toContain("admin");
    expect(window.localStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).toBeNull();

    const parsed = JSON.parse(raw)[0];
    expect(Object.keys(parsed)).toEqual([
      "user",
      "accessToken",
      "issuedAt",
      "expiresAt",
      "lastUsedAt",
    ]);
    expect(Object.keys(parsed.user)).toEqual([
      "userId",
      "email",
      "nickname",
      "createdAt",
      "updatedAt",
    ]);
  });

  it.each([
    ["missing user", { ...authSession("user-a", "a@example.com"), user: undefined }],
    ["empty token", { ...authSession("user-a", "a@example.com"), accessToken: " " }],
    ["invalid issue date", { ...authSession("user-a", "a@example.com"), issuedAt: "bad" }],
    [
      "expiry before issue",
      {
        ...authSession("user-a", "a@example.com"),
        issuedAt: "2026-08-05T07:00:00.000Z",
        expiresAt: "2026-08-04T07:00:00.000Z",
      },
    ],
    [
      "expiry equal to now",
      { ...authSession("user-a", "a@example.com"), expiresAt: NOW.toISOString() },
    ],
  ])("rejects a session with %s without writing it", (_label, value) => {
    const result = saveAccountSession(value as AuthSession, NOW);

    expect(result).toEqual({
      ok: false,
      sessions: [],
      changed: false,
      reason: "invalid-session",
    });
    expect(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).toBeNull();
  });

  it("replaces a matching userId and moves the refreshed token to the front", () => {
    saveAccountSession(authSession("user-a", "old@example.com"), new Date("2026-07-29T05:00:00Z"));
    saveAccountSession(authSession("user-b", "b@example.com"), new Date("2026-07-29T06:00:00Z"));

    const result = saveAccountSession(
      authSession("user-a", "new@example.com", { accessToken: "fresh-token" }),
      NOW,
    );

    expect(result.sessions.map(({ user }) => user.userId)).toEqual(["user-a", "user-b"]);
    expect(result.sessions[0].accessToken).toBe("fresh-token");
    expect(result.sessions[0].user.email).toBe("new@example.com");
  });

  it("replaces a case-insensitive email match even when the userId changes", () => {
    saveAccountSession(authSession("old-id", "same@example.com"), new Date("2026-07-29T06:00:00Z"));

    const result = saveAccountSession(authSession("new-id", " SAME@example.com "), NOW);

    expect(result.sessions).toHaveLength(1);
    expect(result.sessions[0].user).toMatchObject({
      userId: "new-id",
      email: "SAME@example.com",
    });
  });

  it("keeps only the five most recently used distinct sessions", () => {
    for (let index = 1; index <= MAX_ACCOUNT_SESSIONS + 1; index += 1) {
      saveAccountSession(
        authSession(`user-${index}`, `user-${index}@example.com`),
        new Date(`2026-07-29T0${index}:00:00.000Z`),
      );
    }

    expect(
      getAccountSessions(new Date("2026-07-29T07:00:00.000Z")).sessions.map(
        ({ user }) => user.userId,
      ),
    ).toEqual(["user-6", "user-5", "user-4", "user-3", "user-2"]);
  });

  it("repairs order and OR-deduplicates an existing vault by keeping the newest record", () => {
    setRaw([
      stored(authSession("old-id", "shared@example.com"), "2026-07-29T04:00:00Z"),
      stored(authSession("new-id", "shared@example.com"), "2026-07-29T06:00:00Z"),
      stored(authSession("new-id", "other@example.com"), "2026-07-29T05:00:00Z"),
    ]);

    const result = getAccountSessions(NOW);

    expect(result.ok).toBe(true);
    expect(result.changed).toBe(true);
    expect(result.sessions).toHaveLength(1);
    expect(result.sessions[0].user).toMatchObject({
      userId: "new-id",
      email: "shared@example.com",
    });
    expect(JSON.parse(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) ?? "[]")).toEqual(
      result.sessions,
    );
  });

  it("removes expired and malformed records while retaining valid sessions", () => {
    setRaw([
      stored(
        authSession("expired", "expired@example.com", {
          expiresAt: NOW.toISOString(),
        }),
        "2026-07-29T06:00:00Z",
      ),
      { nope: true },
      {
        ...stored(authSession("valid", "valid@example.com"), "2026-07-29T05:00:00Z"),
        password: "strip-me",
      },
    ]);

    const result = getAccountSessions(NOW);

    expect(result).toMatchObject({ ok: true, changed: true });
    expect(result.sessions.map(({ user }) => user.userId)).toEqual(["valid"]);
    expect(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).not.toContain("strip-me");
  });

  it.each([
    ["malformed JSON", "{broken"],
    ["a non-array payload", JSON.stringify({ sessions: [] })],
  ])("safely resets %s", (_label, raw) => {
    window.sessionStorage.setItem(ACCOUNT_SESSIONS_STORAGE_KEY, raw);

    expect(getAccountSessions(NOW)).toEqual({
      ok: true,
      sessions: [],
      changed: true,
    });
    expect(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).toBeNull();
  });

  it("finds a usable session by either userId or normalized email", () => {
    saveAccountSession(authSession("user-a", "a@example.com"), NOW);

    expect(findAccountSession("user-a", NOW)).toMatchObject({
      ok: true,
      session: { accessToken: "token-user-a" },
    });
    expect(findAccountSession(" A@EXAMPLE.COM ", NOW)).toMatchObject({
      ok: true,
      session: { accessToken: "token-user-a" },
    });
    expect(findAccountSession("missing", NOW)).toMatchObject({
      ok: true,
      session: null,
    });
  });

  it("removes by userId or normalized email and reports a no-op explicitly", () => {
    saveAccountSession(authSession("user-a", "a@example.com"), new Date("2026-07-29T06:00:00Z"));
    saveAccountSession(authSession("user-b", "b@example.com"), NOW);

    const removed = removeAccountSession(" B@EXAMPLE.COM ", NOW);
    expect(removed).toMatchObject({ ok: true, changed: true });
    expect(removed.sessions.map(({ user }) => user.userId)).toEqual(["user-a"]);

    const missing = removeAccountSession("missing", NOW);
    expect(missing).toEqual({
      ok: true,
      sessions: removed.sessions,
      changed: false,
    });
  });

  it("clears only this vault and reports whether anything changed", () => {
    window.localStorage.setItem("blog-it:session", "legacy");
    window.localStorage.setItem("blogit.rememberedAccounts.v1", "metadata");
    saveAccountSession(authSession("user-a", "a@example.com"), NOW);

    expect(clearAccountSessions()).toEqual({
      ok: true,
      sessions: [],
      changed: true,
    });
    expect(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.getItem("blog-it:session")).toBe("legacy");
    expect(window.localStorage.getItem("blogit.rememberedAccounts.v1")).toBe("metadata");
    expect(clearAccountSessions()).toEqual({
      ok: true,
      sessions: [],
      changed: false,
    });
  });

  it("does not mutate its input or let returned-object mutation alter persisted data", () => {
    const input = authSession("user-a", "a@example.com");
    const before = structuredClone(input);
    const saved = saveAccountSession(input, NOW);

    expect(input).toEqual(before);
    saved.sessions[0].user.nickname = "mutated outside";
    saved.sessions[0].accessToken = "mutated outside";

    const loaded = getAccountSessions(NOW);
    expect(loaded.sessions[0].user.nickname).toBe("name-user-a");
    expect(loaded.sessions[0].accessToken).toBe("token-user-a");
  });

  it("returns storage-unavailable for every API outside a browser", () => {
    vi.stubGlobal("window", undefined);

    expect(getAccountSessions(NOW)).toMatchObject({
      ok: false,
      reason: "storage-unavailable",
    });
    expect(saveAccountSession(authSession("user-a", "a@example.com"), NOW)).toMatchObject({
      ok: false,
      reason: "storage-unavailable",
    });
    expect(findAccountSession("user-a", NOW)).toMatchObject({
      ok: false,
      session: null,
      reason: "storage-unavailable",
    });
    expect(removeAccountSession("user-a", NOW)).toMatchObject({
      ok: false,
      reason: "storage-unavailable",
    });
    expect(clearAccountSessions()).toMatchObject({
      ok: false,
      reason: "storage-unavailable",
    });
  });

  it("returns storage-unavailable when the sessionStorage getter throws", () => {
    const blockedWindow = {};
    Object.defineProperty(blockedWindow, "sessionStorage", {
      get() {
        throw new DOMException("blocked");
      },
    });
    vi.stubGlobal("window", blockedWindow);

    expect(getAccountSessions(NOW)).toMatchObject({
      ok: false,
      reason: "storage-unavailable",
    });
  });

  it("does not overwrite unknown data when reading storage fails", () => {
    const storage = {
      getItem: vi.fn(() => {
        throw new DOMException("read blocked");
      }),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    } as unknown as Storage;
    vi.stubGlobal("window", { sessionStorage: storage });

    expect(getAccountSessions(NOW)).toMatchObject({
      ok: false,
      reason: "storage-read-failed",
    });
    expect(saveAccountSession(authSession("user-a", "a@example.com"), NOW)).toMatchObject({
      ok: false,
      reason: "storage-read-failed",
    });
    expect(removeAccountSession("user-a", NOW)).toMatchObject({
      ok: false,
      reason: "storage-read-failed",
    });
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.removeItem).not.toHaveBeenCalled();
  });

  it("returns the previous sessions when a save or removal write fails", () => {
    const existing = [stored(authSession("user-a", "a@example.com"))];
    const storage = {
      getItem: vi.fn(() => JSON.stringify(existing)),
      setItem: vi.fn(() => {
        throw new DOMException("quota");
      }),
      removeItem: vi.fn(),
    } as unknown as Storage;
    vi.stubGlobal("window", { sessionStorage: storage });

    const saveResult = saveAccountSession(authSession("user-b", "b@example.com"), NOW);
    expect(saveResult).toMatchObject({
      ok: false,
      reason: "storage-write-failed",
    });
    expect(saveResult.sessions.map(({ user }) => user.userId)).toEqual(["user-a"]);

    const removeResult = removeAccountSession("user-a", NOW);
    expect(removeResult).toMatchObject({
      ok: false,
      reason: "storage-write-failed",
    });
    expect(removeResult.sessions.map(({ user }) => user.userId)).toEqual(["user-a"]);
  });

  it("reports a failed repair when corrupt data cannot be removed", () => {
    const storage = {
      getItem: vi.fn(() => "{broken"),
      setItem: vi.fn(),
      removeItem: vi.fn(() => {
        throw new DOMException("blocked");
      }),
    } as unknown as Storage;
    vi.stubGlobal("window", { sessionStorage: storage });

    expect(getAccountSessions(NOW)).toEqual({
      ok: false,
      sessions: [],
      changed: false,
      reason: "storage-write-failed",
    });
  });

  it("reports clear read and write failures without throwing", () => {
    const readBlocked = {
      getItem: () => {
        throw new DOMException("blocked");
      },
      removeItem: vi.fn(),
    } as unknown as Storage;
    vi.stubGlobal("window", { sessionStorage: readBlocked });
    expect(clearAccountSessions()).toMatchObject({
      ok: false,
      reason: "storage-read-failed",
    });

    vi.unstubAllGlobals();
    const writeBlocked = {
      getItem: () => "stored",
      removeItem: () => {
        throw new DOMException("blocked");
      },
    } as unknown as Storage;
    vi.stubGlobal("window", { sessionStorage: writeBlocked });
    expect(clearAccountSessions()).toMatchObject({
      ok: false,
      reason: "storage-write-failed",
    });
  });
});
