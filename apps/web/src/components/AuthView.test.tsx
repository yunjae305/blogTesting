import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACCOUNT_SESSIONS_STORAGE_KEY,
  saveAccountSession,
} from "../accountSessions";
import { ApiError } from "../api/client";
import type { AuthSession, PublicUser } from "../api/types";
import {
  REMEMBERED_ACCOUNTS_STORAGE_KEY,
  type RememberedAccount,
} from "../rememberedAccounts";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  requestPublic: vi.fn(),
  requestWithToken: vi.fn(),
  store: {
    session: null as AuthSession | null,
    signIn: vi.fn(),
    signOut: vi.fn(),
    closeAccountChooser: vi.fn(),
    reportError: vi.fn(),
    showToast: vi.fn(),
  },
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    request: mocks.request,
    requestPublic: mocks.requestPublic,
    requestWithToken: mocks.requestWithToken,
  };
});
vi.mock("../store", () => ({ useStore: () => mocks.store }));

import { AuthView } from "./AuthView";

const CREATED_AT = "2026-07-28T00:00:00.000Z";
const ISSUED_AT = "2099-01-01T00:00:00.000Z";
const EXPIRES_AT = "2099-01-08T00:00:00.000Z";

function rememberedAccount(
  userId: string,
  email: string,
  displayName: string,
  lastUsedAt = "2099-01-01T00:00:00.000Z",
): RememberedAccount {
  return {
    userId,
    email,
    displayName,
    profileImage: null,
    lastUsedAt,
  };
}

function authSession(
  userId: string,
  email: string,
  nickname: string,
  token = `token-${userId}`,
): AuthSession {
  return {
    user: {
      userId,
      email,
      nickname,
      createdAt: CREATED_AT,
      updatedAt: CREATED_AT,
    },
    accessToken: token,
    issuedAt: ISSUED_AT,
    expiresAt: EXPIRES_AT,
  };
}

function seedRememberedAccounts(accounts: RememberedAccount[]): void {
  window.sessionStorage.setItem(
    REMEMBERED_ACCOUNTS_STORAGE_KEY,
    JSON.stringify(accounts),
  );
}

function seedLoggedInAccount(session: AuthSession, usedAt = new Date(ISSUED_AT)): void {
  const result = saveAccountSession(session, usedAt);
  if (!result.ok) throw new Error(`Unable to seed account session: ${result.reason}`);
}

function buttonContaining(container: ParentNode, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((candidate) =>
    candidate.textContent?.includes(text),
  );
  if (!button) throw new Error(`Button containing "${text}" was not found`);
  return button;
}

function buttonWithExactText(container: ParentNode, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  if (!button) throw new Error(`Button "${text}" was not found`);
  return button;
}

function requiredInput(container: ParentNode, selector: string): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>(selector);
  if (!input) throw new Error(`Input "${selector}" was not found`);
  return input;
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

async function click(button: HTMLButtonElement): Promise<void> {
  await act(async () => {
    button.click();
    await flush();
  });
}

async function changeInput(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )?.set;
    nativeSetter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submit(form: HTMLFormElement): Promise<void> {
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
  });
}

async function pressKey(key: string, shiftKey = false): Promise<void> {
  await act(async () => {
    window.dispatchEvent(
      new KeyboardEvent("keydown", { key, shiftKey, bubbles: true }),
    );
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("AuthView multi-account flow", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    mocks.store.session = null;
    mocks.store.signIn.mockResolvedValue({ activated: true, persisted: true });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    document.body.classList.remove("auth-account-dialog-open");
    vi.restoreAllMocks();
  });

  async function renderAuthView(): Promise<void> {
    await act(async () => {
      root.render(<AuthView />);
    });
  }

  async function renderAuthViewInStrictMode(): Promise<void> {
    await act(async () => {
      root.render(
        <StrictMode>
          <AuthView />
        </StrictMode>,
      );
    });
  }

  it("keeps the manual login form when this browser has no accounts", async () => {
    await renderAuthView();

    expect(container.querySelector("#authTitle")?.textContent).toBe("로그인");
    expect(requiredInput(container, "#authEmail").value).toBe("demo@blog-it.dev");
    expect(requiredInput(container, "#authPassword").value).toBe("demo1234");
    expect(
      container.querySelector('[aria-label="이 브라우저에서 사용한 계정"]'),
    ).toBeNull();
  });

  it("shows even one remembered account in the chooser and requests its password", async () => {
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);

    await renderAuthView();

    expect(container.querySelector("#authTitle")?.textContent).toBe(
      "계정을 선택하세요",
    );
    expect(container.textContent).toContain("비밀번호 필요");
    expect(container.querySelector("#authPassword")).toBeNull();

    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );

    expect(requiredInput(container, "#authPassword").value).toBe("");
    expect(container.querySelector('input[type="hidden"][name="email"]')).toHaveProperty(
      "value",
      "a@example.com",
    );
    expect(mocks.requestWithToken).not.toHaveBeenCalled();
  });

  it("switches a valid saved session without showing or sending a password", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    mocks.requestWithToken.mockResolvedValue(session.user satisfies PublicUser);

    await renderAuthView();
    expect(container.textContent).toContain("로그인됨");

    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );

    expect(mocks.requestWithToken).toHaveBeenCalledWith(
      "/auth/me",
      session.accessToken,
    );
    expect(mocks.requestPublic).not.toHaveBeenCalledWith(
      "/auth/login",
      expect.anything(),
    );
    expect(mocks.store.signIn).toHaveBeenCalledWith(session);
  });

  it("closes the chooser without resetting work when the current account is selected", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    mocks.store.session = session;
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);

    await renderAuthView();

    expect(container.textContent).toContain("현재 계정");
    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );

    expect(mocks.store.closeAccountChooser).toHaveBeenCalledTimes(1);
    expect(mocks.requestWithToken).not.toHaveBeenCalled();
    expect(mocks.store.signIn).not.toHaveBeenCalled();
  });

  it("keeps account switching active through the StrictMode effect probe", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    mocks.requestWithToken.mockResolvedValue(session.user satisfies PublicUser);

    await renderAuthViewInStrictMode();
    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );

    expect(mocks.store.signIn).toHaveBeenCalledWith(session);
  });

  it("drops only an expired saved session and falls back to password login", async () => {
    const account = rememberedAccount("user-a", "a@example.com", "계정 A");
    seedRememberedAccounts([account]);
    window.sessionStorage.setItem(
      ACCOUNT_SESSIONS_STORAGE_KEY,
      JSON.stringify([
        {
          ...authSession("user-a", "a@example.com", "계정 A"),
          issuedAt: "2025-01-01T00:00:00.000Z",
          expiresAt: "2025-01-02T00:00:00.000Z",
          lastUsedAt: "2025-01-01T12:00:00.000Z",
        },
      ]),
    );

    await renderAuthView();
    expect(container.textContent).toContain("비밀번호 필요");
    expect(
      JSON.parse(
        window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) ?? "[]",
      ),
    ).toEqual([]);

    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );
    expect(requiredInput(container, "#authPassword")).toBe(document.activeElement);
  });

  it("downgrades a server-rejected session to password login without deleting metadata", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    mocks.requestWithToken.mockRejectedValue(
      new ApiError("invalid token", 401, "UNAUTHORIZED"),
    );

    await renderAuthView();
    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );

    expect(requiredInput(container, "#authPassword").value).toBe("");
    expect(
      JSON.parse(
        window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) ?? "[]",
      ),
    ).toEqual([]);
    expect(
      JSON.parse(
        window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY) ?? "[]",
      ),
    ).toHaveLength(1);
  });

  it("keeps a saved session when its validation fails because of the network", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    const networkError = new Error("offline");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    mocks.requestWithToken.mockRejectedValue(networkError);

    await renderAuthView();
    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );

    expect(mocks.store.reportError).toHaveBeenCalledWith(networkError);
    expect(container.querySelector("#authTitle")?.textContent).toBe(
      "계정을 선택하세요",
    );
    expect(
      JSON.parse(
        window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) ?? "[]",
      ),
    ).toHaveLength(1);
  });

  it("sorts multiple accounts by recent use and hands focus to the first row", async () => {
    seedRememberedAccounts([
      rememberedAccount(
        "older",
        "older@example.com",
        "오래된 계정",
        "2099-01-01T00:00:00.000Z",
      ),
      rememberedAccount(
        "newer",
        "newer@example.com",
        "최근 계정",
        "2099-01-02T00:00:00.000Z",
      ),
    ]);

    await renderAuthView();

    const list = container.querySelector<HTMLElement>(
      '[aria-label="이 브라우저에서 사용한 계정"]',
    )!;
    expect(list.textContent!.indexOf("newer@example.com")).toBeLessThan(
      list.textContent!.indexOf("older@example.com"),
    );
    expect(document.activeElement).toBe(
      container.querySelector(
        '[aria-label="최근 계정, newer@example.com 계정 선택"]',
      ),
    );
  });

  it("opens a blank manual form from 다른 계정 사용", async () => {
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    await renderAuthView();

    await click(buttonContaining(container, "다른 계정 사용"));

    expect(requiredInput(container, "#authEmail").value).toBe("");
    expect(requiredInput(container, "#authPassword").value).toBe("");
    expect(requiredInput(container, "#authEmail")).toBe(document.activeElement);
  });

  it("uses the existing login API for an account that needs a password", async () => {
    const session = authSession("user-a", "a@example.com", "최신 계정 A");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    mocks.requestPublic.mockResolvedValue(session);
    await renderAuthView();
    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );
    await changeInput(requiredInput(container, "#authPassword"), "fresh-password");

    await submit(container.querySelector("form")!);

    expect(mocks.requestPublic).toHaveBeenCalledWith("/auth/login", {
      method: "POST",
      body: { email: "a@example.com", password: "fresh-password" },
    });
    expect(mocks.store.signIn).toHaveBeenCalledWith(session);
    expect(JSON.stringify(window.localStorage)).not.toContain("fresh-password");
    expect(JSON.stringify(window.sessionStorage)).not.toContain("fresh-password");
  });

  it("does not report a successful login when activation was superseded", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    mocks.requestPublic.mockResolvedValue(session);
    mocks.store.signIn.mockResolvedValue({ activated: false, persisted: true });
    await renderAuthView();

    await submit(container.querySelector("form")!);

    expect(mocks.store.signIn).toHaveBeenCalledWith(session);
    expect(mocks.store.showToast).not.toHaveBeenCalled();
  });

  it("keeps the selected identity and clears only a rejected password", async () => {
    const loginError = new Error("wrong password");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    mocks.requestPublic.mockRejectedValue(loginError);
    await renderAuthView();
    await click(
      container.querySelector<HTMLButtonElement>(
        '[aria-label="계정 A, a@example.com 계정 선택"]',
      )!,
    );
    await changeInput(requiredInput(container, "#authPassword"), "wrong");

    await submit(container.querySelector("form")!);

    expect(mocks.store.reportError).toHaveBeenCalledWith(loginError);
    expect(container.textContent).toContain("a@example.com");
    expect(requiredInput(container, "#authPassword").value).toBe("");
  });

  it("removes both the saved session and browser-only account metadata", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    await renderAuthView();

    const remove = container.querySelector<HTMLButtonElement>(
      '[aria-label="a@example.com 계정을 이 기기에서 삭제"]',
    )!;
    remove.focus();
    await click(remove);

    const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]')!;
    const cancel = buttonWithExactText(dialog, "취소");
    const confirm = buttonWithExactText(dialog, "이 기기에서 삭제");
    expect(document.activeElement).toBe(cancel);
    await pressKey("Tab", true);
    expect(document.activeElement).toBe(confirm);
    await click(confirm);

    expect(
      JSON.parse(
        window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) ?? "[]",
      ),
    ).toEqual([]);
    expect(
      JSON.parse(
        window.sessionStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY) ?? "[]",
      ),
    ).toEqual([]);
    expect(requiredInput(container, "#authEmail").value).toBe("");
    expect(mocks.request).not.toHaveBeenCalled();
  });

  it("does not persist or activate a signup response before normal login", async () => {
    const session = authSession("new-user", "new@example.com", "새 계정");
    mocks.requestPublic.mockResolvedValue(session);
    await renderAuthView();
    await click(buttonWithExactText(container, "회원가입"));
    await changeInput(requiredInput(container, "#authNickname"), "새 계정");
    await changeInput(requiredInput(container, "#authEmail"), "new@example.com");
    await changeInput(requiredInput(container, "#authPassword"), "signup-password");

    await submit(container.querySelector("form")!);

    expect(mocks.requestPublic).toHaveBeenCalledWith("/auth/signup", {
      method: "POST",
      body: {
        email: "new@example.com",
        password: "signup-password",
        nickname: "새 계정",
      },
    });
    expect(mocks.store.signIn).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY)).toBeNull();
  });

  it("blocks duplicate account switches while candidate validation is pending", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    const pending = deferred<PublicUser>();
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    mocks.requestWithToken.mockReturnValue(pending.promise);
    await renderAuthView();
    const accountButton = container.querySelector<HTMLButtonElement>(
      '[aria-label="계정 A, a@example.com 계정 선택"]',
    )!;

    await act(async () => {
      accountButton.click();
      accountButton.click();
      await flush();
    });

    expect(mocks.requestWithToken).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("전환 중");

    await act(async () => {
      pending.resolve(session.user);
      await pending.promise;
      await flush();
    });
  });

  it("does not switch accounts when the chooser closes during session validation", async () => {
    const session = authSession("user-a", "a@example.com", "계정 A");
    const pending = deferred<PublicUser>();
    seedRememberedAccounts([
      rememberedAccount("user-a", "a@example.com", "계정 A"),
    ]);
    seedLoggedInAccount(session);
    mocks.requestWithToken.mockReturnValue(pending.promise);
    await renderAuthView();

    await act(async () => {
      container
        .querySelector<HTMLButtonElement>(
          '[aria-label="계정 A, a@example.com 계정 선택"]',
        )!
        .click();
      await flush();
      root.render(null);
    });

    await act(async () => {
      pending.resolve(session.user);
      await pending.promise;
      await flush();
    });

    expect(mocks.store.signIn).not.toHaveBeenCalled();
    expect(mocks.store.reportError).not.toHaveBeenCalled();
  });

  it("does not activate a password login that finishes after the chooser closes", async () => {
    const session = authSession("user-b", "b@example.com", "계정 B");
    const pending = deferred<AuthSession>();
    mocks.requestPublic.mockReturnValue(pending.promise);
    await renderAuthView();
    await changeInput(requiredInput(container, "#authEmail"), "b@example.com");
    await changeInput(requiredInput(container, "#authPassword"), "password-b");

    await act(async () => {
      container
        .querySelector<HTMLFormElement>("form")!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await flush();
      root.render(null);
    });

    await act(async () => {
      pending.resolve(session);
      await pending.promise;
      await flush();
    });

    expect(mocks.store.signIn).not.toHaveBeenCalled();
    expect(mocks.store.reportError).not.toHaveBeenCalled();
  });
});
