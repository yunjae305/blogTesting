import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  findAccountSession,
  getAccountSessions,
  removeAccountSession,
  saveAccountSession,
  type StoredAccountSession,
} from "../accountSessions";
import {
  ApiError,
  requestPublic,
  requestWithToken,
} from "../api/client";
import type { AuthSession, PublicUser } from "../api/types";
import {
  MAX_REMEMBERED_ACCOUNTS,
  getRememberedAccounts,
  removeRememberedAccount,
  sortRememberedAccountsByLastUsed,
  type RememberedAccount,
} from "../rememberedAccounts";
import { useStore } from "../store";
import {
  RememberedAccountAvatar,
  RememberedAccountList,
  RememberedAccountRemovalDialog,
} from "./auth/RememberedAccountList";

type LoginView = "account-selection" | "password-entry" | "manual-login";
type AuthMode = "login" | "signup";

interface AccountSnapshot {
  accounts: RememberedAccount[];
  sessionUserIds: string[];
  sessionEmails: string[];
  rememberedUserIds: string[];
  rememberedEmails: string[];
}

const DEMO_EMAIL = "demo@blog-it.dev";
const DEMO_PASSWORD = "demo1234";

class SavedSessionIdentityError extends Error {
  name = "SavedSessionIdentityError";
}

function accountFromSession(session: StoredAccountSession): RememberedAccount {
  const emailName = session.user.email.split("@")[0] || session.user.email;
  return {
    userId: session.user.userId,
    email: session.user.email,
    displayName: session.user.nickname.trim() || emailName,
    profileImage: null,
    lastUsedAt: session.lastUsedAt,
  };
}

function normalizedEmail(email: string): string {
  return email.trim().toLowerCase();
}

function loadAccountSnapshot(): AccountSnapshot {
  const remembered = getRememberedAccounts();
  const sessionResult = getAccountSessions();
  const sessions = sessionResult.sessions;
  const candidates = sortRememberedAccountsByLastUsed([
    ...remembered,
    ...sessions.map(accountFromSession),
  ]);
  const userIds = new Set<string>();
  const emails = new Set<string>();
  const accounts: RememberedAccount[] = [];

  for (const account of candidates) {
    const emailKey = normalizedEmail(account.email);
    if (userIds.has(account.userId) || emails.has(emailKey)) continue;
    userIds.add(account.userId);
    emails.add(emailKey);
    accounts.push(account);
    if (accounts.length === MAX_REMEMBERED_ACCOUNTS) break;
  }

  return {
    accounts,
    sessionUserIds: sessions.map((session) => session.user.userId),
    sessionEmails: sessions.map((session) => normalizedEmail(session.user.email)),
    rememberedUserIds: remembered.map((account) => account.userId),
    rememberedEmails: remembered.map((account) => normalizedEmail(account.email)),
  };
}

function authSessionFromStored(
  stored: StoredAccountSession,
  verifiedUser: PublicUser,
): AuthSession {
  return {
    user: verifiedUser,
    accessToken: stored.accessToken,
    issuedAt: stored.issuedAt,
    expiresAt: stored.expiresAt,
  };
}

export function AuthView() {
  const {
    session: activeSession,
    closeAccountChooser,
    signIn,
    signOut,
    reportError,
    showToast,
  } = useStore();
  const [initialSnapshot] = useState<AccountSnapshot>(loadAccountSnapshot);
  const [rememberedAccounts, setRememberedAccounts] = useState<RememberedAccount[]>(
    initialSnapshot.accounts,
  );
  const [sessionUserIds, setSessionUserIds] = useState<string[]>(
    initialSnapshot.sessionUserIds,
  );
  const [sessionEmails, setSessionEmails] = useState<string[]>(
    initialSnapshot.sessionEmails,
  );
  const [rememberedUserIds, setRememberedUserIds] = useState<string[]>(
    initialSnapshot.rememberedUserIds,
  );
  const [rememberedEmails, setRememberedEmails] = useState<string[]>(
    initialSnapshot.rememberedEmails,
  );
  const [selectedAccount, setSelectedAccount] = useState<RememberedAccount | null>(null);
  const [accountPendingRemoval, setAccountPendingRemoval] =
    useState<RememberedAccount | null>(null);
  const [loginView, setLoginView] = useState<LoginView>(
    initialSnapshot.accounts.length ? "account-selection" : "manual-login",
  );
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState(
    initialSnapshot.accounts.length ? "" : DEMO_EMAIL,
  );
  const [nickname, setNickname] = useState("");
  const [password, setPassword] = useState(
    initialSnapshot.accounts.length ? "" : DEMO_PASSWORD,
  );
  const [busy, setBusy] = useState(false);
  const [busyAccountId, setBusyAccountId] = useState<string | null>(null);
  const busyRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    // React StrictMode deliberately runs setup → cleanup → setup in development.
    // Re-arm the flag for the real mounted lifetime after that probe.
    mountedRef.current = true;
    return () => {
      // The chooser can be closed while an account probe or password login is
      // still in flight. A late response must never switch accounts after that
      // explicit cancellation.
      mountedRef.current = false;
    };
  }, []);

  const isLogin = mode === "login";
  const choosingAccount = isLogin && loginView === "account-selection";
  const passwordAccount =
    isLogin && loginView === "password-entry" ? selectedAccount : null;

  const refreshAccounts = useCallback(() => {
    const snapshot = loadAccountSnapshot();
    setRememberedAccounts(snapshot.accounts);
    setSessionUserIds(snapshot.sessionUserIds);
    setSessionEmails(snapshot.sessionEmails);
    setRememberedUserIds(snapshot.rememberedUserIds);
    setRememberedEmails(snapshot.rememberedEmails);
    return snapshot;
  }, []);

  const closeRemovalDialog = useCallback(() => {
    setAccountPendingRemoval(null);
  }, []);

  const enterPasswordFor = useCallback((account: RememberedAccount) => {
    setMode("login");
    setSelectedAccount(account);
    setEmail(account.email);
    setPassword("");
    setLoginView("password-entry");
  }, []);

  async function selectAccount(account: RememberedAccount) {
    if (busyRef.current) return;

    if (
      activeSession?.user.userId === account.userId ||
      normalizedEmail(activeSession?.user.email ?? "") ===
        normalizedEmail(account.email)
    ) {
      closeAccountChooser();
      return;
    }

    const lookup = findAccountSession(account.email);
    setSessionUserIds(lookup.sessions.map((session) => session.user.userId));
    setSessionEmails(
      lookup.sessions.map((session) => normalizedEmail(session.user.email)),
    );
    if (!lookup.ok || !lookup.session) {
      enterPasswordFor(account);
      return;
    }

    busyRef.current = true;
    setBusy(true);
    setBusyAccountId(account.userId);
    try {
      const verifiedUser = await requestWithToken<PublicUser>(
        "/auth/me",
        lookup.session.accessToken,
      );
      if (!mountedRef.current) return;

      const sameUserId =
        lookup.session.user.userId === account.userId &&
        verifiedUser.userId === account.userId;
      const sameEmail =
        normalizedEmail(lookup.session.user.email) ===
          normalizedEmail(account.email) &&
        normalizedEmail(verifiedUser.email) === normalizedEmail(account.email);
      if (!sameUserId && !sameEmail) {
        throw new SavedSessionIdentityError();
      }

      const activation = await signIn(
        authSessionFromStored(lookup.session, verifiedUser),
      );
      if (!activation.activated) return;
      showToast(
        activation.persisted
          ? `${verifiedUser.nickname || verifiedUser.email} 계정으로 전환했습니다.`
          : "계정은 전환했지만 이 기기에 로그인 상태를 저장하지 못했습니다.",
        !activation.persisted,
      );
    } catch (error) {
      if (!mountedRef.current) return;

      if (error instanceof ApiError && error.status === 401) {
        removeAccountSession(account.email);
        refreshAccounts();
        enterPasswordFor(account);
        showToast("로그인이 만료되었습니다. 비밀번호를 다시 입력해 주세요.", true);
      } else if (error instanceof SavedSessionIdentityError) {
        removeAccountSession(account.email);
        refreshAccounts();
        enterPasswordFor(account);
        showToast("저장된 로그인 정보를 확인할 수 없어 다시 로그인이 필요합니다.", true);
      } else {
        reportError(error);
      }
    } finally {
      busyRef.current = false;
      if (mountedRef.current) {
        setBusy(false);
        setBusyAccountId(null);
      }
    }
  }

  function useAnotherAccount() {
    if (busy) return;
    setMode("login");
    setSelectedAccount(null);
    setEmail("");
    setPassword("");
    setLoginView("manual-login");
  }

  function changeAccount() {
    if (busy) return;
    setSelectedAccount(null);
    setEmail("");
    setPassword("");
    setLoginView(rememberedAccounts.length ? "account-selection" : "manual-login");
  }

  function changeMode(nextMode: AuthMode) {
    if (busy || nextMode === mode) return;

    const wasUsingRememberedAccount = loginView !== "manual-login";
    setMode(nextMode);
    setSelectedAccount(null);
    setPassword("");
    setLoginView("manual-login");
    if (wasUsingRememberedAccount) setEmail("");
  }

  function confirmAccountRemoval() {
    if (!accountPendingRemoval || busy) return;

    const account = accountPendingRemoval;
    const previousSession = findAccountSession(account.email);
    const sessionRemoval = removeAccountSession(account.email);
    if (!sessionRemoval.ok) {
      setAccountPendingRemoval(null);
      showToast("이 기기에서 저장된 로그인 상태를 삭제하지 못했습니다.", true);
      return;
    }

    const storedMetadata =
      rememberedUserIds.includes(account.userId) ||
      rememberedEmails.includes(normalizedEmail(account.email));
    if (storedMetadata) {
      const metadataRemoval = removeRememberedAccount(account.email);
      if (!metadataRemoval.removed) {
        if (previousSession.ok && previousSession.session) {
          saveAccountSession(
            previousSession.session,
            new Date(previousSession.session.lastUsedAt),
          );
        }
        setAccountPendingRemoval(null);
        refreshAccounts();
        showToast("이 기기의 계정 목록에서 정보를 삭제하지 못했습니다.", true);
        return;
      }
    }

    setAccountPendingRemoval(null);
    if (
      activeSession?.user.userId === account.userId ||
      normalizedEmail(activeSession?.user.email ?? "") ===
        normalizedEmail(account.email)
    ) {
      signOut({ silent: true });
      showToast("현재 계정에서 로그아웃하고 이 기기의 저장 정보를 삭제했습니다.");
      return;
    }

    const remaining = refreshAccounts().accounts;
    if (!remaining.length) {
      setMode("login");
      setSelectedAccount(null);
      setEmail("");
      setPassword("");
      setLoginView("manual-login");
    }
    showToast("이 기기에서 계정 정보와 저장된 로그인 상태를 삭제했습니다.");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busyRef.current) return;

    busyRef.current = true;
    setBusy(true);
    try {
      if (!isLogin) {
        // Existing signup behavior is intentionally unchanged: account creation
        // succeeds first, then the user signs in through the normal login API.
        await requestPublic<AuthSession>("/auth/signup", {
          method: "POST",
          body: { email, password, nickname: nickname.trim() },
        });
        if (!mountedRef.current) return;

        setMode("login");
        setLoginView("manual-login");
        setSelectedAccount(null);
        setPassword("");
        showToast("가입이 완료되었습니다. 로그인해 주세요.");
        return;
      }

      const session = await requestPublic<AuthSession>("/auth/login", {
        method: "POST",
        body: { email, password },
      });
      if (!mountedRef.current) return;

      const activation = await signIn(session);
      if (!activation.activated) return;
      showToast(
        activation.persisted
          ? `${session.user.nickname || session.user.email}님, 반갑습니다.`
          : "로그인했지만 이 기기에 로그인 상태를 저장하지 못했습니다.",
        !activation.persisted,
      );
    } catch (error) {
      if (!mountedRef.current) return;

      if (isLogin) setPassword("");
      reportError(error);
    } finally {
      busyRef.current = false;
      if (mountedRef.current) setBusy(false);
    }
  }

  const heading = choosingAccount
    ? {
        title: "계정을 선택하세요",
        description: "Blog-it에서 계속하려면 계정을 선택해 주세요.",
      }
    : passwordAccount
      ? {
          title: "다시 오신 걸 환영해요",
          description: "이 계정은 다시 인증해야 합니다. 비밀번호를 입력해 주세요.",
        }
      : {
          title: isLogin ? "로그인" : "회원가입",
          description: isLogin
            ? "작성 중인 글을 이어서 완성해 보세요."
            : "계정을 만들고 첫 글을 시작해 보세요.",
        };

  const alternateModeAction = (
    <div className="tabs auth-mode-switch">
      <span>{isLogin ? "계정이 없으신가요?" : "이미 계정이 있으신가요?"}</span>
      <button
        type="button"
        disabled={busy}
        onClick={() => changeMode(isLogin ? "signup" : "login")}
      >
        {isLogin ? "회원가입" : "로그인"}
      </button>
    </div>
  );

  return (
    <>
      <section className="auth-shell auth-stage" aria-labelledby="authTitle">
        <div className="auth-intro auth-brand-scene" aria-hidden="true">
          <div className="auth-brand-lockup">
            <span className="brand-logo sticky-logo auth-sticky-logo">
              <span className="sticky-tape" />
              <span className="note front">
                <span className="logo-text">Blog-it</span>
              </span>
            </span>
            <h1>
              아이디어를 메모하고,
              <br />
              멋진 블로그로 완성하세요.
            </h1>
          </div>

          <div className="idea-collage auth-illustration">
            <div className="back-paper" />
            <div className="idea-paper">
              <span className="collage-tape" />
              <svg viewBox="0 0 120 120" focusable="false">
                <path d="M44 73h32M48 84h24M60 19v-9M25 33l-7-7M95 33l7-7M34 61a26 26 0 1 1 52 0c0 13-10 15-12 26H46C44 76 34 74 34 61Z" />
                <path d="m52 58 7 8 12-18" />
              </svg>
            </div>
            <div className="blue-paper">
              <i />
              <i />
              <i />
            </div>
            <span className="idea-ray ray-one" />
            <span className="idea-ray ray-two" />
            <span className="idea-ray ray-three" />
          </div>
        </div>

        <div className="panel auth-card auth-form-card">
          <div className="auth-card-heading">
            <span>WELCOME TO BLOG-IT</span>
            <h2 id="authTitle">{heading.title}</h2>
            <p className="subtle">{heading.description}</p>
          </div>

          <div className="panel-body">
            {choosingAccount ? (
              <>
                <RememberedAccountList
                  accounts={rememberedAccounts}
                  hasSession={(account) =>
                    sessionUserIds.includes(account.userId) ||
                    sessionEmails.includes(normalizedEmail(account.email))
                  }
                  isActive={(account) =>
                    activeSession?.user.userId === account.userId ||
                    normalizedEmail(activeSession?.user.email ?? "") ===
                      normalizedEmail(account.email)
                  }
                  busyAccountId={busyAccountId}
                  disabled={busy}
                  onSelect={selectAccount}
                  onUseAnother={useAnotherAccount}
                  onRequestRemove={setAccountPendingRemoval}
                />
                {alternateModeAction}
              </>
            ) : passwordAccount ? (
              <>
                <div
                  className="auth-selected-account"
                  id="selectedAccountIdentity"
                >
                  <RememberedAccountAvatar account={passwordAccount} size="large" />
                  <span className="auth-account-copy">
                    <strong>{passwordAccount.displayName}</strong>
                    <span>{passwordAccount.email}</span>
                  </span>
                  <button
                    className="auth-account-change"
                    type="button"
                    disabled={busy}
                    onClick={changeAccount}
                  >
                    계정 변경
                  </button>
                </div>

                <form onSubmit={submit}>
                  <input type="hidden" name="email" value={passwordAccount.email} />
                  <div className="form-grid">
                    <div className="field full">
                      <label htmlFor="authPassword">비밀번호</label>
                      <input
                        id="authPassword"
                        type="password"
                        autoComplete="current-password"
                        placeholder="비밀번호를 입력하세요"
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        aria-describedby="selectedAccountIdentity authPasswordHint"
                        autoFocus
                        required
                      />
                      <p className="hint" id="authPasswordHint">
                        비밀번호는 이 기기에 저장되지 않습니다.
                      </p>
                    </div>
                  </div>
                  <div className="actions auth-actions">
                    <button
                      className="button primary"
                      type="submit"
                      id="authSubmit"
                      disabled={busy}
                    >
                      {busy ? (
                        <>
                          <span className="spinner" aria-hidden="true" /> 처리 중
                        </>
                      ) : (
                        "로그인"
                      )}
                    </button>
                  </div>
                </form>

                <div className="auth-divider" aria-hidden="true">
                  <span>또는</span>
                </div>
                {alternateModeAction}
              </>
            ) : (
              <>
                <form onSubmit={submit}>
                  <div className="form-grid">
                    {!isLogin && (
                      <div className="field full">
                        <label htmlFor="authNickname">닉네임</label>
                        <input
                          id="authNickname"
                          type="text"
                          autoComplete="nickname"
                          maxLength={30}
                          placeholder="표시할 이름"
                          value={nickname}
                          onChange={(event) => setNickname(event.target.value)}
                          autoFocus
                          required
                        />
                        <p className="hint">화면에 표시할 이름이에요. 최대 30자.</p>
                      </div>
                    )}
                    <div className="field full">
                      <div className="auth-field-heading">
                        <label htmlFor="authEmail">이메일</label>
                        {isLogin && rememberedAccounts.length > 0 && (
                          <button type="button" disabled={busy} onClick={changeAccount}>
                            저장된 계정 보기
                          </button>
                        )}
                      </div>
                      <input
                        id="authEmail"
                        type="email"
                        autoComplete="email"
                        placeholder="이메일을 입력하세요"
                        value={email}
                        onChange={(event) => setEmail(event.target.value)}
                        autoFocus={isLogin}
                        required
                      />
                    </div>
                    <div className="field full">
                      <label htmlFor="authPassword">비밀번호</label>
                      <input
                        id="authPassword"
                        type="password"
                        autoComplete={isLogin ? "current-password" : "new-password"}
                        placeholder="비밀번호를 입력하세요"
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        required
                      />
                      <p className="hint">
                        {isLogin
                          ? "가입할 때 정한 비밀번호를 입력하세요."
                          : "8자 이상으로 정해 주세요."}
                      </p>
                    </div>
                  </div>
                  <div className="actions auth-actions">
                    <button
                      className="button primary"
                      type="submit"
                      id="authSubmit"
                      disabled={busy}
                    >
                      {busy ? (
                        <>
                          <span className="spinner" aria-hidden="true" /> 처리 중
                        </>
                      ) : isLogin ? (
                        "로그인"
                      ) : (
                        "가입하기"
                      )}
                    </button>
                  </div>
                </form>

                <div className="auth-divider" aria-hidden="true">
                  <span>또는</span>
                </div>
                {alternateModeAction}
              </>
            )}
          </div>
        </div>
      </section>

      {accountPendingRemoval && (
        <RememberedAccountRemovalDialog
          account={accountPendingRemoval}
          onCancel={closeRemovalDialog}
          onConfirm={confirmAccountRemoval}
        />
      )}
    </>
  );
}
