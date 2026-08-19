import { useEffect, useRef, useState } from "react";

import type { PostingChannel } from "../../api/types";
import { PlusIcon } from "./icons";
import { MaterialInputRow } from "./MaterialInputRow";
import { normalizeTopics } from "./topics";

/**
 * 처음 보여 주는 입력칸 수. 모자라면 Enter나 '소재 추가'로 늘어난다.
 *
 * 3이다(2026-08-05 사용자 요청). 넷을 깔아 두면 빈 칸 하나가 늘 남아 화면이 '아직 덜
 * 채운 것'처럼 보였다 — 칸의 수는 권유가 아니라 자리일 뿐이다.
 */
const MIN_ROWS = 3;

const ROW_PLACEHOLDER = "소재를 입력하고 Enter를 눌러주세요";

type Props = {
  /** 줄바꿈으로 이어 붙인 소재 목록. 바깥으로 오가는 형식은 예전 그대로다. */
  value: string;
  onChange: (value: string) => void;
  /** 한 번에 예약할 수 있는 소재 수. 그 위로는 줄을 늘리지 않는다. */
  maxRows: number;
  /** 소재 줄과 짝을 이루는 플랫폼 선택. */
  platformsList: PostingChannel[][];
  onPlatformsChangeAt: (index: number, platforms: PostingChannel[]) => void;
  /**
   * 소재 줄과 짝을 이루는 **소재 분야**(2026-08-12). 플랫폼과 같은 순서 규칙이다.
   * 넘기지 않으면 분야 칸을 그리지 않는다.
   */
  categories?: string[];
  onCategoryChangeAt?: (index: number, category: string) => void;
  /**
   * 소재 줄과 짝을 이루는 **작업 시각**(2026-08-12). 빈 값은 '앞 글이 끝나면'이다.
   * 넘기지 않으면 시각 칸을 그리지 않는다.
   */
  workStartTimes?: string[];
  onWorkStartAtChangeAt?: (index: number, value: string) => void;
  /** 시각이 잘못된 소재 번호(0부터). 그 줄만 붉게 짚는다. -1이면 없다. */
  invalidTimeIndex?: number;
  disabled?: boolean;
};

/**
 * 소재별 한 편 — 번호가 붙은 줄 입력칸.
 *
 * 칸 목록을 따로 들고 있지 않고 `value`를 줄바꿈으로 나눠 그때그때 만든다. 서버가 준
 * 소재 목록(작업을 지우면 줄어든다)이 바로 칸에 반영되어야 하기 때문이다.
 */
export function MaterialInputList({
  value,
  onChange,
  maxRows,
  platformsList,
  onPlatformsChangeAt,
  categories,
  onCategoryChangeAt,
  workStartTimes,
  onWorkStartAtChangeAt,
  invalidTimeIndex = -1,
  disabled,
}: Props) {
  const inputs = useRef<(HTMLInputElement | null)[]>([]);
  // 칸을 새로 늘리면서 옮길 때는 아직 그 input이 없다. 그려진 뒤에 옮긴다.
  const [focusRow, setFocusRow] = useState<number | null>(null);

  const lines = value.split("\n");
  const rowCount = Math.min(maxRows, Math.max(MIN_ROWS, lines.length));
  const rows = Array.from({ length: rowCount }, (_, index) => lines[index] ?? "");
  // placeholder는 아직 비어 있는 **첫 칸**에만 둔다. 빈 칸마다 같은 문구가 반복되면
  // 어디까지 적었는지가 오히려 보이지 않는다.
  const firstEmpty = rows.findIndex((row) => row.trim() === "");

  /**
   * 화면의 줄 번호 → 저장된 소재 순서.
   *
   * 두 번호는 **같지 않다.** 화면에는 빈 줄도 있고 같은 소재를 두 번 적을 수도 있지만,
   * 플랫폼 선택은 빈 줄과 중복을 뺀 소재 순서로 저장된다(`normalizeTopics` — 서버가 세는
   * 방법과 같다). 이 둘을 그대로 맞바꾸던 것이, 가운데 줄을 비우면 플랫폼 선택이 엉뚱한
   * 소재에 붙던 원인이다(2026-08-05).
   *
   * 아직 아무것도 적지 않은 줄에는 짝이 되는 소재가 없다(-1). 그런 줄에서는 고를 수 없다 —
   * 골라 봐야 담아 둘 곳이 없기 때문이다.
   */
  const stored = normalizeTopics(value);
  const topicIndexOf = (row: number) => {
    const text = rows[row]?.trim() ?? "";
    return text ? stored.indexOf(text) : -1;
  };

  useEffect(() => {
    if (focusRow === null) return;
    inputs.current[focusRow]?.focus();
    setFocusRow(null);
  }, [focusRow]);

  const write = (next: string[]) => onChange(next.join("\n"));

  const change = (index: number, text: string) => {
    const next = [...rows];
    next[index] = text;
    write(next);
  };

  const enter = (index: number) => {
    if (index < rowCount - 1) {
      setFocusRow(index + 1);
      return;
    }
    // 마지막 칸이다. 적은 것이 있을 때만 칸을 하나 늘린다 — 빈 칸에서 Enter를 눌러도
    // 늘어나면 빈 칸만 끝없이 쌓인다.
    if (rows[index].trim() === "" || rowCount >= maxRows) return;
    write([...rows, ""]);
    setFocusRow(index + 1);
  };

  /**
   * 줄 하나를 뺀다. 번호는 남은 줄이 그대로 이어받는다(자리가 곧 번호다).
   *
   * 마지막 한 줄은 지우지 않는다 — 입력할 곳이 없는 화면이 되기 때문이다. 대신 그 줄의
   * 내용만 비운다.
   */
  const remove = (index: number) => {
    const next = rows.filter((_, position) => position !== index);
    write(next.length > 0 ? next : [""]);
  };

  /**
   * 여러 줄을 한꺼번에 붙여 넣으면 줄 수만큼 칸에 나눠 담는다.
   *
   * 예전 입력칸은 textarea 하나여서 목록을 통째로 붙여 넣을 수 있었다. 칸을 나눈 뒤에도
   * 그 방법이 그대로 되어야 한다 — 안 그러면 열 줄이 한 소재로 뭉친다.
   */
  const paste = (index: number, text: string) => {
    const pasted = text.split(/\r?\n/);
    if (pasted.length < 2) return false;
    const next = [...rows];
    while (next.length < index + pasted.length) next.push("");
    for (let i = 0; i < pasted.length; i += 1) next[index + i] = pasted[i];
    write(next.slice(0, maxRows));
    setFocusRow(Math.min(index + pasted.length - 1, maxRows - 1));
    return true;
  };

  return (
    <>
      <div className="scheduled-topic-lead">
        {/* 위 문단은 줄이 무엇으로 이루어지는지를 말하고, 여기서는 **지금 할 일**만
            말한다. 예전에는 둘 다 "소재를 입력해 주세요"로 시작해 같은 지시가 두 번
            읽혔다(2026-08-12 사용자 지적). */}
        <strong id="scheduled-topic-lead">소재를 한 줄에 하나씩 적어 주세요.</strong>
        {/* 칸을 늘리는 길이 둘이다. Enter만 적어 두면 마우스를 쓰는 사람은 아래
            '소재 추가' 버튼을 따로 찾아야 한다(2026-08-05 사용자 요청). */}
        <span className="scheduled-topic-tip">
          Enter를 누르거나 소재 추가를 클릭하면 다음 칸이 생성됩니다
        </span>
      </div>
      <div className="scheduled-topic-rows" role="group" aria-labelledby="scheduled-topic-lead">
        {rows.map((row, index) => {
          const topicIndex = topicIndexOf(index);
          return (
            <MaterialInputRow
              // 칸의 자리(번호)가 곧 정체성이다. 값으로 키를 잡으면 같은 소재를 두 번
              // 적었을 때 키가 겹친다.
              key={index}
              ref={(element) => {
                inputs.current[index] = element;
              }}
              index={index}
              value={row}
              placeholder={index === firstEmpty ? ROW_PLACEHOLDER : undefined}
              disabled={disabled}
              // 읽을 때도 고칠 때도 **소재 순서**로 본다. 줄 번호로 보면 빈 줄 하나에
              // 목록 전체가 한 칸씩 밀린다.
              platforms={topicIndex >= 0 ? (platformsList[topicIndex] ?? []) : []}
              platformsDisabled={topicIndex < 0}
              onPlatformsChange={(platforms) => {
                if (topicIndex >= 0) onPlatformsChangeAt(topicIndex, platforms);
              }}
              // 분야도 **소재 순서**로 읽고 쓴다 — 플랫폼과 같은 이유다.
              category={topicIndex >= 0 ? (categories?.[topicIndex] ?? "") : ""}
              onCategoryChange={
                onCategoryChangeAt
                  ? (category) => {
                      if (topicIndex >= 0) onCategoryChangeAt(topicIndex, category);
                    }
                  : undefined
              }
              // 작업 시각도 같은 규칙이다.
              workStartAt={topicIndex >= 0 ? (workStartTimes?.[topicIndex] ?? "") : ""}
              workStartInvalid={topicIndex >= 0 && topicIndex === invalidTimeIndex}
              onWorkStartAtChange={
                onWorkStartAtChangeAt
                  ? (next) => {
                      if (topicIndex >= 0) onWorkStartAtChangeAt(topicIndex, next);
                    }
                  : undefined
              }
              onRemove={rows.length > 1 ? () => remove(index) : undefined}
              onChange={(text) => change(index, text)}
              onEnter={() => enter(index)}
              onPaste={(text) => paste(index, text)}
            />
          );
        })}
      </div>
      <button
        type="button"
        className="scheduled-topic-add"
        disabled={disabled || rowCount >= maxRows}
        onClick={() => {
          write([...rows, ""]);
          setFocusRow(rowCount);
        }}
      >
        <PlusIcon />
        소재 추가
      </button>
    </>
  );
}
