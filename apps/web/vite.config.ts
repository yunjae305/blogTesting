import react from "@vitejs/plugin-react";
import { createLogger } from "vite";
import { defineConfig } from "vitest/config";

// The app calls the API with same-origin relative paths, so the dev server
// proxies them. API_TARGET lets us point at either backend while both exist:
// the Node server (3000) or the FastAPI one (3001).
const API_TARGET = process.env.API_TARGET ?? "http://127.0.0.1:3000";

// Every top-level path the API serves. A route missing from this list does not fail
// loudly in dev — Vite answers it with index.html, the frontend fails to parse that as
// JSON, and the user gets "요청에 실패했습니다" while the API sits there having never
// been asked. That is exactly what /naver did.
// (2026-08-04) /posting을 빠뜨려 같은 일이 또 났다: 2단계 인증 코드 입력창이 영영 뜨지
// 않았는데, 폴링이 받은 index.html을 catch가 조용히 삼켜 아무 단서도 남지 않았다.
const API_ROUTES = [
  "/auth",
  "/posts",
  "/users",
  "/personas",
  "/health",
  "/naver",
  "/threads",
  "/posting",
  "/scheduled",
  "/brands",
  "/trends",
  "/live",
];

/**
 * API가 아직 안 떠 있을 때의 로그 소음을 줄인다.
 *
 * `npm run dev`를 백엔드보다 먼저 띄우면(흔한 순서다) 화면이 부르는 요청마다 Vite가
 * ECONNREFUSED **스택 트레이스**를 찍는다. 화면 하나 여는 데 예닐곱 줄씩 쏟아져서,
 * 정작 봐야 할 로그가 묻힌다.
 *
 * 그래서 **연결 거부만** 한 줄로 줄인다. 그것도 처음 한 번만 찍고, 프록시가 한 번이라도
 * 응답을 받으면 다시 무장한다 — 돌던 백엔드가 중간에 죽는 것은 알아야 하기 때문이다.
 *
 * **다른 프록시 오류는 그대로 시끄럽게 둔다.** 이 목록에서 경로가 빠지는 실수는 조용히
 * 깨지는 쪽이라(위 주석 참고) 로그를 더 줄이면 안 된다.
 */
let apiDownReported = false;

const logger = createLogger();
const logError = logger.error.bind(logger);
logger.error = (message, options) => {
  const refused =
    (options?.error as NodeJS.ErrnoException | undefined)?.code === "ECONNREFUSED" ||
    message.includes("ECONNREFUSED");
  if (refused && message.includes("http proxy error")) {
    if (apiDownReported) return;
    apiDownReported = true;
    logError(
      `API 서버에 연결하지 못했습니다 (${API_TARGET}). 백엔드를 띄우면 그대로 이어집니다.`,
      { timestamp: true },
    );
    return;
  }
  logError(message, options);
};

export default defineConfig({
  plugins: [react()],
  customLogger: logger,
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      API_ROUTES.map((route) => [
        route,
        {
          target: API_TARGET,
          changeOrigin: true,
          // 응답이 한 번이라도 오면 "안 떠 있다"는 안내를 다시 할 수 있게 되돌린다.
          configure: (proxy) => {
            proxy.on("proxyRes", () => {
              apiDownReported = false;
            });
          },
        },
      ]),
    ),
  },
  build: {
    outDir: "dist",
  },
  test: {
    environment: "happy-dom",
  },
});
