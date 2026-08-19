/** API client. Port of the request()/session helpers in apps/web/public/app.js. */

import { ERROR_MESSAGES } from "../constants";
import type { AuthSession } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const SESSION_KEY = "blog-it:session";

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

class NetworkError extends Error {
  name = "NetworkError";
}

export function friendlyError(error: unknown): string {
  if (error instanceof ApiError) return (error.code && ERROR_MESSAGES[error.code]) || error.message;
  if (error instanceof NetworkError) {
    return "서버와의 연결이 끊어졌습니다. API 서버가 실행 중인지 확인해 주세요.";
  }
  // A bug in our code, not a failed request. Surface it instead of blaming the network.
  console.error(error);
  // 무슨 일이 났는지 그대로 붙인다. "예상치 못한 오류"만 보여 주면 개발자 도구를 열기
  // 전까지 아무것도 알 수 없다 — 실제로 그것 때문에 원인을 엉뚱한 곳에서 찾았다.
  const detail = error instanceof Error ? error.message : String(error ?? "");
  return detail ? `화면에서 오류가 났습니다: ${detail}` : "예상치 못한 오류가 발생했습니다.";
}

/**
 * 실패한 HTTP 응답을 **사유가 보이는** 한 문장으로.
 *
 * 예전에는 본문에 message가 없으면 무조건 "요청에 실패했습니다"였다. API 서버가 꺼져
 * 있을 때가 정확히 그 경우다 — 개발 서버의 프록시가 대신 500을 돌려주고 본문은 비어
 * 있다. 그래서 "서버가 안 떠 있다"는 사실이 "요청에 실패했습니다"로 뭉개졌다.
 *
 * 서버가 사유를 말해 줬으면 그것이 먼저다. 아무 말도 없을 때만 상태 코드로 짐작하고,
 * **짐작이라는 것과 상태 코드를 함께 보여 준다.**
 */
function httpFailureMessage(status: number, payload: unknown): string {
  const body = (payload ?? null) as {
    errors?: { message: string }[];
    message?: unknown;
  } | null;

  // Settings validation returns a field-level list; join it into one message.
  const detail = body?.errors?.map((item) => item.message).join(" ");
  if (detail) return detail;
  if (typeof body?.message === "string" && body.message.trim()) return body.message;

  // 본문이 비었다 = API가 답한 것이 아니다. 우리 API는 실패에도 항상 message를 싣는다.
  if (status >= 500) {
    return `서버와의 연결이 끊어졌습니다 (HTTP ${status}). API 서버가 실행 중인지 확인해 주세요.`;
  }
  if (status === 401) return "로그인이 만료되었습니다. 다시 로그인해 주세요.";
  if (status === 403) return "이 작업을 할 권한이 없습니다.";
  if (status === 404) return `서버에 없는 요청입니다 (HTTP 404).`;
  if (status === 413) return "보낸 자료가 너무 큽니다. 파일이나 이미지를 줄여 주세요.";
  if (status === 429) return "요청이 너무 잦습니다. 잠시 뒤에 다시 시도해 주세요.";
  return `요청이 거절되었습니다 (HTTP ${status}).`;
}

/* ------------------------------------------------------------------ session */

/**
 * Authentication state is scoped to the current browser tab. During the
 * localStorage -> sessionStorage transition, keep an existing signed-in tab
 * working when possible and always make a best-effort attempt to erase the
 * persistent copy. New credentials are never written to localStorage.
 */
function getBrowserSessionStorage(): Storage | null {
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
    const legacyValue = legacyStorage.getItem(SESSION_KEY);
    if (legacyValue !== null && storage) {
      try {
        if (storage.getItem(SESSION_KEY) === null) {
          storage.setItem(SESSION_KEY, legacyValue);
        }
      } catch {
        // Security takes precedence over persistence if tab storage is blocked.
      }
    }
  } catch {
    // A blocked read must not prevent use of an otherwise available session store.
  }

  try {
    legacyStorage.removeItem(SESSION_KEY);
  } catch {
    // Best effort: the next session operation retries this cleanup.
  }

  return storage;
}

function validStoredSession(value: unknown): value is AuthSession {
  if (!value || typeof value !== "object") return false;
  const session = value as Record<string, unknown>;
  const user =
    session.user && typeof session.user === "object"
      ? (session.user as Record<string, unknown>)
      : null;
  if (
    !user ||
    typeof user.userId !== "string" ||
    !user.userId.trim() ||
    typeof user.email !== "string" ||
    !user.email.includes("@") ||
    typeof user.nickname !== "string" ||
    typeof user.createdAt !== "string" ||
    typeof user.updatedAt !== "string" ||
    typeof session.accessToken !== "string" ||
    !session.accessToken.trim() ||
    typeof session.issuedAt !== "string" ||
    typeof session.expiresAt !== "string"
  ) {
    return false;
  }

  const createdAt = Date.parse(user.createdAt);
  const updatedAt = Date.parse(user.updatedAt);
  const issuedAt = Date.parse(session.issuedAt);
  const expiresAt = Date.parse(session.expiresAt);
  return (
    Number.isFinite(createdAt) &&
    Number.isFinite(updatedAt) &&
    Number.isFinite(issuedAt) &&
    Number.isFinite(expiresAt) &&
    expiresAt > issuedAt &&
    expiresAt > Date.now()
  );
}

export function loadSession(): AuthSession | null {
  const storage = getBrowserSessionStorage();
  if (!storage) return null;

  try {
    const stored = JSON.parse(storage.getItem(SESSION_KEY) ?? "null");
    if (validStoredSession(stored)) return stored;
    storage.removeItem(SESSION_KEY);
  } catch {
    /* fall through to a signed-out start */
  }
  return null;
}

export function storeSession(session: AuthSession): boolean {
  const storage = getBrowserSessionStorage();
  if (!storage) return false;
  try {
    storage.setItem(SESSION_KEY, JSON.stringify(session));
    return true;
  } catch {
    return false;
  }
}

export function clearSession(): boolean {
  const storage = getBrowserSessionStorage();
  if (!storage) return false;
  try {
    storage.removeItem(SESSION_KEY);
    return true;
  } catch {
    return false;
  }
}

/* ------------------------------------------------------------------ request */

// Set by the app so a 401 can drop the session without importing React state here.
let onUnauthorized: (() => void) | null = null;
let currentToken: string | null = null;

export function configureAuth(token: string | null, unauthorizedHandler: () => void): void {
  currentToken = token;
  onUnauthorized = unauthorizedHandler;
}

/**
 * 지금 활성 계정의 토큰. 라이브 뷰 스트림(fetch 스트리밍)처럼 `request()`를 못 쓰는
 * 곳이 Authorization 헤더를 직접 실을 때만 쓴다.
 */
export function authToken(): string | null {
  return currentToken;
}

/**
 * API 절대 경로. `request()`를 거치지 않는 fetch(라이브 뷰 SSE)도 VITE_API_BASE
 * 구성을 따라야 한다 — 안 그러면 API가 다른 오리진일 때 스트림만 웹 서버로 간다.
 */
export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
}

async function sendRequest<T>(
  path: string,
  options: RequestOptions,
  token: string | null,
  notifyUnauthorized: boolean,
): Promise<T> {
  const { method = "GET", body } = options;

  const headers: Record<string, string> = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (token) headers.authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new NetworkError(`${method} ${path} failed to reach the server`);
  }

  if (response.status === 204) return null as T;

  // An HTML answer means the request never reached the API — in dev, that is the Vite
  // proxy handing back index.html for a path it was not told to forward. Parsing that
  // as JSON gives null, and the user gets a shrug ("요청에 실패했습니다") while the API
  // sits there having never been asked. Say what actually happened.
  if (response.headers.get("content-type")?.includes("text/html")) {
    throw new ApiError(
      `${method} ${path}이(가) API가 아니라 웹 서버로 갔습니다. vite.config.ts의 프록시 목록을 확인해 주세요.`,
      response.status,
      "NOT_PROXIED",
    );
  }

  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    // A request from account A can finish after the user has already switched
    // to account B. Only the token that is still active may sign the app out.
    if (response.status === 401 && notifyUnauthorized && token && token === currentToken) {
      onUnauthorized?.();
    }
    throw new ApiError(
      httpFailureMessage(response.status, payload),
      response.status,
      payload?.errorCode,
    );
  }

  // /posts responses are wrapped in {success, data}; auth and settings are not.
  return (payload?.data ?? payload) as T;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  // Capture the token when the request starts. Reading the global token after the
  // response arrives would let an old account's 401 log out the newly selected one.
  const requestToken = currentToken;
  return sendRequest<T>(path, options, requestToken, true);
}

/**
 * Sends a public request without inheriting the currently active account.
 * Login and signup use this while the account chooser is open over another
 * account, so a rejected password cannot sign that existing account out.
 */
export async function requestPublic<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  return sendRequest<T>(path, options, null, false);
}

/**
 * Sends an isolated authentication probe with a candidate account token.
 *
 * It never changes the app-wide active token and a 401 never invokes the active
 * account's unauthorized handler. This is used only to validate a saved account
 * before switching to it.
 */
export async function requestWithToken<T>(
  path: string,
  token: string,
  options: RequestOptions = {},
): Promise<T> {
  return sendRequest<T>(path, options, token, false);
}

/**
 * Sends work owned by a specific signed-in session with its original token.
 *
 * Unlike a candidate-account probe, a 401 signs the app out when that token is
 * still active. If the user has already switched accounts, the token mismatch
 * keeps the new account signed in.
 */
export async function requestWithSessionToken<T>(
  path: string,
  token: string,
  options: RequestOptions = {},
): Promise<T> {
  return sendRequest<T>(path, options, token, true);
}
