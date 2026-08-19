/**
 * 라이브 뷰 API — 서버에서 도는 발행 크롬 화면을 보고 조작한다.
 *
 * 발행·로그인 크롬은 서버 PC에 뜬다. 외부 PC 사용자가 2단계 인증·캡차를 처리하려면
 * 그 화면을 봐야 한다. 프레임은 SSE로 오는데 브라우저 EventSource는 Authorization
 * 헤더를 못 실으므로 fetch 스트리밍으로 직접 읽는다.
 */

import { apiUrl, authToken, request } from "./client";

export interface LiveSessionInfo {
  channel: string;
  label: string;
  /** login | publish | preview — 어느 화면에 보여줄 중계인지. */
  kind: string;
  active: boolean;
  startedAt: number;
}

export interface LiveFramePayload {
  seq: number;
  /** base64 JPEG */
  image: string;
  width: number;
  height: number;
}

export type LiveInputEvent =
  | { type: "click"; x: number; y: number; button?: string; clickCount?: number; modifiers?: number }
  | { type: "wheel"; x: number; y: number; deltaX?: number; deltaY?: number }
  | { type: "text"; text: string }
  | { type: "key"; key: string; modifiers?: number };

export async function fetchLiveSessions(): Promise<LiveSessionInfo[]> {
  const answer = await request<{ sessions?: LiveSessionInfo[] }>("/live/sessions");
  return answer?.sessions ?? [];
}

export async function sendLiveInput(
  channel: string,
  events: LiveInputEvent[],
): Promise<void> {
  await request(`/live/${channel}/input`, { method: "POST", body: { events } });
}

export interface LiveStreamHandlers {
  onFrame: (frame: LiveFramePayload) => void;
  /** 세션이 없거나(404) 스트림이 끝났다. 다시 열지 말지는 부르는 쪽이 정한다. */
  onEnd: (reason: "no-session" | "closed" | "error") => void;
}

/**
 * 프레임 스트림을 연다. 돌려주는 함수를 부르면 닫힌다.
 *
 * SSE 규격의 최소만 파싱한다: `event:`/`data:` 줄과 빈 줄 구분. 주석(`:`)은 keepalive다.
 */
export function openLiveStream(
  channel: string,
  handlers: LiveStreamHandlers,
): () => void {
  const controller = new AbortController();
  let stopped = false;

  void (async () => {
    let reason: "no-session" | "closed" | "error" = "error";
    try {
      const token = authToken();
      const response = await fetch(apiUrl(`/live/${channel}/stream`), {
        headers: token ? { authorization: `Bearer ${token}` } : {},
        signal: controller.signal,
      });
      if (response.status === 404) {
        reason = "no-session";
        return;
      }
      if (!response.ok || !response.body) return;

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE 이벤트는 빈 줄로 끝난다. 마지막 조각은 다음 read에 이어 붙인다.
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";
        for (const chunk of chunks) {
          let eventName = "";
          let data = "";
          for (const line of chunk.split("\n")) {
            if (line.startsWith("event: ")) eventName = line.slice(7).trim();
            else if (line.startsWith("data: ")) data += line.slice(6);
          }
          if (eventName === "frame" && data) {
            try {
              handlers.onFrame(JSON.parse(data) as LiveFramePayload);
            } catch {
              // 프레임 하나가 깨져도 스트림은 계속 본다.
            }
          } else if (eventName === "closed") {
            reason = "closed";
            return;
          }
        }
      }
      reason = "closed";
    } catch {
      // abort(정상 종료) 포함 — stopped면 onEnd를 부르지 않는다.
    } finally {
      if (!stopped) handlers.onEnd(reason);
    }
  })();

  return () => {
    stopped = true;
    controller.abort();
  };
}
