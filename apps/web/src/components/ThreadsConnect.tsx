import { useEffect, useState, type FormEvent } from "react";

import { request } from "../api/client";
import { useStore } from "../store";
import { LiveBrowserView } from "./LiveBrowserView";
import type { PendingVerification } from "./write/VerificationCodeModal";

// 로그인이 도는 동안 인증코드 요청을 살피는 주기(발행 화면과 같은 값).
const VERIFICATION_POLL_MS = 2000;

const SAVED_PASSWORD_MASK = "••••••••••";

export interface ThreadsStatus {
  saved: boolean;
  savedUsername: string | null;
  /** 세션이 있으면 발행 때 로그인창 없이 바로 게시된다. */
  hasSession?: boolean;
  /** 그 세션이 **어느 계정 것인지**. 세션만으로는 누구인지 알 수 없다. */
  sessionAccount?: string | null;
}

/**
 * 현재 Blog-it 로그인 사용자의 스레드 발행 정보 저장.
 *
 * 네이버와 같은 원칙이다: 비밀번호는 DB로 가지 않고 이 PC의 사용자 전용 영역에
 * 암호화되어 보관되며, 되돌려주는 라우트가 없다. 발행 때 로그인 폼에 자동 입력되고,
 * Meta가 추가 확인(2단계 인증 등)을 요구하면 열린 창에서 사람이 처리한다 — 한 번
 * 로그인하면 세션이 유지되어 다음부터는 로그인 없이 게시된다.
 */
export function ThreadsConnect() {
  const { showToast, reportError } = useStore();

  const [status, setStatus] = useState<ThreadsStatus | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [editingPassword, setEditingPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [verification, setVerification] = useState<PendingVerification | null>(null);

  // 로그인이 도는 동안 2단계 인증 코드 요청을 살핀다. 자동화가 인증 화면을 만나면
  // 여기로 코드 입력창이 뜬다 — 중계 화면을 직접 클릭하는 것과 두 겹의 길이다.
  useEffect(() => {
    if (!busy) {
      setVerification(null);
      return;
    }
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const answer = await request<{ pending: PendingVerification | null }>(
            "/posting/verification",
          );
          const pending = answer?.pending;
          setVerification(pending && pending.channel === "threads" ? pending : null);
        } catch {
          // 한 번 못 읽는 것은 문제가 아니다 — 다음 주기에 다시 본다.
        }
      })();
    }, VERIFICATION_POLL_MS);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    void (async () => {
      try {
        const loaded = await request<ThreadsStatus>("/threads/status");
        setStatus(loaded);
        if (loaded.savedUsername) setUsername(loaded.savedUsername);
      } catch {
        // 설정 화면에서 상태를 못 읽은 것은 토스트를 띄울 일이 아니다.
      }
    })();
  }, []);

  const remembered = status?.savedUsername ?? null;
  const canUseSaved = Boolean(remembered) && username.trim() === remembered;

  /** 인증 코드(또는 __RESEND__/__BACKUP__ 지시)를 기다리는 자동화에 넘긴다. */
  async function submitVerificationCode(code: string) {
    await request("/posting/verification", { method: "POST", body: { code } });
  }

  async function saveThreads(event: FormEvent) {
    event.preventDefault();
    if (!username.trim() || (!password && !canUseSaved)) {
      showToast("Threads 아이디와 비밀번호를 입력해 주세요.", true);
      return;
    }

    setBusy(true);
    try {
      // 네이버와 같다 — 저장만 하지 않고 그 자리에서 로그인까지 한다. Meta의 추가 확인은
      // 사람이 화면 앞에 있는 지금 끝내는 편이 낫다.
      const result = await request<ThreadsStatus>("/threads/login", {
        method: "POST",
        body: password ? { username: username.trim(), password } : { username: username.trim() },
      });
      setStatus(result);
      setPassword("");
      setEditingPassword(false);
      showToast("Threads에 로그인했습니다. 이제 발행할 수 있습니다.");
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  const saved = Boolean(status?.saved);
  const hasSession = Boolean(status?.hasSession);
  // 어느 계정으로 들어가 있는지까지 보여준다. '로그인됨'만으로는 아이디를 바꿔 다시
  // 누른 뒤에도 바뀐 게 없어 보여, 방금 한 로그인이 먹혔는지 알 수가 없다.
  const loggedInAs = (status?.sessionAccount || "").trim();

  return (
    <section className="settings-card naver-connect" aria-labelledby="threads-connect-title">
      <header className="settings-card__header">
        <span className="settings-card__index" aria-hidden="true">
          04
        </span>
        <div className="settings-card__heading">
          <h3 id="threads-connect-title">Threads 계정</h3>
          <p>Threads 발행에 사용할 로그인 정보를 관리합니다.</p>
        </div>
        <span
          className={`badge naver-connect-badge ${hasSession ? "ok" : saved ? "ok" : ""}`}
          aria-live="polite"
        >
          {loggedInAs ? `${loggedInAs} 로그인됨` : hasSession ? "로그인됨" : saved ? "저장됨" : "저장 안 됨"}
        </span>
      </header>

      <div className="settings-card__body">
        {/* 로그인이 도는 동안에는 계정 입력칸을 감추고 **그 자리에** 서버 크롬 화면을
            중계한다(2026-08-18 사용자 요청). 아이디·비밀번호는 이미 서버로 넘어갔고,
            남은 일은 화면 속 로그인·2단계 인증뿐이다. */}
        {busy && (
          <>
            {/* 2단계 인증이 뜨면 안내가 그 상황(오답·재전송 결과)으로 바뀐다. */}
            <p className="hint" role="status">
              {verification
                ? verification.prompt
                : "아래 화면에서 로그인과 2단계 인증을 끝내 주세요. 서버에서 열린 " +
                  "Chrome이 그대로 보이고, 클릭·입력이 전달됩니다."}
            </p>
            {/* 인증 코드 입력은 별도 카드가 아니라 **중계 화면의 입력줄 하나**다
                (2026-08-18 사용자 요청). 코드가 창구(/posting/verification)로 가면
                자동화가 사람 속도로 대신 입력·제출하고, 옆의 두 버튼은 크롬 화면의
                '코드 재전송'·'백업 코드 사용'을 대신 눌러 준다. */}
            <LiveBrowserView
              channel="threads"
              label="스레드 로그인"
              withTyping
              typingPlaceholder={
                verification
                  ? "받은 인증 코드를 입력하고 Enter (백업 코드는 8자리 그대로)"
                  : undefined
              }
              onSendText={verification ? submitVerificationCode : undefined}
              actions={
                verification ? (
                  <>
                    <button
                      className="button small"
                      type="button"
                      title="인증번호 입력 화면으로 돌아가기 — 크롬 화면의 뒤로(←)를 대신 누릅니다"
                      onClick={() => void submitVerificationCode("__BACK__").catch(reportError)}
                    >
                      ←
                    </button>
                    <button
                      className="button small"
                      type="button"
                      onClick={() => void submitVerificationCode("__RESEND__").catch(reportError)}
                    >
                      코드 재전송
                    </button>
                    <button
                      className="button small"
                      type="button"
                      onClick={() => void submitVerificationCode("__BACKUP__").catch(reportError)}
                    >
                      백업 코드 사용
                    </button>
                  </>
                ) : undefined
              }
            />
          </>
        )}
        {!busy && (
        <div className="settings-rows naver-connect-rows">
          <div className="settings-row">
            <div className="settings-row-copy">
              <label htmlFor="threadsId">Threads 아이디</label>
              <span className="settings-row-description">
                사용자 이름·전화번호·이메일 중 하나 (Threads 로그인과 동일)
              </span>
            </div>
            <div className="settings-row-control">
              <input
                id="threadsId"
                autoComplete="username"
                placeholder="사용자 이름, 전화번호 또는 이메일 주소"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </div>
          </div>

          <div className="settings-row">
            <div className="settings-row-copy">
              <label htmlFor="threadsPassword">비밀번호</label>
              <span className="settings-row-description">
                DB로 전송하지 않고 이 PC에만 보관합니다.
              </span>
            </div>
            <div className="settings-row-control settings-row-control-stacked">
              <input
                id="threadsPassword"
                type="password"
                autoComplete="current-password"
                aria-describedby={canUseSaved ? "threadsPasswordSaved" : undefined}
                value={
                  password || (canUseSaved && !editingPassword ? SAVED_PASSWORD_MASK : "")
                }
                onFocus={() => {
                  if (canUseSaved && !password) setEditingPassword(true);
                }}
                onBlur={() => {
                  if (!password) setEditingPassword(false);
                }}
                onChange={(event) => setPassword(event.target.value)}
              />
              {canUseSaved && !password && (
                <p className="hint" id="threadsPasswordSaved">
                  비밀번호가 이 사용자 전용 로컬 영역에 암호화되어 저장되어 있습니다.
                </p>
              )}
            </div>
          </div>
        </div>
        )}

        <div className="naver-connect-actions">
          <button
            className="button naver-save-button"
            type="button"
            id="saveThreadsCredentials"
            onClick={saveThreads}
            disabled={busy}
          >
            {busy ? (
              <>
                <span className="spinner" aria-hidden="true" /> 로그인 중
              </>
            ) : (
              "저장하고 로그인"
            )}
          </button>
        </div>

        <p className="naver-privacy-note">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M12 3.5 19 6v5.5c0 4.2-2.9 7.7-7 8.9-4.1-1.2-7-4.7-7-8.9V6Z" />
            <path d="m9 12 2 2 4-4" />
          </svg>
          <span>저장한 로그인 정보는 서비스 DB에 저장되지 않고, 사용자의 PC에 암호화되어 저장됩니다.</span>
        </p>
      </div>
    </section>
  );
}
