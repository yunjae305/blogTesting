import { forwardRef } from "react";

import type { PostingChannel } from "../../api/types";
import { SUBJECT_CATEGORIES } from "../../constants";
// 작업 시각 고르개. 새 글 작성과 같은 것을 쓴다(2026-08-12).
import { SchedulePicker } from "../SchedulePicker";
import { PlatformToggleGroup } from "./PlatformToggle";

type Props = {
  index: number;
  value: string;
  placeholder?: string;
  disabled?: boolean;
  /** 이 소재를 어디에 올릴지. 네이버·쓰레드를 함께 고를 수 있다. */
  platforms: PostingChannel[];
  /** 아직 소재를 적지 않은 줄인가. 담아 둘 곳이 없어 고를 수 없다. */
  platformsDisabled?: boolean;
  onPlatformsChange: (platforms: PostingChannel[]) => void;
  /**
   * 이 소재가 어느 분야인가. 빈 문자열은 '고르지 않음'이고, 그때는 예전처럼 모델이
   * 소재 글자만 보고 판단한다. 넘기지 않으면 칸 자체를 그리지 않는다.
   */
  category?: string;
  onCategoryChange?: (category: string) => void;
  /**
   * 이 글의 **작업 시각**(datetime-local 값). 비어 있으면 '앞 글이 발행되면 이어서'다
   * (2026-08-12 사용자 결정). 넘기지 않으면 칸 자체를 그리지 않는다.
   *
   * 발행 시각이 아니라 **일이 시작되는 시각**이다 — 저장할 때 준비 여유를 더해 발행
   * 시각으로 옮긴다(schedule.ts의 workStartInputToPublishIso). 새 글 작성·작업 큐가
   * 쓰는 기준과 같다.
   */
  workStartAt?: string;
  onWorkStartAtChange?: (value: string) => void;
  /** 이 줄의 시각이 잘못됐는가(지난 시각·너무 촘촘함). 붉게 짚어 준다. */
  workStartInvalid?: boolean;
  /** 이 줄을 지운다. 줄이 하나뿐이면 지우지 않는다(호출부가 판단해 넘긴다). */
  onRemove?: () => void;
  onChange: (value: string) => void;
  onEnter: () => void;
  onPaste: (text: string) => boolean;
};

/**
 * 소재 한 줄: [번호] [입력칸] [분야] [네이버] [쓰레드] [삭제].
 *
 * 번호는 값이 아니라 **자리**다. 줄을 지우면 남은 줄이 그대로 1번부터 다시 매겨지도록
 * 여기서는 받은 index만 그린다.
 *
 * 플랫폼 칸은 폭이 고정이다. 켜고 꺼도 줄의 크기가 변하지 않아야 목록이 흔들리지 않는다.
 *
 * 분야 칸은 **선택이다**(2026-08-12). 소재마다 분야가 다른 것이 보통이라 배치 하나로
 * 받지 않고 줄마다 두었고, 비워 두면 예전처럼 모델이 소재 글자만 보고 판단한다.
 */
export const MaterialInputRow = forwardRef<HTMLInputElement, Props>(function MaterialInputRow(
  {
    index,
    value,
    placeholder,
    disabled,
    platforms,
    platformsDisabled,
    onPlatformsChange,
    category,
    onCategoryChange,
    workStartAt,
    onWorkStartAtChange,
    workStartInvalid,
    onRemove,
    onChange,
    onEnter,
    onPaste,
  },
  ref,
) {
  const written = value.trim() !== "";
  // 적어 둔 소재인데 올릴 곳이 없다 — 이 줄이 다음 걸음을 막는 이유다.
  const noPlatform = written && platforms.length === 0;

  return (
    <div
      className={`scheduled-topic-row ${written ? "" : "is-empty"} ${
        noPlatform ? "is-missing-platform" : ""
      }`.trim()}
    >
      <span className="scheduled-topic-index" aria-hidden="true">
        {index + 1}
      </span>
      {/* 시각 칸은 **소재 왼쪽**이다(2026-08-12 사용자 요청). 줄을 왼쪽부터 읽으면
          '언제 · 무엇을 · 어느 분야로 · 어디에'가 된다. */}
      {/* 새 글 작성과 **같은 고르개**를 쓴다(2026-08-12 사용자 지적). 브라우저 기본
          선택창은 분까지 골라도 밖을 눌러야 적용됐다 — 두 화면 다 같은 증상이었다. */}
      {onWorkStartAtChange && (
        <SchedulePicker
          label={`소재 ${index + 1}의 작업 시각`}
          inputClassName={`scheduled-topic-when ${workStartInvalid ? "is-invalid" : ""}`.trim()}
          value={workStartAt ?? ""}
          disabled={disabled || platformsDisabled}
          title={
            platformsDisabled
              ? "소재를 먼저 입력해 주세요."
              : "비워 두면 앞 글이 발행된 뒤 이어서 진행합니다."
          }
          // 비우는 것이 기본값이자 뜻 있는 선택이다 — 되돌릴 길을 창 안에 둔다.
          clearLabel="비우기(앞 글 뒤에 이어서)"
          onChange={onWorkStartAtChange}
        />
      )}
      <input
        ref={ref}
        type="text"
        className="scheduled-topic-line"
        aria-label={`소재 ${index + 1}`}
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        onPaste={(event) => {
          if (onPaste(event.clipboardData.getData("text"))) event.preventDefault();
        }}
        onKeyDown={(event) => {
          // 한글을 조합하는 중의 Enter는 글자를 확정하는 Enter다. 여기서 칸을 옮기면
          // 마지막 글자가 잘린다.
          if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
          // 폼 안에 있어도 Enter로 제출되지 않게 막는다 — 여기서 Enter는 '다음 칸'이다.
          event.preventDefault();
          onEnter();
        }}
      />
      {onCategoryChange && (
        <select
          className="scheduled-topic-category"
          aria-label={`소재 ${index + 1}의 분야`}
          value={category ?? ""}
          // 소재를 적기 전에는 고를 수 없다 — 플랫폼과 같은 이유로, 담아 둘 곳이 없다.
          disabled={disabled || platformsDisabled}
          title={
            platformsDisabled
              ? "소재를 먼저 입력해 주세요."
              : "'오디세이'가 영화인지 게임인지를 가릅니다. 비워 두면 자동으로 판단합니다."
          }
          onChange={(event) => onCategoryChange(event.target.value)}
        >
          <option value="">분야 자동</option>
          {SUBJECT_CATEGORIES.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      )}
      <PlatformToggleGroup
        selected={platforms}
        onChange={onPlatformsChange}
        disabled={disabled || platformsDisabled}
        rowLabel={`소재 ${index + 1}`}
        hint={platformsDisabled ? "소재를 먼저 입력해 주세요." : undefined}
      />
      <button
        type="button"
        className="scheduled-topic-remove"
        // 마지막 한 줄까지 지우면 입력할 곳이 사라진다. 그때는 버튼을 잠근다.
        disabled={disabled || onRemove === undefined}
        aria-label={`소재 ${index + 1} 삭제`}
        title="이 줄을 지웁니다"
        onClick={() => onRemove?.()}
      >
        ×
      </button>
    </div>
  );
});
