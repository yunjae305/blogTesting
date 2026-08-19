import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  type WheelEvent,
} from "react";

import {
  openLiveStream,
  sendLiveInput,
  type LiveFramePayload,
  type LiveInputEvent,
} from "../api/live";

/** 화면에서 키 이벤트로 그대로 넘길 수 있는 특수 키(서버의 _SPECIAL_KEYS와 같은 목록). */
const FORWARDED_KEYS = new Set([
  "Enter",
  "Backspace",
  "Tab",
  "Escape",
  "Delete",
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);

/** CDP 수정키 비트: Alt=1, Ctrl=2, Meta=4, Shift=8. */
function modifierBits(event: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
}

/**
 * 서버에서 도는 발행 크롬 화면 하나를 보여 주고 조작하게 한다.
 *
 * 발행·로그인 크롬은 서버 PC에 뜬다 — 외부 PC 사용자가 로그인 화면·2단계 인증·캡차를
 * 처리할 유일한 길이 이 화면이다. 화면을 클릭하면 그 좌표(0~1 정규화)가 크롬에
 * 전달되고, 글자는 아래 입력칸으로 보낸다(한글은 키 이벤트로 조립할 수 없어서다).
 *
 * 스트림이 끊기면 3초마다 다시 붙는다 — 발행 코드가 크롬을 새로 여는 순간(세션 교체)
 * 스트림이 한 번 끊기는 것이 정상이라, 끊김을 오류로 보여 주지 않는다.
 */
export function LiveBrowserView({
  channel,
  label,
  withTyping = false,
  typingPlaceholder,
  onSendText,
  actions,
}: {
  channel: string;
  label?: string;
  /**
   * true면 화면 아래에 텍스트 전송칸(인증코드·한글용)을 붙인다. 로그인 카드처럼
   * 사용자가 **입력해야 하는** 자리만 켠다 — 발행 중계는 지켜보는 것이 목적이라
   * 입력칸이 화면만 좁힌다(2026-08-18 사용자 지적). 클릭·키보드 전달은 어느 모드든
   * 된다(캡차 같은 돌발 화면 대비).
   */
  withTyping?: boolean;
  /** 전송칸 안내 문구를 바꾼다 — 2단계 인증 중에는 '인증 코드를 입력' 같은 문구. */
  typingPlaceholder?: string;
  /**
   * 전송칸의 목적지를 바꾼다. 기본은 크롬에 글자를 그대로 흘리는 것(insertText)인데,
   * 2단계 인증 중에는 코드 창구(/posting/verification)로 보내 자동화가 사람 속도로
   * 대신 입력·제출하게 한다(2026-08-18 사용자 요청 — 입력은 이 줄 하나로 통일).
   */
  onSendText?: (text: string) => void | Promise<void>;
  /** 전송칸 옆에 붙일 버튼들 — '코드 재전송'·'백업 코드 사용' 같은 것. */
  actions?: ReactNode;
}) {
  const [frame, setFrame] = useState<LiveFramePayload | null>(null);
  const [state, setState] = useState<"connecting" | "streaming" | "none">("connecting");
  const [textDraft, setTextDraft] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const screenRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  useEffect(() => {
    let closed = false;
    let close: (() => void) | null = null;
    let retry: number | null = null;

    const connect = () => {
      if (closed) return;
      close = openLiveStream(channel, {
        onFrame: (next) => {
          setFrame(next);
          setState("streaming");
        },
        onEnd: (reason) => {
          if (closed) return;
          setState(reason === "no-session" ? "none" : "connecting");
          retry = window.setTimeout(connect, 3000);
        },
      });
    };
    connect();

    return () => {
      closed = true;
      if (retry !== null) window.clearTimeout(retry);
      close?.();
    };
  }, [channel]);

  const forward = useCallback(
    (events: LiveInputEvent[]) => {
      void sendLiveInput(channel, events)
        .then(() => setInputError(null))
        .catch((error) => {
          // 조용히 삼키면 "클릭이 안 되는데 왜인지 모른다"가 된다(2026-08-18 실사용).
          // 화면에 사유를 그대로 보여 주고 콘솔에도 남긴다 — 스트림은 계속 본다.
          console.warn("라이브 뷰 입력 전달 실패:", error);
          setInputError(error instanceof Error ? error.message : String(error));
        });
    },
    [channel],
  );

  /** 클릭 지점을 이미지 기준 0~1로 바꾼다. 서버가 실제 뷰포트 픽셀로 환산한다.

   * 기준은 컨테이너가 아니라 **이미지 요소**다. 컨테이너는 min-height로 이미지보다
   * 커질 수 있고(letterbox), 그 기준으로 재면 y가 아래로 밀려 엉뚱한 곳이 눌린다.
   * 이미지가 아직 없으면(첫 프레임 전) 좌표를 만들 수 없다 — 입력을 버린다. */
  const normalized = (event: PointerEvent | WheelEvent | globalThis.WheelEvent) => {
    const box = imgRef.current?.getBoundingClientRect();
    if (!box || box.width < 1 || box.height < 1) return null;
    const x = (event.clientX - box.left) / box.width;
    const y = (event.clientY - box.top) / box.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const point = normalized(event);
    if (!point) return;
    event.preventDefault();
    screenRef.current?.focus();
    forward([
      {
        type: "click",
        ...point,
        button: event.button === 2 ? "right" : "left",
        modifiers: modifierBits(event),
      },
    ]);
  };

  // 휠은 native non-passive 리스너로 받는다. React의 합성 wheel은 passive로 붙어
  // preventDefault가 통하지 않아, 원격 화면을 스크롤하면 이 페이지까지 함께
  // 스크롤됐다 — 화면이 커서 밑에서 벗어나면 좌표도 어긋난다.
  useEffect(() => {
    const element = screenRef.current;
    if (!element) return;
    const onWheel = (event: globalThis.WheelEvent) => {
      const point = normalized(event);
      if (!point) return;
      event.preventDefault();
      forward([{ type: "wheel", ...point, deltaX: event.deltaX, deltaY: event.deltaY }]);
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
    // screenRef는 state가 "none"이 아니어야 그려진다 — state가 바뀔 때 다시 건다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forward, state]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (FORWARDED_KEYS.has(event.key)) {
      event.preventDefault();
      forward([{ type: "key", key: event.key, modifiers: modifierBits(event) }]);
      return;
    }
    // 한 글자 키(영문·숫자·기호)는 그대로 보낸다. 한글 조합은 여기로 오지 않으므로
    // 아래 입력칸을 쓴다.
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      forward([{ type: "text", text: event.key }]);
    }
  };

  const sendDraft = () => {
    const text = textDraft;
    if (!text) return;
    setTextDraft("");
    if (onSendText) {
      // 목적지가 지정됐다(2단계 인증 코드 창구 등) — 실패 사유는 같은 빨간 줄로 보인다.
      Promise.resolve(onSendText(text)).catch((error) => {
        console.warn("입력 전송 실패:", error);
        setInputError(error instanceof Error ? error.message : String(error));
      });
      return;
    }
    forward([{ type: "text", text }]);
  };

  return (
    <div className="live-browser" data-channel={channel}>
      <div className="live-browser-head">
        <span className={`live-browser-dot ${state === "streaming" ? "on" : ""}`} aria-hidden="true" />
        <span className="live-browser-label">{label || "발행 화면"}</span>
        <span className="live-browser-state">
          {state === "streaming"
            ? withTyping
              ? "실시간 — 화면을 클릭해 직접 조작할 수 있습니다"
              : "실시간"
            : state === "none"
              ? "지금 중계 중인 화면이 없습니다"
              : "화면에 연결하는 중…"}
        </span>
      </div>

      {inputError && (
        <p className="live-browser-error" role="alert">
          입력 전달 실패: {inputError}
        </p>
      )}

      {state !== "none" && (
        <>
          <div
            ref={screenRef}
            className="live-browser-screen"
            role="application"
            aria-label={`${label || channel} 브라우저 화면`}
            tabIndex={0}
            onPointerDown={onPointerDown}
            onKeyDown={onKeyDown}
            onContextMenu={(event) => event.preventDefault()}
          >
            {frame ? (
              <img
                ref={imgRef}
                src={`data:image/jpeg;base64,${frame.image}`}
                alt="서버에서 실행 중인 브라우저 화면"
                draggable={false}
              />
            ) : (
              <p className="live-browser-empty">첫 화면을 기다리는 중…</p>
            )}
          </div>

          {withTyping && (
          <div className="live-browser-typing">
            <input
              value={textDraft}
              placeholder={typingPlaceholder ?? "여기에 입력하고 Enter — 인증코드·한글 입력용"}
              onChange={(event) => setTextDraft(event.target.value)}
              onKeyDown={(event) => {
                // 한글 조합을 확정하는 Enter도 keydown으로 들어온다 — 조합 중의
                // Enter로 초안을 보내면 글자가 잘린 채 전송된다.
                if (event.nativeEvent.isComposing) return;
                if (event.key === "Enter") {
                  event.preventDefault();
                  sendDraft();
                }
              }}
            />
            <button className="button small" type="button" onClick={sendDraft} disabled={!textDraft}>
              보내기
            </button>
            {actions}
          </div>
          )}
        </>
      )}
    </div>
  );
}
