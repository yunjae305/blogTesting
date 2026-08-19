/**
 * 「작업 관리」 화면의 머리. 무엇을 하는 화면인지만 말한다.
 *
 * 걸음 표시(1 소재 → 2 발행 방식 → 3 작업 큐)는 없앴다(2026-08-11). 입력하는 걸음이
 * 사라져 걸어갈 곳이 없다 — 이 화면에는 작업 큐와 발행 내역만 있다.
 */
export function ReservationHeader() {
  return (
    <header className="reservation-header">
      <div className="reservation-header-copy">
        {/* 이 화면의 표식(노란 포스트잇). 글자가 아니라 자리를 알려 주는 것이라
            읽어 주지 않는다. */}
        <span className="reservation-mark" aria-hidden="true" />
        <div>
          <h1 className="reservation-title">예약작업 관리</h1>
          <p className="reservation-subtitle">
            걸어 둔 작업의 진행 상황과 발행 결과를 봅니다.
          </p>
        </div>
      </div>
    </header>
  );
}
