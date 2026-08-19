/**
 * 예약 시각을 화면 값과 저장 값 사이에서 옮기는 함수들.
 *
 * 규칙은 하나다: **화면은 사용자의 로컬 시간, 저장은 절대 시각.**
 *
 * `<input type="datetime-local">`은 시간대가 없는 "2026-08-06T15:00"을 준다. 그 문자열을
 * `new Date(...)`에 넣으면 브라우저가 **로컬 시간으로** 읽고, `toISOString()`이 그것을
 * UTC로 옮긴다 — 그래서 서버에는 언제나 한 가지 기준의 절대 시각만 저장된다. 반대 방향도
 * 같은 원리로 되돌린다. 서버는 시간대 변환을 하지 않으므로, 변환이 일어나는 곳은 이 파일뿐이고
 * 날짜가 하루 밀리는 종류의 버그도 여기서만 생길 수 있다.
 */

/** 브라우저가 보고 있는 시간대(IANA). 서버에는 표시·감사용으로만 보낸다. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** Date → `<input type="datetime-local">`이 읽는 로컬 문자열. */
export function toLocalInputValue(date: Date): string {
  if (Number.isNaN(date.getTime())) return "";
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** 저장된 절대 시각(ISO) → 입력칸의 로컬 문자열. 값이 없거나 깨졌으면 빈 문자열. */
export function isoToLocalInput(iso: string | undefined | null): string {
  if (!iso) return "";
  return toLocalInputValue(new Date(iso));
}

/**
 * 입력칸의 로컬 문자열 → 절대 시각(UTC ISO). 읽을 수 없으면 null.
 *
 * null을 돌려주는 것이 중요하다. 반쯤 입력된 값을 임의로 채워 보내면 사용자가 고르지
 * 않은 시각에 글이 올라간다 — 호출하는 쪽이 그때는 보내지 않는다.
 */
export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

/** 지금부터 `minutes`분 뒤를 입력칸 값으로. 처음 채워 두는 기본값에 쓴다. */
export function localInputAfterMinutes(minutes: number, now: Date = new Date()): string {
  const target = new Date(now.getTime() + minutes * 60_000);
  // 초 단위는 입력칸에 없다. 분 아래를 버려 두면 '지금보다 뒤'가 확실해진다.
  target.setSeconds(0, 0);
  return toLocalInputValue(target);
}

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

/**
 * 원고 **작업이 시작되는** 시각.
 *
 * **저장된 값이 곧 작업 시각이다** — 더하거나 빼지 않는다(2026-08-12 사용자 지시:
 * "그 20분 차이 굳이 없어도 되는거잖아. 아까 내가 지우라고 했을텐데?").
 *
 * 예전에는 저장값이 '발행 시각'이라, 워커가 그보다 준비 여유(20분) 앞서 원고를 만들었고
 * 화면은 그 여유를 빼서 그렸다. 서버에서 여유를 없앤 뒤에도 **화면의 보정만 남아 있었다** —
 * 그래서 오후 1시 21분에 건 작업이 큐에 '오후 1:01'로 찍혔다(2026-08-12 신고).
 */
export function formatWorkStartAt(iso: string | undefined | null): string {
  return formatPublishAt(iso);
}

/** 화면이 받은 **작업 시각**(로컬 입력값) → 저장할 시각(UTC ISO). 그대로 옮긴다. */
export function workStartInputToPublishIso(localValue: string): string | null {
  return localInputToIso(localValue);
}

/** 저장된 시각 → 입력칸이 보여 줄 **작업 시각**. 위 변환의 반대이며, 역시 그대로다. */
export function publishIsoToWorkStartInput(iso: string | undefined | null): string {
  return isoToLocalInput(iso);
}

/** 예약 시각을 사람이 읽는 로컬 시간 문구로. "8월 6일(목) 오후 3:00" */
export function formatPublishAt(iso: string | undefined | null): string {
  if (!iso) return "시각 미정";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "시각 미정";
  const hours = date.getHours();
  const meridiem = hours < 12 ? "오전" : "오후";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return (
    `${date.getMonth() + 1}월 ${date.getDate()}일(${WEEKDAYS[date.getDay()]}) ` +
    `${meridiem} ${hour12}:${pad(date.getMinutes())}`
  );
}

/** 그 시각이 이미 지났는가. 저장 전에 화면에서 먼저 잡아 준다(서버도 다시 본다). */
export function isPast(value: string, now: Date = new Date()): boolean {
  const iso = localInputToIso(value);
  if (iso === null) return false;
  return new Date(iso).getTime() <= now.getTime();
}

/**
 * 글과 글 사이의 최소 발행 간격(분).
 *
 * **서버의 `MIN_PUBLISH_GAP_SECONDS`와 같은 값이어야 한다**(validation.py). 어긋나면
 * 화면은 통과시키고 서버가 거부하는, 사용자가 이유를 알 수 없는 상태가 된다.
 */
export const MIN_PUBLISH_GAP_MINUTES = 12;

/**
 * 예약 시각들이 서로 `MIN_PUBLISH_GAP_MINUTES`만큼 떨어져 있는가.
 *
 * 어긴 칸의 **번호(0부터)** 를 돌려주고, 아무 문제 없으면 -1이다. 읽을 수 없는 칸은
 * 건너뛴다 — 그 칸은 이미 다른 검사가 짚고 있다.
 *
 * 이웃은 **시각 순**으로 본다(입력 순서가 아니라). 3시·1시 순으로 입력해도 실제로
 * 올라가는 순서는 1시·3시이고, 간격은 그 순서에서 재야 뜻이 있다. 다만 사용자에게
 * 짚어 줄 때는 **뒤에 오는 쪽의 입력 칸**을 가리킨다 — 고쳐야 할 칸이 그쪽이다.
 */
export function tooCloseIndex(values: string[]): number {
  const points = values
    .map((value, index) => ({ index, iso: localInputToIso(value) }))
    .filter((item): item is { index: number; iso: string } => item.iso !== null)
    .map((item) => ({ index: item.index, at: new Date(item.iso).getTime() }))
    .sort((a, b) => a.at - b.at);
  const gap = MIN_PUBLISH_GAP_MINUTES * 60 * 1000;
  for (let i = 1; i < points.length; i += 1) {
    if (points[i].at - points[i - 1].at < gap) return points[i].index;
  }
  return -1;
}
