import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * 라이브 뷰 스트림 파서. EventSource를 못 쓰고(fetch 스트리밍, 인증 헤더 때문)
 * SSE를 손으로 파싱하므로, 조각난 청크·keepalive·종료 이벤트를 규약대로 다루는지 본다.
 */
vi.mock("./client", () => ({
  authToken: () => "token-1",
  apiUrl: (path: string) => path,
  request: vi.fn(),
}));

import { openLiveStream } from "./live";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

function respondWith(chunks: string[], status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      status,
      ok: status >= 200 && status < 300,
      body: streamOf(chunks),
    })) as unknown as typeof fetch,
  );
}

async function collect(chunks: string[], status = 200) {
  respondWith(chunks, status);
  const frames: unknown[] = [];
  let ended: string | null = null;
  openLiveStream("naver", {
    onFrame: (frame) => frames.push(frame),
    onEnd: (reason) => {
      ended = reason;
    },
  });
  // 스트림 소비는 비동기다 — 끝났다는 신호까지 잠깐 기다린다.
  for (let i = 0; i < 50 && ended === null; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return { frames, ended };
}

describe("openLiveStream", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("프레임 이벤트를 파싱해 전달한다", async () => {
    const frame = JSON.stringify({ seq: 1, image: "aGk=", width: 100, height: 50 });
    const { frames, ended } = await collect([
      `event: status\ndata: {}\n\n`,
      `event: frame\ndata: ${frame}\n\n`,
    ]);
    expect(frames).toEqual([{ seq: 1, image: "aGk=", width: 100, height: 50 }]);
    expect(ended).toBe("closed");
  });

  it("청크 경계에서 잘린 이벤트도 이어 붙여 파싱한다", async () => {
    const frame = JSON.stringify({ seq: 2, image: "eW8=", width: 10, height: 10 });
    const whole = `event: frame\ndata: ${frame}\n\n`;
    const { frames } = await collect([whole.slice(0, 20), whole.slice(20)]);
    expect(frames).toHaveLength(1);
  });

  it("keepalive 주석은 프레임으로 만들지 않는다", async () => {
    const { frames } = await collect([`: keepalive\n\n`]);
    expect(frames).toHaveLength(0);
  });

  it("세션이 없으면(404) no-session으로 끝난다", async () => {
    const { ended } = await collect([], 404);
    expect(ended).toBe("no-session");
  });

  it("closed 이벤트를 받으면 closed로 끝난다", async () => {
    const { ended } = await collect([`event: closed\ndata: {}\n\n`]);
    expect(ended).toBe("closed");
  });
});
