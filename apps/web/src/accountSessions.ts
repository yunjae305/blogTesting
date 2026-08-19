import type { AuthSession, PublicUser } from "./api/types";

export const ACCOUNT_SESSIONS_STORAGE_KEY = "blogit.accountSessions.v1";
export const MAX_ACCOUNT_SESSIONS = 5;

export interface StoredAccountSession extends AuthSession {
  lastUsedAt: string;
}

type AccountSessionFailureReason =
  | "storage-unavailable"
  | "storage-read-failed"
  | "storage-write-failed"
  | "invalid-session";

type AccountSessionsResult =
  | {
      ok: true;
      sessions: StoredAccountSession[];
      changed: boolean;
    }
  | {
      ok: false;
      sessions: StoredAccountSession[];
      changed: false;
      reason: AccountSessionFailureReason;
    };

type AccountSessionLookupResult =
  | {
      ok: true;
      session: StoredAccountSession | null;
      sessions: StoredAccountSession[];
      changed: boolean;
    }
  | {
      ok: false;
      session: null;
      sessions: StoredAccountSession[];
      changed: false;
      reason: AccountSessionFailureReason;
    };

function success(
  sessions: StoredAccountSession[],
  changed = false,
): AccountSessionsResult {
  return { ok: true, sessions, changed };
}

function failure(
  reason: AccountSessionFailureReason,
  sessions: StoredAccountSession[] = [],
): AccountSessionsResult {
  return { ok: false, sessions, changed: false, reason };
}

/**
 * Keep bearer tokens tab-scoped. Existing localStorage data is moved once when
 * possible, then the persistent source is erased. Every call retries cleanup so
 * a temporary browser-policy failure cannot silently become permanent.
 */
function getBrowserStorage(): Storage | null {
  if (typeof window === "undefined") return null;

  let storage: Storage | null = null;
  try {
    storage = window.sessionStorage ?? null;
  } catch {
    // Cleanup below should still run when sessionStorage is blocked.
  }

  let legacyStorage: Storage | null = null;
  try {
    legacyStorage = window.localStorage ?? null;
  } catch {
    return storage;
  }

  if (!legacyStorage || legacyStorage === storage) return storage;

  try {
    const legacyValue = legacyStorage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY);
    if (legacyValue !== null && storage) {
      try {
        if (storage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) === null) {
          storage.setItem(ACCOUNT_SESSIONS_STORAGE_KEY, legacyValue);
        }
      } catch {
        // Security takes precedence over persistence if tab storage is blocked.
      }
    }
  } catch {
    // Continue to the best-effort removal below.
  }

  try {
    legacyStorage.removeItem(ACCOUNT_SESSIONS_STORAGE_KEY);
  } catch {
    // The next vault operation retries this cleanup.
  }

  return storage;
}

function normalizedEmail(email: string): string {
  return email.trim().toLowerCase();
}

function validEmail(email: string): boolean {
  const at = email.indexOf("@");
  return at > 0 && at < email.length - 1;
}

function normalizedIsoDate(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}

function projectUser(value: unknown): PublicUser | null {
  if (!value || typeof value !== "object") return null;

  const user = value as Record<string, unknown>;
  if (
    typeof user.userId !== "string" ||
    typeof user.email !== "string" ||
    typeof user.nickname !== "string"
  ) {
    return null;
  }

  const userId = user.userId.trim();
  const email = user.email.trim();
  const createdAt = normalizedIsoDate(user.createdAt);
  const updatedAt = normalizedIsoDate(user.updatedAt);
  if (!userId || !validEmail(email) || !createdAt || !updatedAt) return null;

  return {
    userId,
    email,
    nickname: user.nickname,
    createdAt,
    updatedAt,
  };
}

function projectSession(
  value: unknown,
  lastUsedAtValue: unknown,
  nowMillis: number,
): StoredAccountSession | null {
  if (!value || typeof value !== "object") return null;

  const session = value as Record<string, unknown>;
  const user = projectUser(session.user);
  if (!user || typeof session.accessToken !== "string" || !session.accessToken.trim()) {
    return null;
  }

  const issuedAt = normalizedIsoDate(session.issuedAt);
  const expiresAt = normalizedIsoDate(session.expiresAt);
  const lastUsedAt = normalizedIsoDate(lastUsedAtValue);
  if (!issuedAt || !expiresAt || !lastUsedAt) return null;

  const issuedAtMillis = Date.parse(issuedAt);
  const expiresAtMillis = Date.parse(expiresAt);
  const lastUsedAtMillis = Date.parse(lastUsedAt);
  if (
    expiresAtMillis <= nowMillis ||
    expiresAtMillis <= issuedAtMillis ||
    lastUsedAtMillis > expiresAtMillis
  ) {
    return null;
  }

  return {
    user,
    accessToken: session.accessToken.trim(),
    issuedAt,
    expiresAt,
    lastUsedAt,
  };
}

function projectStoredSession(value: unknown, nowMillis: number): StoredAccountSession | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  return projectSession(record, record.lastUsedAt, nowMillis);
}

function lastUsedMillis(session: StoredAccountSession): number {
  return Date.parse(session.lastUsedAt);
}

function canonicalSessions(
  sessions: readonly StoredAccountSession[],
): StoredAccountSession[] {
  const userIds = new Set<string>();
  const emails = new Set<string>();
  const canonical: StoredAccountSession[] = [];
  const sorted = [...sessions].sort(
    (left, right) => lastUsedMillis(right) - lastUsedMillis(left),
  );

  for (const session of sorted) {
    const emailKey = normalizedEmail(session.user.email);
    if (userIds.has(session.user.userId) || emails.has(emailKey)) continue;

    userIds.add(session.user.userId);
    emails.add(emailKey);
    canonical.push(session);
    if (canonical.length === MAX_ACCOUNT_SESSIONS) break;
  }

  return canonical;
}

function writeSessions(
  storage: Storage,
  sessions: readonly StoredAccountSession[],
): boolean {
  try {
    storage.setItem(ACCOUNT_SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
    return true;
  } catch {
    return false;
  }
}

function resetCorruptedStorage(storage: Storage): AccountSessionsResult {
  try {
    storage.removeItem(ACCOUNT_SESSIONS_STORAGE_KEY);
    return success([], true);
  } catch {
    return failure("storage-write-failed");
  }
}

function getAccountSessionsFromStorage(
  storage: Storage,
  nowMillis: number,
): AccountSessionsResult {
  let raw: string | null;
  try {
    raw = storage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY);
  } catch {
    return failure("storage-read-failed");
  }

  if (raw === null) return success([]);

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return resetCorruptedStorage(storage);
  }

  if (!Array.isArray(parsed)) return resetCorruptedStorage(storage);

  const valid: StoredAccountSession[] = [];
  for (const item of parsed) {
    const projected = projectStoredSession(item, nowMillis);
    if (projected) valid.push(projected);
  }

  const canonical = canonicalSessions(valid);
  const repaired = JSON.stringify(parsed) !== JSON.stringify(canonical);
  if (repaired && !writeSessions(storage, canonical)) {
    return failure("storage-write-failed", canonical);
  }

  return success(canonical, repaired);
}

function nowMillis(now: Date): number | null {
  const millis = now.getTime();
  return Number.isFinite(millis) ? millis : null;
}

/**
 * Returns all usable account sessions. Expired or malformed records are removed
 * before they can be selected, and any repaired list is written back.
 */
export function getAccountSessions(now: Date = new Date()): AccountSessionsResult {
  const storage = getBrowserStorage();
  if (!storage) return failure("storage-unavailable");

  const currentTime = nowMillis(now);
  if (currentTime === null) return failure("invalid-session");
  return getAccountSessionsFromStorage(storage, currentTime);
}

/**
 * Stores a newly issued session and moves it to the front. A match by user id OR
 * normalized email replaces the previous token rather than adding a duplicate.
 */
export function saveAccountSession(
  session: AuthSession,
  usedAt: Date = new Date(),
): AccountSessionsResult {
  const storage = getBrowserStorage();
  if (!storage) return failure("storage-unavailable");

  const currentTime = nowMillis(usedAt);
  if (currentTime === null) return failure("invalid-session");

  const current = getAccountSessionsFromStorage(storage, currentTime);
  if (!current.ok) return current;

  const projected = projectSession(session, usedAt.toISOString(), currentTime);
  if (!projected) return failure("invalid-session", current.sessions);

  const emailKey = normalizedEmail(projected.user.email);
  const withoutDuplicate = current.sessions.filter(
    (candidate) =>
      candidate.user.userId !== projected.user.userId &&
      normalizedEmail(candidate.user.email) !== emailKey,
  );
  const next = canonicalSessions([projected, ...withoutDuplicate]);

  return writeSessions(storage, next)
    ? success(next, true)
    : failure("storage-write-failed", current.sessions);
}

/**
 * Looks up either an exact user id or a case-insensitive email address.
 */
export function findAccountSession(
  accountIdOrEmail: string,
  now: Date = new Date(),
): AccountSessionLookupResult {
  const result = getAccountSessions(now);
  if (!result.ok) {
    return {
      ok: false,
      session: null,
      sessions: result.sessions,
      changed: false,
      reason: result.reason,
    };
  }

  const identity = accountIdOrEmail.trim();
  const emailKey = normalizedEmail(identity);
  const session =
    result.sessions.find(
      (candidate) =>
        candidate.user.userId === identity ||
        normalizedEmail(candidate.user.email) === emailKey,
    ) ?? null;

  return {
    ok: true,
    session,
    sessions: result.sessions,
    changed: result.changed,
  };
}

/**
 * Removes one stored token by user id or normalized email.
 */
export function removeAccountSession(
  accountIdOrEmail: string,
  now: Date = new Date(),
): AccountSessionsResult {
  const storage = getBrowserStorage();
  if (!storage) return failure("storage-unavailable");

  const currentTime = nowMillis(now);
  if (currentTime === null) return failure("invalid-session");

  const current = getAccountSessionsFromStorage(storage, currentTime);
  if (!current.ok) return current;

  const identity = accountIdOrEmail.trim();
  const emailKey = normalizedEmail(identity);
  const next = current.sessions.filter(
    (candidate) =>
      candidate.user.userId !== identity &&
      normalizedEmail(candidate.user.email) !== emailKey,
  );
  if (next.length === current.sessions.length) {
    return success(current.sessions, current.changed);
  }

  return writeSessions(storage, next)
    ? success(next, true)
    : failure("storage-write-failed", current.sessions);
}

/**
 * Clears only the tab-scoped multi-account session vault. Remembered account
 * metadata and the single active-session key are intentionally outside this utility.
 */
export function clearAccountSessions(): AccountSessionsResult {
  const storage = getBrowserStorage();
  if (!storage) return failure("storage-unavailable");

  let existed: boolean;
  try {
    existed = storage.getItem(ACCOUNT_SESSIONS_STORAGE_KEY) !== null;
  } catch {
    return failure("storage-read-failed");
  }

  try {
    storage.removeItem(ACCOUNT_SESSIONS_STORAGE_KEY);
    return success([], existed);
  } catch {
    return failure("storage-write-failed");
  }
}
