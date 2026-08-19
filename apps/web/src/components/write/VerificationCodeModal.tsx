import { useEffect, useRef, useState } from "react";

import { request } from "../../api/client";

/**
 * 발행 자동화가 기다리고 있는 인증 요청. 서버의 posting/verification.py가 만드는 값이고,
 * 코드 자체는 절대 내려오지 않는다(넣기만 한다).
 */
export type PendingVerification = {
  postId: string;
  channel: string;
  prompt: string;
  attempt: number;
  maxAttempts: number;
  waitingSeconds: number;
};

const CHANNEL_LABELS: Record<string, string> = {
  threads: "Threads",
  naver: "Naver",
};

/**
 * 2단계 인증 코드를 받아 발행 자동화에 넘기는 창.
 *
 * 예전에는 자동화가 띄운 Chrome 창을 사용자가 직접 만져야 했다. 이제 그 창은 그대로 두고,
 * 코드만 여기서 받아 서버가 대신 입력한다 — 사람이 브라우저를 조작할 일이 없다.
 *
 * 코드는 이 창과 서버 메모리에만 잠깐 머물고 어디에도 저장되지 않는다.
 */
export function VerificationCodeModal({
  pending,
  onDone,
}: {
  pending: PendingVerification;
  onDone: () => void;
}) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resent, setResent] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 요청이 바뀌면(코드가 틀려 다시 물어보는 경우) 입력칸을 비우고 다시 포커스한다.
  useEffect(() => {
    setCode("");
    setError(null);
    inputRef.current?.focus();
  }, [pending.attempt, pending.postId]);

  const channel = CHANNEL_LABELS[pending.channel] ?? pending.channel;

  async function submit() {
    const trimmed = code.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      await request("/posting/verification", { method: "POST", body: { code: trimmed } });
      onDone();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "코드를 넘기지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  /**
   * 문자가 안 올 때 — 자동화가 대신 스레드 화면의 '코드 재전송' 버튼을 누른다.
   * 코드가 아니라 지시이므로 시도 횟수를 소모하지 않고, 창은 그대로 열려 있는다.
   */
  async function resend() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await request("/posting/verification", { method: "POST", body: { code: "__RESEND__" } });
      setResent(true);
    } catch (resendError) {
      setError(
        resendError instanceof Error ? resendError.message : "재전송을 요청하지 못했습니다.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    try {
      await request("/posting/verification", { method: "DELETE" });
    } catch {
      // 취소가 실패해도 창은 닫는다 — 서버 쪽 대기는 시간이 지나면 스스로 끝난다.
    } finally {
      setBusy(false);
      onDone();
    }
  }

  return (
    <div
      className="verify-overlay write-verify-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="verificationCodeTitle"
    >
      <div className="verify-dialog write-verify-dialog verification-code-dialog">
        <div className="verify-dialog-header">
          <div className="panel-heading-copy">
            <h3 id="verificationCodeTitle">{channel} 2단계 인증</h3>
            <p>{pending.prompt}</p>
          </div>
        </div>

        <div className="verification-code-body">
          <label htmlFor="verificationCode">
            인증코드
          </label>
          <input
            id="verificationCode"
            ref={inputRef}
            className="verification-code-input"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submit();
            }}
            // 문자로 온 코드를 브라우저가 채워 줄 수 있게 한다.
            autoComplete="one-time-code"
            inputMode="numeric"
            placeholder="문자로 받은 코드"
            disabled={busy}
            autoFocus
          />
          <p className="verification-code-hint">
            {channel} 계정에 등록된 방법(문자·인증 앱)으로 받은 코드를 넣어 주세요. 열린 브라우저
            창은 그대로 두시면 됩니다 — 코드는 서버가 대신 입력합니다.
            {pending.channel === "threads" &&
              " 문자가 안 오면 '코드 재전송'을 누르거나, 백업 코드(8자리)를 넣으면 자동으로 백업 코드 화면에서 제출합니다."}
            {pending.attempt > 1 && ` (${pending.attempt}/${pending.maxAttempts}번째 시도)`}
          </p>
          {resent && (
            <p className="verification-code-hint" role="status">
              재전송을 요청했습니다. 새로 받은 코드를 입력해 주세요.
            </p>
          )}
          {error && <p className="verification-code-error">{error}</p>}
        </div>

        <div className="verify-dialog-actions">
          <button type="button" className="button" onClick={cancel} disabled={busy}>
            취소
          </button>
          {pending.channel === "threads" && (
            <button type="button" className="button" onClick={resend} disabled={busy}>
              코드 재전송
            </button>
          )}
          <button
            type="button"
            className="button primary"
            onClick={submit}
            disabled={busy || !code.trim()}
          >
            {busy ? "확인 중…" : "완료"}
          </button>
        </div>
      </div>
    </div>
  );
}
