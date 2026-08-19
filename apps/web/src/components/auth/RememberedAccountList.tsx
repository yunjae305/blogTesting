import { useEffect, useId, useRef, useState } from "react";

import type { RememberedAccount } from "../../rememberedAccounts";

function ChevronIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="m9 5 7 7-7 7" />
    </svg>
  );
}

function RemoveIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4.5 7h15M9 7V4.5h6V7M7 7l.8 12.5h8.4L17 7M10 10.5v5.5M14 10.5v5.5" />
    </svg>
  );
}

function OtherAccountIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="9" cy="8" r="3.5" />
      <path d="M3.5 19c.5-3.5 2.3-5.5 5.5-5.5 1.7 0 3 .5 3.9 1.4M17 11v7M13.5 14.5h7" />
    </svg>
  );
}

export function RememberedAccountAvatar({
  account,
  size = "regular",
}: {
  account: RememberedAccount;
  size?: "regular" | "large";
}) {
  const [imageFailed, setImageFailed] = useState(false);
  const initial = (
    account.email.trim().charAt(0) ||
    account.displayName.charAt(0) ||
    "?"
  ).toUpperCase();
  const showImage = Boolean(account.profileImage) && !imageFailed;

  useEffect(() => {
    setImageFailed(false);
  }, [account.profileImage]);

  return (
    <span
      className={`auth-account-avatar${size === "large" ? " is-large" : ""}`}
      aria-hidden="true"
    >
      {showImage ? (
        <img
          src={account.profileImage!}
          alt=""
          referrerPolicy="no-referrer"
          onError={() => setImageFailed(true)}
        />
      ) : (
        initial
      )}
    </span>
  );
}

interface RememberedAccountListProps {
  accounts: RememberedAccount[];
  onSelect: (account: RememberedAccount) => void;
  onUseAnother: () => void;
  onRequestRemove: (account: RememberedAccount) => void;
  hasSession?: (account: RememberedAccount) => boolean;
  isActive?: (account: RememberedAccount) => boolean;
  busyAccountId?: string | null;
  disabled?: boolean;
}

const hasNoActiveSession = () => false;
const isNotActive = () => false;

export function RememberedAccountList({
  accounts,
  onSelect,
  onUseAnother,
  onRequestRemove,
  hasSession = hasNoActiveSession,
  isActive = isNotActive,
  busyAccountId = null,
  disabled = false,
}: RememberedAccountListProps) {
  const firstAccountRef = useRef<HTMLButtonElement>(null);
  const accountButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const previousBusyAccountId = useRef<string | null>(null);
  const accountListId = useId();
  const isSwitchingAccount = busyAccountId !== null;
  const controlsDisabled = disabled || isSwitchingAccount;
  const busyAccount = accounts.find((account) => account.userId === busyAccountId);

  useEffect(() => {
    firstAccountRef.current?.focus();
  }, [accounts]);

  useEffect(() => {
    const previous = previousBusyAccountId.current;
    previousBusyAccountId.current = busyAccountId;
    if (previous && busyAccountId === null) {
      accountButtonRefs.current.get(previous)?.focus();
    }
  }, [busyAccountId]);

  return (
    <>
      <ul
        className={`auth-account-list${controlsDisabled ? " is-disabled" : ""}`}
        aria-label="이 브라우저에서 사용한 계정"
        aria-busy={isSwitchingAccount || undefined}
      >
        {accounts.map((account, index) => {
          const accountHasSession = hasSession(account);
          const accountIsActive = isActive(account);
          const accountIsBusy = busyAccountId === account.userId;
          const sessionStatusId = `${accountListId}-session-${index}`;
          const transitionStatusId = `${accountListId}-transition-${index}`;
          const describedBy = accountIsBusy
            ? `${sessionStatusId} ${transitionStatusId}`
            : sessionStatusId;

          return (
            <li
              className={`auth-account-entry${accountIsBusy ? " is-busy" : ""}${
                controlsDisabled && !accountIsBusy ? " is-disabled" : ""
              }`}
              key={account.userId}
              aria-busy={accountIsBusy || undefined}
            >
              <button
                className="auth-account-select"
                type="button"
                aria-label={`${account.displayName}, ${account.email} 계정 선택`}
                aria-describedby={describedBy}
                ref={(node) => {
                  if (index === 0) firstAccountRef.current = node;
                  if (node) accountButtonRefs.current.set(account.userId, node);
                  else accountButtonRefs.current.delete(account.userId);
                }}
                disabled={controlsDisabled}
                onClick={() => onSelect(account)}
              >
                <RememberedAccountAvatar account={account} />
                <span className="auth-account-copy">
                  <span className="auth-account-name-row">
                    <strong>{account.displayName}</strong>
                    <span
                      className={`auth-account-session-badge ${
                        accountIsActive
                          ? "is-active"
                          : accountHasSession
                            ? "has-session"
                            : "needs-password"
                      }`}
                      id={sessionStatusId}
                    >
                      {accountIsActive
                        ? "현재 계정"
                        : accountHasSession
                          ? "로그인됨"
                          : "비밀번호 필요"}
                    </span>
                  </span>
                  <span className="auth-account-email">{account.email}</span>
                </span>
                <span
                  className={`auth-account-chevron${accountIsBusy ? " is-busy" : ""}`}
                  id={accountIsBusy ? transitionStatusId : undefined}
                >
                  {accountIsBusy ? (
                    <>
                      <span className="spinner auth-account-switch-spinner" aria-hidden="true" />
                      <span>전환 중</span>
                    </>
                  ) : (
                    <ChevronIcon />
                  )}
                </span>
              </button>
              <button
                className="auth-account-remove"
                type="button"
                aria-label={`${account.email} 계정을 이 기기에서 삭제`}
                title="이 기기에서 삭제"
                disabled={controlsDisabled}
                onClick={() => onRequestRemove(account)}
              >
                <RemoveIcon />
              </button>
            </li>
          );
        })}
        <li
          className={`auth-account-entry auth-account-entry--other${
            controlsDisabled ? " is-disabled" : ""
          }`}
        >
          <button
            className="auth-account-select auth-use-another"
            type="button"
            disabled={controlsDisabled}
            onClick={onUseAnother}
          >
            <span className="auth-account-avatar auth-account-avatar--other" aria-hidden="true">
              <OtherAccountIcon />
            </span>
            <span className="auth-account-copy">
              <strong>다른 계정 사용</strong>
              <span>새 이메일로 로그인합니다.</span>
            </span>
            <span className="auth-account-chevron">
              <ChevronIcon />
            </span>
          </button>
        </li>
      </ul>
      <span className="auth-account-live-status" role="status" aria-live="polite" aria-atomic="true">
        {busyAccount ? `${busyAccount.displayName} 계정으로 전환 중` : ""}
      </span>
    </>
  );
}

export function RememberedAccountRemovalDialog({
  account,
  onCancel,
  onConfirm,
}: {
  account: RememberedAccount;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    document.body.classList.add("auth-account-dialog-open");

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = [
        ...(dialogRef.current?.querySelectorAll<HTMLButtonElement>(
          "button:not(:disabled)",
        ) ?? []),
      ];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last ||
          !dialogRef.current?.contains(document.activeElement))
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      document.body.classList.remove("auth-account-dialog-open");
      window.removeEventListener("keydown", onKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [onCancel]);

  return (
    <div
      className="verify-overlay auth-account-remove-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <section
        className="verify-dialog auth-account-remove-dialog"
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="removeRememberedAccountTitle"
        aria-describedby="removeRememberedAccountDescription"
      >
        <div className="verify-dialog-header">
          <div>
            <p className="verify-kicker">REMEMBERED ACCOUNT</p>
            <h2 id="removeRememberedAccountTitle">이 기기에서 이 계정을 삭제할까요?</h2>
          </div>
        </div>

        <div className="verify-dialog-body">
          <div className="auth-removal-account">
            <RememberedAccountAvatar account={account} />
            <span className="auth-account-copy">
              <strong>{account.displayName}</strong>
              <span>{account.email}</span>
            </span>
          </div>
          <p id="removeRememberedAccountDescription">
            저장된 로그인 상태와 계정 정보가 이 브라우저에서 삭제됩니다. Blog-it 회원
            계정 자체는 삭제되지 않습니다.
          </p>
        </div>

        <div className="verify-dialog-actions">
          <button className="button" type="button" ref={cancelRef} onClick={onCancel}>
            취소
          </button>
          <button className="button danger" type="button" onClick={onConfirm}>
            이 기기에서 삭제
          </button>
        </div>
      </section>
    </div>
  );
}
