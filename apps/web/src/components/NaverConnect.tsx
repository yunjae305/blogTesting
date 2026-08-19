import { useEffect, useRef, useState, type FormEvent } from "react";

import { request } from "../api/client";
import { useStore } from "../store";
import { LiveSessionsPanel } from "./LiveSessionsPanel";

const SAVED_PASSWORD_MASK = "••••••••••";

export interface NaverStatus {
  configured: boolean;
  blogId: string | null;
  saved: boolean;
  savedUsername: string | null;
  /** 저장된 네이버 세션이 있으면 발행 때 로그인창이 뜨지 않는다. */
  hasSession?: boolean;
  /** 그 세션이 **어느 계정 것인지**. 세션만으로는 누구인지 알 수 없다. */
  sessionAccount?: string | null;
}

/**
 * 현재 Blog-it 로그인 사용자의 네이버 자동 발행 정보 저장.
 *
 * The password never goes to the database — that is a shared Atlas cluster, and a 네이버
 * password sitting in it is a 네이버 account belonging to anyone who can read the
 * collection. It is remembered on this machine, encrypted to this Windows account, so
 * that reconnecting is a button rather than typing it again. Nothing reads it back out:
 * there is no route that returns a password.
 */
export function NaverConnect() {
  const { showToast, reportError } = useStore();

  const [status, setStatus] = useState<NaverStatus | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [editingPassword, setEditingPassword] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const loaded = await request<NaverStatus>("/naver/status");
        setStatus(loaded);
        if (loaded.savedUsername) setUsername(loaded.savedUsername);
      } catch {
        // Not being able to read the status is not worth a toast on a settings page.
      }
    })();
  }, []);

  /** 로그인이 끝났는지 되풀이해 보는 타이머. 화면을 떠나면 반드시 끈다. */
  const watcher = useRef<number | null>(null);
  /** 세션이 이 계정 것으로 바뀐 것을 이미 확인했는가. 늦게 오는 응답을 어떻게 다룰지 정한다. */
  const confirmed = useRef(false);

  const stopWatching = () => {
    if (watcher.current === null) return;
    window.clearInterval(watcher.current);
    watcher.current = null;
  };
  useEffect(() => stopWatching, []);

  /**
   * 로그인이 끝났는지 3초마다 확인한다.
   *
   * `/naver/login`은 **사람이 2단계 인증을 마칠 때까지 최대 7분을 기다리고**, 인증이
   * 끝난 뒤에도 글쓰기 화면을 여는 동안 응답이 돌아오지 않는다. 그동안 화면은
   * '로그인 중'에 멈춰 있어서, 휴대폰으로 인증까지 끝낸 사용자에게는 아무 일도 일어나지
   * 않는 것으로 보였다(2026-08-06 사용자 신고).
   *
   * 서버는 로그인에 성공하는 **그 순간** 어느 계정으로 들어갔는지를 프로필에 적는다
   * (`session_account`). 그래서 응답을 기다리지 않고 그 기록이 바뀌는 것으로 성공을 안다.
   *
   * **이미 그 계정 세션이 있는 상태에서는 감시하지 않는다.** 그때는 기록이 바뀌지 않아
   * 성공과 구별할 수 없다 — 눌러 놓고 실패해도 '로그인됐다'고 말하게 된다.
   */
  const watchLogin = (account: string) => {
    stopWatching();
    watcher.current = window.setInterval(() => {
      void (async () => {
        try {
          const next = await request<NaverStatus>("/naver/status");
          setStatus(next);
          const current = (next.sessionAccount || "").trim().toLowerCase();
          if (!next.hasSession || !current || current !== account) return;
          confirmed.current = true;
          stopWatching();
          setBusy(false);
          setPassword("");
          setEditingPassword(false);
          showToast("Naver에 로그인했습니다. 이제 발행할 수 있습니다.");
        } catch {
          // 한 번 못 읽는 것은 문제가 아니다 — 다음 주기에 다시 본다.
        }
      })();
    }, 3000);
  };

  const remembered = status?.savedUsername ?? null;
  // The saved password stands in for a typed one, but only for the account it belongs to.
  const canUseSaved = Boolean(remembered) && username.trim() === remembered;

  async function saveNaver(event: FormEvent) {
    event.preventDefault();
    if (!username.trim() || (!password && !canUseSaved)) {
      showToast("Naver 아이디와 비밀번호를 입력해 주세요.", true);
      return;
    }

    const account = username.trim().toLowerCase();
    // 지금 살아 있는 세션이 이미 이 계정 것인가. 그렇다면 서버는 다시 로그인하지 않고
    // 그 세션을 그대로 쓰므로 응답이 곧 온다 — 감시할 것이 없다.
    const sameAccountSession =
      Boolean(status?.hasSession) &&
      (status?.sessionAccount || "").trim().toLowerCase() === account;

    setBusy(true);
    confirmed.current = false;
    if (!sameAccountSession) watchLogin(account);
    try {
      // 저장만 하지 않고 **그 자리에서 로그인까지** 한다(/naver/login이 둘을 함께 한다).
      // 계정을 바꾸는 이 순간이 2단계 인증을 끝내기 가장 좋은 때다 — 발행 도중에 만나면
      // 기다릴 수 있는 시간이 짧아 그 발행이 통째로 실패한다.
      const result = await request<NaverStatus>("/naver/login", {
        method: "POST",
        body: password ? { username: username.trim(), password } : { username: username.trim() },
      });
      setStatus({ ...result, configured: true });
      setPassword("");
      setEditingPassword(false);
      // 감시가 먼저 성공을 알렸으면 같은 말을 두 번 하지 않는다.
      if (!confirmed.current) showToast("Naver에 로그인했습니다. 이제 발행할 수 있습니다.");
    } catch (error) {
      // 세션이 이미 이 계정 것으로 바뀐 것을 봤다면 늦게 온 실패는 알리지 않는다.
      // 로그인은 됐고, 그 뒤(글쓰기 화면을 여는 단계)에서 난 일이다.
      if (!confirmed.current) reportError(error);
    } finally {
      stopWatching();
      setBusy(false);
    }
  }

  const saved = Boolean(status?.saved);
  const hasSession = Boolean(status?.hasSession);
  // 어느 계정으로 들어가 있는지까지 보여준다. '로그인됨'만으로는 아이디를 바꿔 다시
  // 누른 뒤에도 바뀐 게 없어 보여, 방금 한 로그인이 먹혔는지 알 수가 없다.
  const loggedInAs = (status?.sessionAccount || "").trim();

  return (
    <section className="settings-card naver-connect" aria-labelledby="naver-connect-title">
      <header className="settings-card__header">
        <span className="settings-card__index" aria-hidden="true">
          03
        </span>
        <div className="settings-card__heading">
          <h3 id="naver-connect-title">Naver 계정</h3>
          <p>자동 발행에 사용할 로그인 정보를 관리합니다.</p>
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
            중계한다(2026-08-18 사용자 요청, Threads 카드와 같은 규칙). */}
        {busy && (
          <>
            <p className="hint naver-connect-waiting" role="status">
              아래 화면에서 로그인과 2단계 인증을 끝내 주세요. 서버에서 열린 Chrome이
              그대로 보이고, 클릭·입력이 전달됩니다. 인증이 끝나면 이 화면이 저절로
              완료됩니다(최대 7분까지 기다립니다). 입력한 계정은 이미 저장되었습니다.
            </p>
            <LiveSessionsPanel channels={["naver"]} kinds={["login"]} withTyping />
          </>
        )}
        {!busy && (
        <div className="settings-rows naver-connect-rows">
          <div className="settings-row">
            <div className="settings-row-copy">
              <label htmlFor="naverId">Naver 아이디</label>
              <span className="settings-row-description">
                blog.naver.com/아이디에 사용되는 계정
              </span>
            </div>
            <div className="settings-row-control">
              <input
                id="naverId"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </div>
          </div>

          <div className="settings-row">
            <div className="settings-row-copy">
              <label htmlFor="naverPassword">비밀번호</label>
              <span className="settings-row-description">
                DB로 전송하지 않고 이 PC에만 보관합니다.
              </span>
            </div>
            <div className="settings-row-control settings-row-control-stacked">
              <input
                id="naverPassword"
                type="password"
                autoComplete="current-password"
                aria-describedby={canUseSaved ? "naverPasswordSaved" : undefined}
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
                <p className="hint" id="naverPasswordSaved">
                  비밀번호가 이 사용자 전용 로컬 영역에 암호화되어 저장되어 있습니다.
                </p>
              )}
            </div>
          </div>
        </div>
        )}

        {/* '연결 관리'라는 별도 행을 두지 않는다 — 저장 버튼과 보안 안내만 있으면 되는데
            섹션 하나를 더 만들어 카드가 길어지기만 했다. 세션 재사용 설명은 아래 한 줄에 합쳤다. */}
        <div className="naver-connect-actions">
          <button
            className="button naver-save-button"
            type="button"
            id="saveNaverCredentials"
            onClick={saveNaver}
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

        {/* 무엇을 기다리는 중인지 적는다. 이 버튼은 열린 Chrome에서 사람이 인증을 끝낼
            때까지 몇 분이고 기다리는데, 그 사이 화면에 아무 말도 없으면 멈춘 것으로
            보인다(2026-08-06 사용자 신고). 아이디·비밀번호는 이미 저장돼 있다는 것도
            함께 알린다 — 창을 닫아도 다시 입력할 필요는 없다. */}
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
