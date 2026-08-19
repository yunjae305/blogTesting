import type { ScheduledBatch } from "../../api/types";
import { PauseIcon, PlayIcon, StopIcon } from "./icons";

type Props = {
  batch: ScheduledBatch | null;
  /** 지금 글을 쓰거나 발행하는 중인가. 안내 문구가 이 값을 읽는다. */
  executing: boolean;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  /** 배치를 버리고 처음으로 — 미완료 작업 삭제 + 입력 초기화('새 예약 시작'). */
  onDiscard: () => void;
};

const RUNNING = new Set(["READY", "RUNNING", "PAUSE_REQUESTED", "STOP_REQUESTED"]);
// 일시정지 계열. 이때 '일시정지' 자리의 버튼이 '계속'으로 바뀐다(2026-08-04 사용자 요청) —
// 재개는 같은 배치·같은 큐를 멈춘 지점부터 잇는 것이다.
const PAUSED_LIKE = new Set(["PAUSE_REQUESTED", "PAUSED"]);
const STOPPABLE = new Set([
  "READY",
  "RUNNING",
  "PAUSE_REQUESTED",
  "PAUSED",
  "NEEDS_HUMAN",
  "STOP_REQUESTED",
]);

/**
 * 작업 큐 맨 위의 제어 카드.
 *
 * **시작 버튼은 없다**(2026-08-06 사용자 요청). 예약은 '새 예약 만들기'의 2걸음에서
 * 「예약 포스팅 시작」을 누르는 순간 걸리고, 그 길로 이 화면에 온다 — 같은 시작을 두
 * 곳에 두면 어느 쪽이 지금 값으로 시작하는 것인지 알 수 없다. 여기 남는 것은 **이미
 * 걸린 예약을 손대는 두 가지**다: 멈췄다 잇기, 그리고 통째로 버리고 새로 시작하기.
 */
export function ScheduleControlCard({
  batch,
  executing,
  busy,
  onPause,
  onResume,
  onDiscard,
}: Props) {
  const status = batch?.status ?? null;
  const running = status !== null && RUNNING.has(status);
  const paused = status !== null && PAUSED_LIKE.has(status);
  // 인증 대기는 '계속'이 아니라 '예약 재개'다 — 사용자가 브라우저에서 인증을 마친 뒤
  // 눌러야 하는 버튼이라, 일시정지의 되돌림과는 성격이 다르다. 시작 버튼이 없어진 뒤로는
  // 이 자리(일시정지 자리)가 그 일을 맡는다.
  const resumable = status === "NEEDS_HUMAN";
  const stoppable = status !== null && STOPPABLE.has(status);

  return (
    <section className="panel scheduled-panel" aria-labelledby="scheduled-control-title">
      <div className="panel-header">
        <h2 className="panel-title" id="scheduled-control-title">
          <span className="scheduled-panel-icon" aria-hidden="true">
            <PlayIcon />
          </span>
          스케줄 시작
        </h2>
      </div>
      <div className="panel-body">
        <div className="scheduled-controls">
          {paused || resumable ? (
            // 멈춘 예약을 잇는 자리. 큐는 멈춘 지점부터 이어지고(서버 resume — 같은
            // 배치·작업·원고), 다시 돌기 시작하면 버튼은 '일시정지'로 돌아온다.
            <button
              className="button scheduled-control pause"
              type="button"
              disabled={busy}
              onClick={onResume}
            >
              <PlayIcon />
              {resumable ? "예약 재개" : "계속"}
            </button>
          ) : (
            <button
              className="button scheduled-control pause"
              type="button"
              disabled={busy || (status !== "RUNNING" && status !== "READY")}
              onClick={onPause}
            >
              <PauseIcon />
              일시정지
            </button>
          )}
          {/* 예전의 '정지'. 이제 멈추기만 하는 게 아니라 미완료 작업을 지우고 입력까지
              초기화해 정말 처음부터 다시 시작하게 한다(2026-08-04 사용자 결정). */}
          <button
            className="button scheduled-control stop"
            type="button"
            disabled={busy || !stoppable}
            onClick={onDiscard}
          >
            <StopIcon />
            새 예약 시작
          </button>
        </div>
        {/* 손댈 예약이 없으면 두 버튼이 모두 잠긴다. 왜 잠겼는지와 어디서 시작하는지를
            함께 적는다 — 회색 버튼만 두면 고장으로 읽힌다. */}
        {!batch && (
          <p className="scheduled-control-note">
            진행 중인 예약이 없습니다. 「새 예약 만들기」에서 소재와 일정을 정하고 시작해 주세요.
          </p>
        )}
        {executing && (
          <p className="scheduled-control-note">글을 쓰거나 발행하는 중입니다.</p>
        )}
        {running && !executing && (
          <p className="scheduled-control-note">다음 발행을 기다리는 중입니다.</p>
        )}
        {paused && (
          <p className="scheduled-control-note">
            일시정지되었습니다. 「계속」을 누르면 남은 작업이 멈춘 지점부터 이어집니다.
          </p>
        )}
        {resumable && (
          <p className="scheduled-control-note">
            인증이 필요해 멈춰 있습니다. 인증을 마친 뒤 「예약 재개」를 눌러 주세요.
          </p>
        )}
      </div>
    </section>
  );
}
