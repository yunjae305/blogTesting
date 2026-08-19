import { useEffect, useState } from "react";

import { fetchLiveSessions, type LiveSessionInfo } from "../api/live";
import { LiveBrowserView } from "./LiveBrowserView";

/**
 * 지금 이 사용자 몫으로 중계 중인 발행 크롬 화면들을 전부 보여 준다.
 *
 * 발행 화면(StepPublish)·예약 화면에 늘 놓여 있고, 중계할 것이 없으면 아무것도
 * 그리지 않는다 — 발행이 시작돼 서버에 크롬이 뜨는 순간 저절로 나타난다.
 * 채널을 넘기면 그 채널만 본다(설정의 로그인 카드가 쓴다).
 */
export function LiveSessionsPanel({
  channels,
  kinds,
  withTyping = false,
}: {
  channels?: string[];
  /**
   * 어떤 종류의 중계만 보일지 (login | publish | preview). 발행·예약·자동 포스팅
   * 탭은 ["publish"]만 본다 — 안 거르면 설정에서 한 로그인의 중계가 발행 탭에
   * 뜬다(2026-08-18 사용자 지적).
   */
  kinds?: string[];
  /** 텍스트 전송칸을 붙일지. 로그인 카드만 켠다 — 발행 중계는 지켜보는 화면이다. */
  withTyping?: boolean;
}) {
  const [sessions, setSessions] = useState<LiveSessionInfo[]>([]);

  useEffect(() => {
    let closed = false;
    const poll = async () => {
      try {
        const found = await fetchLiveSessions();
        if (!closed) setSessions(found);
      } catch {
        // 목록을 한 번 못 읽는 것은 오류로 보여 줄 일이 아니다 — 다음 주기에 다시 본다.
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 4000);
    return () => {
      closed = true;
      window.clearInterval(timer);
    };
  }, []);

  const visible = sessions.filter(
    (session) =>
      session.active &&
      (!channels || channels.includes(session.channel)) &&
      (!kinds || kinds.includes(session.kind)),
  );
  if (visible.length === 0) return null;

  return (
    <div className="live-sessions">
      {visible.map((session) => (
        <LiveBrowserView
          key={session.channel}
          channel={session.channel}
          label={session.label}
          withTyping={withTyping}
        />
      ))}
    </div>
  );
}
