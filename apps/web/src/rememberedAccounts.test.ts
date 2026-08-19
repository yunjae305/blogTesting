import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearSession, storeSession } from "./api/client";
import type { AuthSession } from "./api/types";
import {
  clearRememberedAccounts,
  getRememberedAccounts,
  MAX_REMEMBERED_ACCOUNTS,
  REMEMBERED_ACCOUNTS_STORAGE_KEY,
  removeRememberedAccount,
  saveRememberedAccount,
  sortRememberedAccountsByLastUsed,
  type RememberedAccount,
  type RememberedAccountInput,
} from "./rememberedAccounts";

function account(
  userId: string,
  email: string,
  lastUsedAt: string,
  displayName = userId,
): RememberedAccount {
  return { userId, email, displayName, profileImage: null, lastUsedAt };
}

describe("remembered accounts storage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("returns an empty list when nothing has been remembered", () => {
    expect(getRememberedAccounts()).toEqual([]);
  });

  it("moves legacy account metadata into tab storage and erases the persistent email", () => {
    const legacy = [
      account("legacy-user", "legacy@example.com", "2026-07-28T07:00:00.000Z"),
    ];
    window.localStorage.setItem(
      REMEMBERED_ACCOUNTS_STORAGE_KEY,
      JSON.stringify(legacy),
    );

    expect(getRememberedAccounts()).toEqual(legacy);
    expect(window.localStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).not.toBeNull();
  });

  it("stores only the five allowed fields and never persists credentials or tokens", () => {
    const input = {
      userId: " user-1 ",
      email: " blogger@example.com ",
      displayName: " Blogger ",
      profileImage: " https://example.com/profile.png ",
      password: "do-not-store",
      accessToken: "also-do-not-store",
    } as RememberedAccountInput & { password: string; accessToken: string };

    const result = saveRememberedAccount(input, new Date("2026-07-28T07:00:00.000Z"));

    expect(result).toEqual([
      {
        userId: "user-1",
        email: "blogger@example.com",
        displayName: "Blogger",
        profileImage: "https://example.com/profile.png",
        lastUsedAt: "2026-07-28T07:00:00.000Z",
      },
    ]);

    const raw = window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY);
    expect(raw).not.toContain("do-not-store");
    expect(raw).not.toContain("also-do-not-store");
    expect(window.localStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).toBeNull();
    expect(Object.keys(JSON.parse(raw ?? "[]")[0]).sort()).toEqual(
      ["userId", "email", "displayName", "profileImage", "lastUsedAt"].sort(),
    );
  });

  it("falls back to the email prefix and a null profile image", () => {
    expect(
      saveRememberedAccount(
        {
          userId: "user-1",
          email: "writer@example.com",
          displayName: " ",
          profileImage: " ",
        },
        new Date("2026-07-28T07:00:00.000Z"),
      ),
    ).toEqual([
      {
        userId: "user-1",
        email: "writer@example.com",
        displayName: "writer",
        profileImage: null,
        lastUsedAt: "2026-07-28T07:00:00.000Z",
      },
    ]);
  });

  it("updates rather than duplicates an account with the same userId", () => {
    saveRememberedAccount(
      { userId: "user-1", email: "old@example.com", displayName: "Old" },
      new Date("2026-07-28T05:00:00.000Z"),
    );

    const result = saveRememberedAccount(
      { userId: "user-1", email: "new@example.com", displayName: "New" },
      new Date("2026-07-28T07:00:00.000Z"),
    );

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      userId: "user-1",
      email: "new@example.com",
      displayName: "New",
      lastUsedAt: "2026-07-28T07:00:00.000Z",
    });
  });

  it("deduplicates the same normalized email even when the userId changes", () => {
    saveRememberedAccount(
      { userId: "old-id", email: "same@example.com" },
      new Date("2026-07-28T05:00:00.000Z"),
    );

    const result = saveRememberedAccount(
      { userId: "new-id", email: " SAME@example.com " },
      new Date("2026-07-28T07:00:00.000Z"),
    );

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({ userId: "new-id", email: "SAME@example.com" });
  });

  it("repairs existing duplicates by keeping the newest matching identity", () => {
    window.sessionStorage.setItem(
      REMEMBERED_ACCOUNTS_STORAGE_KEY,
      JSON.stringify([
        account("old-id", "shared@example.com", "2026-07-28T05:00:00.000Z"),
        account("new-id", "shared@example.com", "2026-07-28T07:00:00.000Z"),
        account("new-id", "other@example.com", "2026-07-28T04:00:00.000Z"),
      ]),
    );

    const result = getRememberedAccounts();

    expect(result).toEqual([
      account("new-id", "shared@example.com", "2026-07-28T07:00:00.000Z"),
    ]);
    expect(JSON.parse(window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY) ?? "[]")).toEqual(
      result,
    );
  });

  it("sorts newest first and removes the oldest account beyond the five-account limit", () => {
    for (let index = 1; index <= MAX_REMEMBERED_ACCOUNTS + 1; index += 1) {
      saveRememberedAccount(
        { userId: `user-${index}`, email: `user-${index}@example.com` },
        new Date(`2026-07-28T0${index}:00:00.000Z`),
      );
    }

    expect(getRememberedAccounts().map(({ userId }) => userId)).toEqual([
      "user-6",
      "user-5",
      "user-4",
      "user-3",
      "user-2",
    ]);
  });

  it("sorts without mutating the caller's array", () => {
    const original = [
      account("older", "older@example.com", "2026-07-28T05:00:00.000Z"),
      account("newer", "newer@example.com", "2026-07-28T07:00:00.000Z"),
    ];

    expect(sortRememberedAccountsByLastUsed(original).map(({ userId }) => userId)).toEqual([
      "newer",
      "older",
    ]);
    expect(original.map(({ userId }) => userId)).toEqual(["older", "newer"]);
  });

  it.each([
    ["malformed JSON", "{broken"],
    ["a non-array payload", JSON.stringify({ accounts: [] })],
    [
      "an array containing an invalid record",
      JSON.stringify([
        account("valid", "valid@example.com", "2026-07-28T07:00:00.000Z"),
        { userId: "invalid", email: "invalid@example.com", lastUsedAt: "not-a-date" },
      ]),
    ],
  ])("strictly resets %s", (_label, raw) => {
    window.sessionStorage.setItem(REMEMBERED_ACCOUNTS_STORAGE_KEY, raw);

    expect(getRememberedAccounts()).toEqual([]);
    expect(window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).toBeNull();
  });

  it("normalizes missing optional fields and strips unexpected stored properties", () => {
    window.sessionStorage.setItem(
      REMEMBERED_ACCOUNTS_STORAGE_KEY,
      JSON.stringify([
        {
          userId: "user-1",
          email: "writer@example.com",
          lastUsedAt: "2026-07-28T07:00:00Z",
          accessToken: "must-disappear",
        },
      ]),
    );

    const result = getRememberedAccounts();

    expect(result).toEqual([
      {
        userId: "user-1",
        email: "writer@example.com",
        displayName: "writer",
        profileImage: null,
        lastUsedAt: "2026-07-28T07:00:00.000Z",
      },
    ]);
    expect(window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).not.toContain(
      "must-disappear",
    );
  });

  it("removes one local account or clears the complete remembered list", () => {
    saveRememberedAccount(
      { userId: "user-1", email: "one@example.com" },
      new Date("2026-07-28T06:00:00.000Z"),
    );
    saveRememberedAccount(
      { userId: "user-2", email: "two@example.com" },
      new Date("2026-07-28T07:00:00.000Z"),
    );

    const removal = removeRememberedAccount(" user-2 ");
    expect(removal.removed).toBe(true);
    expect(removal.accounts.map(({ userId }) => userId)).toEqual(["user-1"]);
    expect(getRememberedAccounts().map(({ userId }) => userId)).toEqual(["user-1"]);

    clearRememberedAccounts();
    expect(getRememberedAccounts()).toEqual([]);
    expect(window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).toBeNull();
  });

  it("clearing the login session preserves remembered accounts", () => {
    const remembered = saveRememberedAccount(
      { userId: "user-1", email: "one@example.com", displayName: "One" },
      new Date("2026-07-28T07:00:00.000Z"),
    );
    const session: AuthSession = {
      user: {
        userId: "user-1",
        email: "one@example.com",
        nickname: "One",
        createdAt: "2026-07-28T06:00:00.000Z",
        updatedAt: "2026-07-28T07:00:00.000Z",
      },
      accessToken: "session-token",
      issuedAt: "2026-07-28T07:00:00.000Z",
      expiresAt: "2026-07-28T08:00:00.000Z",
    };
    storeSession(session);

    expect(window.sessionStorage.getItem("blog-it:session")).not.toBeNull();
    expect(window.localStorage.getItem("blog-it:session")).toBeNull();

    clearSession();

    expect(window.sessionStorage.getItem("blog-it:session")).toBeNull();
    expect(getRememberedAccounts()).toEqual(remembered);
    expect(window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY)).not.toBeNull();
  });

  it("is safe when rendered without a browser window", () => {
    vi.stubGlobal("window", undefined);

    expect(getRememberedAccounts()).toEqual([]);
    expect(
      saveRememberedAccount(
        { userId: "user-1", email: "one@example.com" },
        new Date("2026-07-28T07:00:00.000Z"),
      ),
    ).toEqual([]);
    expect(removeRememberedAccount("user-1")).toEqual({
      accounts: [],
      removed: false,
    });
    expect(() => clearRememberedAccounts()).not.toThrow();
  });

  it("does not throw when browser storage access is blocked", () => {
    const blockedStorage = {
      getItem: () => {
        throw new DOMException("blocked");
      },
      setItem: () => {
        throw new DOMException("blocked");
      },
      removeItem: () => {
        throw new DOMException("blocked");
      },
    } as unknown as Storage;
    vi.stubGlobal("window", { sessionStorage: blockedStorage });

    expect(getRememberedAccounts()).toEqual([]);
    expect(
      saveRememberedAccount(
        { userId: "user-1", email: "one@example.com" },
        new Date("2026-07-28T07:00:00.000Z"),
      ),
    ).toEqual([]);
    expect(removeRememberedAccount("user-1")).toEqual({
      accounts: [],
      removed: false,
    });
    expect(() => clearRememberedAccounts()).not.toThrow();
  });
});
