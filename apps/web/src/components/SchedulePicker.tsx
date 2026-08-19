/**
 * 「언제, 몇 편, 어디에」의 작업 시각 고르개 — 달력과 시·분·오전/오후를 **우리가 그린다**
 * (2026-08-12 사용자 신고).
 *
 * 증상: 시간을 분까지 고르고 나도 값이 들어오지 않아, 선택창 밖을 한 번 더 눌러야
 * 적용됐다.
 *
 * 원인: 여태 `<input type="datetime-local">`의 브라우저 기본 선택창(showPicker)을
 * 그대로 썼다. 그 창은 브라우저가 그리는 것이라 **열 순서도, 언제 닫을지도 우리가 정할
 * 수 없다** — 닫는 API 자체가 없고(showPicker의 짝이 없다), 값이 언제 확정되는지도
 * 브라우저마다 다르다. CSS로도 손이 닿지 않는다.
 *
 * 그래서 같은 모양(달력 + 시각 세 열)을 직접 그리고, 세 가지를 우리가 정한다.
 *
 *   1. **왼쪽부터 오전/오후 → 시 → 분 순이다**(2026-08-12 사용자 지시). 말하는 차례와
 *      같고, 왼쪽에서 오른쪽으로 눌러 가면 마지막에 분이 온다.
 *   2. **누르는 즉시 적용한다.** 날짜든 시든 분이든 한 번 누를 때마다 값이 곧바로 위
 *      칸에 들어간다. 밖을 눌러 확정하는 단계가 없다.
 *   3. **분을 고르면 창이 닫힌다.** 분이 시각의 마지막 자리라, 거기까지 골랐으면 할 일이
 *      끝난 것이다. 오전/오후·시·날짜는 닫지 않는다 — 아직 고를 것이 남아 있다.
 *
 * 세 열은 아무 순서로 눌러도 된다. 다만 **닫는 것은 분 하나뿐이다**. 그래야
 * "닫혔다 = 다 골랐다"가 어긋나지 않는다.
 *
 * 입력 칸 자체는 그대로 `datetime-local`이다. 키보드로 연·월·일·시각을 직접 치던 것,
 * 화면 낭독기가 읽는 것, 지역 표기(오전/오후)가 그대로 유지된다.
 */

import { useEffect, useId, useRef, useState } from "react";

/** 시각 한 벌. `datetime-local`이 다루는 만큼만 — 초·시간대는 여기 없다. */
type Parts = { year: number; month: number; day: number; hour: number; minute: number };

type Meridiem = "am" | "pm";

/** 요일 머리글. 일요일부터다(Date.getDay()와 같은 순서). */
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

const HOURS_12 = Array.from({ length: 12 }, (_, index) => index + 1);
const MINUTES = Array.from({ length: 60 }, (_, index) => index);

/**
 * 값이 없을 때 선택창이 처음 가리키는 곳 = 지금부터 이만큼 뒤.
 *
 * 0으로 두면 '지금'이 기본이 되는데, 지금은 이미 지난 시각이라 그 칸이 눌리지 않는다.
 * 열자마자 못 누르는 칸이 강조돼 있으면 고장으로 보인다.
 */
const SEED_LEAD_MINUTES = 10;

const VALUE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/;

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function partsToDate(parts: Parts): Date {
  return new Date(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, 0, 0);
}

function partsOfDate(at: Date): Parts {
  return {
    year: at.getFullYear(),
    month: at.getMonth() + 1,
    day: at.getDate(),
    hour: at.getHours(),
    minute: at.getMinutes(),
  };
}

/** `datetime-local` 값("2026-08-13T15:00") → 시각 한 벌. 형식이 아니면 null. */
function parseLocalParts(value: string | null | undefined): Parts | null {
  const match = VALUE_PATTERN.exec((value || "").trim());
  if (!match) return null;
  const parts: Parts = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
  };
  // "2026-02-31"은 Date가 3월 3일로 넘겨 버린다. 되돌려 보고 달라지면 시각이 아니다.
  const at = partsToDate(parts);
  if (at.getMonth() + 1 !== parts.month || at.getDate() !== parts.day) return null;
  if (parts.hour > 23 || parts.minute > 59) return null;
  return parts;
}

/** 시각 한 벌 → `datetime-local`이 읽는 값. */
function formatLocalParts(parts: Parts): string {
  return `${parts.year}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(parts.minute)}`;
}

function meridiemOf(hour: number): Meridiem {
  return hour < 12 ? "am" : "pm";
}

function hour12Of(hour: number): number {
  return hour % 12 === 0 ? 12 : hour % 12;
}

function to24(hour12: number, meridiem: Meridiem): number {
  const base = hour12 % 12;
  return meridiem === "am" ? base : base + 12;
}

type Props = {
  /** 지금 고른 시각(`datetime-local` 값). 비어 있으면 예약 없음이다. */
  value: string;
  /**
   * 고를 수 있는 가장 이른 시각. 이보다 이른 칸은 아예 눌리지 않는다.
   * 넘기지 않으면 '지금'이다.
   */
  min?: string;
  /** 입력 칸을 읽어 주는 이름. */
  label: string;
  onChange: (next: string) => void;
  /** 선택창을 열 때 한 번. 부모가 '지금'을 다시 재는 자리다. */
  onOpen?: () => void;
  disabled?: boolean;
  /** 입력 칸에 얹을 이름. 화면마다 칸의 크기가 달라서 밖에서 준다. */
  inputClassName?: string;
  title?: string;
  /**
   * 비우기 단추에 적을 말. 넘기면 선택창 아래에 그 단추가 붙는다.
   *
   * 비우는 것이 **뜻 있는 선택**인 화면에서만 쓴다(자동 포스팅의 "앞 글 뒤에 이어서").
   * 새 글 작성은 되돌리는 단추가 칸 옆에 따로 있어 넘기지 않는다.
   */
  clearLabel?: string;
};

export function SchedulePicker({
  value,
  min,
  label,
  onChange,
  onOpen,
  disabled,
  inputClassName,
  title,
  clearLabel,
}: Props) {
  const [open, setOpen] = useState(false);
  // 자동 포스팅은 이 고르개를 줄마다 하나씩 그린다 — id를 글자로 박아 두면 겹친다.
  const uid = useId();
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const hourListRef = useRef<HTMLDivElement>(null);
  const minuteListRef = useRef<HTMLDivElement>(null);

  const floor = parseLocalParts(min) ?? partsOfDate(new Date());
  const floorAt = partsToDate(floor).getTime();

  /**
   * 아직 아무것도 고르지 않았을 때 선택창이 가리키는 곳. 값을 고르면 `value`가 이긴다 —
   * 위 칸에 직접 타이핑한 것도 곧바로 선택창에 비쳐야 한다.
   */
  const [draft, setDraft] = useState<Parts>(() => parseLocalParts(value) ?? seed());
  const chosen = parseLocalParts(value);
  const current = chosen ?? draft;

  const [view, setView] = useState(() => ({ year: current.year, month: current.month }));

  function seed(): Parts {
    const base = parseLocalParts(min) ?? partsOfDate(new Date());
    return partsOfDate(new Date(partsToDate(base).getTime() + SEED_LEAD_MINUTES * 60_000));
  }

  const openPanel = () => {
    onOpen?.();
    const base = parseLocalParts(value) ?? seed();
    setDraft(base);
    setView({ year: base.year, month: base.month });
    setOpen(true);
  };

  const closePanel = (focusInput: boolean) => {
    setOpen(false);
    if (focusInput) inputRef.current?.focus();
  };

  /**
   * 고른 것을 곧바로 위 칸에 올린다. 이것이 이 컴포넌트를 만든 이유다 — 밖을 눌러
   * 확정하는 단계가 없다.
   */
  const commit = (next: Parts) => {
    const lifted = liftAboveFloor(next);
    setDraft(lifted);
    onChange(formatLocalParts(lifted));
  };

  /**
   * 지난 시각이 되어 버린 선택을 **고를 수 있는 가장 이른 시각**으로 밀어 준다.
   *
   * 지난 칸은 눌리지 않게 막아 두었는데도 이 일이 생긴다 — 막는 기준은 '그 칸이 통째로
   * 지났는가'이지, 나머지 칸과 합쳐 본 결과가 아니기 때문이다. 지금이 9시 30분인데
   * 오후 2시 10분에서 '9시'를 고르면 9시 10분, 14시 10분에서 '오전'을 고르면 2시 10분 —
   * 둘 다 지났다. 그렇다고 '오전 9시'나 '오전'을 막으면 왜 안 눌리는지 알 수 없다.
   *
   * 그래서 누르게 두고 값을 민다. 미는 곳이 언제나 가장 이른 시각 바로 뒤라서, 고른
   * 오전/오후와 시가 그 안에 들어 있으면 그대로 남는다(9시를 골랐으면 9시 31분).
   */
  function liftAboveFloor(parts: Parts): Parts {
    if (partsToDate(parts).getTime() > floorAt) return parts;
    // 지난 날은 아예 눌리지 않으므로, 여기 오는 것은 언제나 가장 이른 시각과 같은 날이다.
    return partsOfDate(new Date(floorAt + 60_000));
  }

  /** 이 순간이 고를 수 있는 범위 밖인가. */
  const isPast = (parts: Parts): boolean => partsToDate(parts).getTime() <= floorAt;

  const meridiem = meridiemOf(current.hour);

  const monthShift = (delta: number) => {
    const at = new Date(view.year, view.month - 1 + delta, 1);
    setView({ year: at.getFullYear(), month: at.getMonth() + 1 });
  };

  // 지난 달로는 갈 이유가 없다. 그 달의 마지막 순간까지 이미 지났으면 막는다.
  const prevMonthBlocked = new Date(view.year, view.month - 1, 0, 23, 59).getTime() <= floorAt;

  const leadingBlanks = new Date(view.year, view.month - 1, 1).getDay();
  const daysInMonth = new Date(view.year, view.month, 0).getDate();

  // 밖을 누르면 닫는다. 값은 이미 올라가 있으므로 닫는 것만으로 잃는 것이 없다.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  /**
   * 열었을 때 지금 고른 시·분이 열 가운데 오도록 굴려 둔다. 60개짜리 분 목록이 0분부터
   * 보이면 고른 값이 어디 있는지 찾아야 한다.
   *
   * `scrollIntoView` 대신 `scrollTop`을 직접 준다 — 그 함수는 페이지까지 함께 굴려서
   * 화면이 튄다.
   */
  useEffect(() => {
    if (!open) return;
    for (const list of [hourListRef.current, minuteListRef.current]) {
      const picked = list?.querySelector<HTMLElement>(".is-picked");
      if (!list || !picked) continue;
      list.scrollTop = picked.offsetTop - (list.clientHeight - picked.clientHeight) / 2;
    }
  }, [open]);

  return (
    <div
      className="when-picker"
      ref={wrapRef}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          closePanel(true);
        }
      }}
    >
      <input
        ref={inputRef}
        type="datetime-local"
        className={inputClassName}
        aria-label={label}
        aria-expanded={open}
        value={value}
        min={min}
        disabled={disabled}
        title={title}
        // 위 칸에 직접 치는 것도 그대로 둔다 — 키보드만 쓰는 사람에게는 이쪽이 빠르다.
        onChange={(event) => onChange(event.target.value)}
        // 연도·월·일·시각 어느 칸을 눌러도 선택창이 열린다(2026-08-11부터의 동작).
        onClick={() => {
          if (!open) openPanel();
        }}
      />
      <button
        type="button"
        className="when-picker-toggle"
        aria-label={`${label} 선택창 ${open ? "닫기" : "열기"}`}
        aria-expanded={open}
        disabled={disabled}
        onClick={() => (open ? closePanel(true) : openPanel())}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" />
          <path d="M3.5 9.5h17M8 3.5v3M16 3.5v3" />
        </svg>
      </button>

      {open && (
        <div className="when-picker-panel" role="dialog" aria-label={`${label} 선택`}>
          <div className="when-picker-calendar">
            <div className="when-picker-month">
              <button
                type="button"
                aria-label="이전 달"
                disabled={prevMonthBlocked}
                onClick={() => monthShift(-1)}
              >
                ‹
              </button>
              <strong aria-live="polite">
                {view.year}년 {view.month}월
              </strong>
              <button type="button" aria-label="다음 달" onClick={() => monthShift(1)}>
                ›
              </button>
            </div>
            <div className="when-picker-weekdays" aria-hidden="true">
              {WEEKDAYS.map((name) => (
                <span key={name}>{name}</span>
              ))}
            </div>
            <div className="when-picker-days" role="group" aria-label="날짜">
              {Array.from({ length: leadingBlanks }, (_, index) => (
                <span key={`blank-${index}`} />
              ))}
              {Array.from({ length: daysInMonth }, (_, index) => index + 1).map((day) => {
                const parts: Parts = { ...current, year: view.year, month: view.month, day };
                // 그 날이 통째로 지났는가. 시각은 아직 안 골랐을 수 있으므로 끝 시각으로 잰다.
                const blocked = isPast({ ...parts, hour: 23, minute: 59 });
                const picked =
                  current.year === view.year && current.month === view.month && current.day === day;
                return (
                  <button
                    key={day}
                    type="button"
                    className={picked ? "is-picked" : undefined}
                    aria-label={`${view.year}년 ${view.month}월 ${day}일`}
                    aria-pressed={Boolean(chosen) && picked}
                    disabled={blocked}
                    // 날짜는 닫지 않는다 — 시각이 아직 남아 있다.
                    onClick={() => commit(parts)}
                  >
                    {day}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="when-picker-clock">
            <div className="when-picker-column">
              <p className="when-picker-column-title" id={`${uid}-meridiem`}>
                오전·오후
              </p>
              <div
                className="when-picker-options is-short"
                role="group"
                aria-labelledby={`${uid}-meridiem`}
              >
                {(["am", "pm"] as Meridiem[]).map((option) => {
                  const parts: Parts = { ...current, hour: to24(hour12Of(current.hour), option) };
                  // 오전(0~11시)·오후(12~23시)가 통째로 지났는가.
                  const blocked = isPast({ ...parts, hour: option === "am" ? 11 : 23, minute: 59 });
                  const picked = meridiem === option;
                  return (
                    <button
                      key={option}
                      type="button"
                      className={picked ? "is-picked" : undefined}
                      aria-pressed={Boolean(chosen) && picked}
                      disabled={blocked}
                      onClick={() => commit(parts)}
                    >
                      {option === "am" ? "오전" : "오후"}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="when-picker-column">
              <p className="when-picker-column-title" id={`${uid}-hour`}>
                시
              </p>
              <div className="when-picker-options" ref={hourListRef} role="group" aria-labelledby={`${uid}-hour`}>
                {HOURS_12.map((hour) => {
                  const parts: Parts = { ...current, hour: to24(hour, meridiem) };
                  // 그 시간대가 통째로 지났을 때만 막는다(59분까지 봐서).
                  const blocked = isPast({ ...parts, minute: 59 });
                  const picked = hour12Of(current.hour) === hour;
                  return (
                    <button
                      key={hour}
                      type="button"
                      className={picked ? "is-picked" : undefined}
                      aria-label={`${hour}시`}
                      aria-pressed={Boolean(chosen) && picked}
                      disabled={blocked}
                      onClick={() => commit(parts)}
                    >
                      {pad(hour)}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="when-picker-column">
              <p className="when-picker-column-title" id={`${uid}-minute`}>
                분
              </p>
              <div
                className="when-picker-options"
                ref={minuteListRef}
                role="group"
                aria-labelledby={`${uid}-minute`}
              >
                {MINUTES.map((minute) => {
                  const parts: Parts = { ...current, minute };
                  const blocked = isPast(parts);
                  const picked = current.minute === minute;
                  return (
                    <button
                      key={minute}
                      type="button"
                      className={picked ? "is-picked" : undefined}
                      aria-label={`${minute}분`}
                      aria-pressed={Boolean(chosen) && picked}
                      disabled={blocked}
                      // **분에서만 닫는다.** 분이 시각의 마지막 자리다.
                      onClick={() => {
                        commit(parts);
                        closePanel(true);
                      }}
                    >
                      {pad(minute)}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          {/* 비우는 것이 뜻 있는 선택인 화면에서만 붙는다. 키보드로 지울 수는 늘 있지만,
              마우스만 쓰는 사람에게는 되돌릴 길이 보이지 않았다. */}
          {clearLabel && (
            <div className="when-picker-foot">
              <button
                type="button"
                disabled={!value}
                onClick={() => {
                  onChange("");
                  closePanel(true);
                }}
              >
                {clearLabel}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
