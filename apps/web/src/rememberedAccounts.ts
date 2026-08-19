export const REMEMBERED_ACCOUNTS_STORAGE_KEY = "blogit.rememberedAccounts.v1";
export const MAX_REMEMBERED_ACCOUNTS = 5;

export interface RememberedAccount {
  userId: string;
  email: string;
  displayName: string;
  profileImage: string | null;
  lastUsedAt: string;
}

export type RememberedAccountInput = Pick<RememberedAccount, "userId" | "email"> &
  Partial<Pick<RememberedAccount, "displayName" | "profileImage">>;

interface RememberedAccountRemovalResult {
  accounts: RememberedAccount[];
  removed: boolean;
}

interface StoredAccountsRead {
  accounts: RememberedAccount[];
  readable: boolean;
}

/**
 * Full email addresses are needed by the account chooser, but they do not need
 * to survive a browser session. Move a legacy list into tab storage once when
 * possible and erase the persistent copy. New account metadata is never written
 * to localStorage.
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
    const legacyValue = legacyStorage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY);
    if (legacyValue !== null && storage) {
      try {
        if (storage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY) === null) {
          storage.setItem(REMEMBERED_ACCOUNTS_STORAGE_KEY, legacyValue);
        }
      } catch {
        // Security takes precedence over persistence if tab storage is blocked.
      }
    }
  } catch {
    // Continue to the best-effort removal below.
  }

  try {
    legacyStorage.removeItem(REMEMBERED_ACCOUNTS_STORAGE_KEY);
  } catch {
    // The next account-list operation retries this cleanup.
  }

  return storage;
}

function normalizedEmail(email: string): string {
  return email.trim().toLowerCase();
}

function fallbackDisplayName(email: string): string {
  return email.split("@", 1)[0] || email;
}

function normalizeIdentity(value: unknown): { userId: string; email: string } | null {
  if (!value || typeof value !== "object") return null;

  const candidate = value as Record<string, unknown>;
  if (typeof candidate.userId !== "string" || typeof candidate.email !== "string") return null;

  const userId = candidate.userId.trim();
  const email = candidate.email.trim();
  const at = email.indexOf("@");
  if (!userId || at <= 0 || at === email.length - 1) return null;

  return { userId, email };
}

function normalizeProfileImage(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeStoredAccount(value: unknown): RememberedAccount | null {
  const identity = normalizeIdentity(value);
  if (!identity) return null;

  const candidate = value as Record<string, unknown>;
  if (typeof candidate.lastUsedAt !== "string") return null;

  const usedAt = new Date(candidate.lastUsedAt);
  if (!Number.isFinite(usedAt.getTime())) return null;

  const requestedName =
    typeof candidate.displayName === "string" ? candidate.displayName.trim() : "";

  return {
    userId: identity.userId,
    email: identity.email,
    displayName: requestedName || fallbackDisplayName(identity.email),
    profileImage: normalizeProfileImage(candidate.profileImage),
    lastUsedAt: usedAt.toISOString(),
  };
}

function normalizeInput(
  input: RememberedAccountInput,
  usedAt: Date,
): RememberedAccount | null {
  const identity = normalizeIdentity(input);
  if (!identity || !Number.isFinite(usedAt.getTime())) return null;

  const requestedName = typeof input.displayName === "string" ? input.displayName.trim() : "";

  return {
    userId: identity.userId,
    email: identity.email,
    displayName: requestedName || fallbackDisplayName(identity.email),
    profileImage: normalizeProfileImage(input.profileImage),
    lastUsedAt: usedAt.toISOString(),
  };
}

function usedAtMillis(account: RememberedAccount): number {
  const millis = Date.parse(account.lastUsedAt);
  return Number.isFinite(millis) ? millis : Number.NEGATIVE_INFINITY;
}

export function sortRememberedAccountsByLastUsed(
  accounts: readonly RememberedAccount[],
): RememberedAccount[] {
  return [...accounts].sort((left, right) => usedAtMillis(right) - usedAtMillis(left));
}

function canonicalAccounts(accounts: readonly RememberedAccount[]): RememberedAccount[] {
  const userIds = new Set<string>();
  const emails = new Set<string>();
  const result: RememberedAccount[] = [];

  for (const account of sortRememberedAccountsByLastUsed(accounts)) {
    const emailKey = normalizedEmail(account.email);
    if (userIds.has(account.userId) || emails.has(emailKey)) continue;

    userIds.add(account.userId);
    emails.add(emailKey);
    result.push(account);
    if (result.length === MAX_REMEMBERED_ACCOUNTS) break;
  }

  return result;
}

function resetCorruptedStorage(storage: Storage): StoredAccountsRead {
  try {
    storage.removeItem(REMEMBERED_ACCOUNTS_STORAGE_KEY);
    return { accounts: [], readable: true };
  } catch {
    // Blocked browser storage must not prevent the login screen from rendering.
    return { accounts: [], readable: false };
  }
}

function writeAccounts(storage: Storage, accounts: readonly RememberedAccount[]): boolean {
  try {
    storage.setItem(REMEMBERED_ACCOUNTS_STORAGE_KEY, JSON.stringify(accounts));
    return true;
  } catch {
    return false;
  }
}

function readAccountsResult(storage: Storage): StoredAccountsRead {
  let raw: string | null;
  try {
    raw = storage.getItem(REMEMBERED_ACCOUNTS_STORAGE_KEY);
  } catch {
    return { accounts: [], readable: false };
  }

  if (raw === null) return { accounts: [], readable: true };

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return resetCorruptedStorage(storage);
  }

  if (!Array.isArray(parsed)) return resetCorruptedStorage(storage);

  const normalized: RememberedAccount[] = [];
  for (const item of parsed) {
    const account = normalizeStoredAccount(item);
    if (!account) return resetCorruptedStorage(storage);
    normalized.push(account);
  }

  const canonical = canonicalAccounts(normalized);
  // Rewriting strips unexpected properties and repairs order, duplicates,
  // missing optional fields, and legacy lists over the supported maximum.
  writeAccounts(storage, canonical);
  return { accounts: canonical, readable: true };
}

function readAccounts(storage: Storage): RememberedAccount[] {
  return readAccountsResult(storage).accounts;
}

export function getRememberedAccounts(): RememberedAccount[] {
  const storage = getBrowserStorage();
  return storage ? readAccounts(storage) : [];
}

export function saveRememberedAccount(
  input: RememberedAccountInput,
  usedAt: Date = new Date(),
): RememberedAccount[] {
  const storage = getBrowserStorage();
  if (!storage) return [];

  const currentResult = readAccountsResult(storage);
  if (!currentResult.readable) return [];

  const current = currentResult.accounts;
  const nextAccount = normalizeInput(input, usedAt);
  if (!nextAccount) return current;

  const nextEmail = normalizedEmail(nextAccount.email);
  const withoutDuplicate = current.filter(
    (account) =>
      account.userId !== nextAccount.userId && normalizedEmail(account.email) !== nextEmail,
  );
  const next = canonicalAccounts([nextAccount, ...withoutDuplicate]);

  return writeAccounts(storage, next) ? next : current;
}

export function removeRememberedAccount(
  accountId: string,
): RememberedAccountRemovalResult {
  const storage = getBrowserStorage();
  if (!storage) return { accounts: [], removed: false };

  const currentResult = readAccountsResult(storage);
  if (!currentResult.readable) return { accounts: [], removed: false };

  const current = currentResult.accounts;
  const normalizedId = accountId.trim();
  const normalizedEmailKey = normalizedEmail(normalizedId);
  const next = current.filter(
    (account) =>
      account.userId !== normalizedId &&
      normalizedEmail(account.email) !== normalizedEmailKey,
  );
  if (next.length === current.length) return { accounts: current, removed: false };

  return writeAccounts(storage, next)
    ? { accounts: next, removed: true }
    : { accounts: current, removed: false };
}

export function clearRememberedAccounts(): void {
  const storage = getBrowserStorage();
  if (!storage) return;

  try {
    storage.removeItem(REMEMBERED_ACCOUNTS_STORAGE_KEY);
  } catch {
    // Clearing remembered accounts is best effort, just like reading them.
  }
}
