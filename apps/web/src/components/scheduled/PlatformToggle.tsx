import type { PostingChannel } from "../../api/types";
import { NaverMark, ThreadsMark } from "./icons";

/**
 * 화면에 보이는 한글 이름과 저장되는 식별자를 나눠 둔다.
 *
 * 식별자는 이미 프로젝트에 있는 값이다(PostingChannel = "naver" | "threads"). 화면 문구를
 * 그대로 저장하면 문구를 다듬는 순간 저장된 값과 어긋난다.
 */
const PLATFORM_LABELS: Record<PostingChannel, string> = {
  naver: "네이버",
  threads: "쓰레드",
};

const PLATFORM_ORDER: PostingChannel[] = ["naver", "threads"];

/**
 * 플랫폼 표식(초록 N / 검은 스레드).
 *
 * 이미 있는 표식(`.scheduled-mark`)을 그대로 쓰고 **크기만** `.platform-mark`로 줄인다.
 * 한때 이것을 다시 동그란 칸으로 감쌌는데, 안쪽 표식(24px 사각형)이 바깥 칸(18px 원)보다
 * 커서 버튼 밖으로 삐져나왔다(2026-08-05). 색과 모양은 한 곳에서만 정한다.
 */
export function platformMark(platform: PostingChannel) {
  return platform === "naver" ? (
    <NaverMark className="platform-mark" />
  ) : (
    <ThreadsMark className="platform-mark" />
  );
}

/**
 * 플랫폼별 발행 작업 수 — `[N 네이버 2건] [@ 쓰레드 1건]`.
 *
 * 합계('발행 작업 3건')만 적으면 그것이 어디로 나뉘어 가는지 보이지 않는다. 바로 옆의
 * 합계를 쪼갠 것이므로 **단위도 '건'으로 같다** — 원고를 세는 '편'과 섞지 않는다.
 *
 * **0건도 적는다.** 빼 버리면 '고르지 않았다'와 '이 화면이 세지 않는다'가 구분되지 않는다.
 */
export function PlatformCountBadges({ counts }: { counts: Record<PostingChannel, number> }) {
  return (
    <span className="platform-badges platform-badges--counts">
      {PLATFORM_ORDER.map((platform) => (
        <span
          className={`platform-badge ${counts[platform] === 0 ? "is-zero" : ""}`.trim()}
          key={platform}
        >
          {platformMark(platform)}
          {PLATFORM_LABELS[platform]} {counts[platform]}건
        </span>
      ))}
    </span>
  );
}

type Props = {
  platform: PostingChannel;
  selected: boolean;
  onToggle: () => void;
  disabled?: boolean;
  /** 어느 소재의 버튼인지. 읽어 주는 이름에만 쓴다. */
  rowLabel?: string;
};

/**
 * 플랫폼 한 칸. **누를 때마다 켜지고 꺼지는 토글**이지 라디오가 아니다 —
 * 네이버와 쓰레드는 동시에 고를 수 있다.
 *
 * **계정이 저장돼 있지 않다고 여기서 잠그지 않는다**(2026-08-05 사용자 요청). 잠가 두면
 * 무엇을 고르려던 것인지 화면에 남지 않는다 — 연결이 필요하다는 말은 예약을 시작할 때
 * 한 번에 한다(ScheduledView의 blockedReason).
 */
function PlatformToggle({
  platform,
  selected,
  onToggle,
  disabled,
  rowLabel,
}: Props) {
  const label = PLATFORM_LABELS[platform];
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={selected}
      aria-label={rowLabel ? `${rowLabel} ${label} 발행` : `${label} 발행`}
      className={`platform-toggle ${selected ? "is-on" : ""}`.trim()}
      disabled={disabled}
      onClick={onToggle}
    >
      {platformMark(platform)}
      {label}
    </button>
  );
}

type GroupProps = {
  selected: PostingChannel[];
  onChange: (platforms: PostingChannel[]) => void;
  disabled?: boolean;
  rowLabel?: string;
  /** 지금 고를 수 없는 이유. 잠긴 칸에 마우스를 올리면 나온다. */
  hint?: string;
};

/** 네이버·쓰레드 두 칸을 한 묶음으로. 폭을 고정해 켜고 꺼도 줄 높이가 흔들리지 않는다. */
export function PlatformToggleGroup({
  selected,
  onChange,
  disabled,
  rowLabel,
  hint,
}: GroupProps) {
  const toggle = (platform: PostingChannel) => {
    const next = selected.includes(platform)
      ? selected.filter((item) => item !== platform)
      : [...selected, platform];
    // 저장 순서를 늘 같게 둔다 — 순서만 다른 두 값이 다른 값처럼 보이지 않게.
    onChange(PLATFORM_ORDER.filter((item) => next.includes(item)));
  };

  return (
    <div className="platform-toggles" title={hint}>
      {PLATFORM_ORDER.map((platform) => (
        <PlatformToggle
          key={platform}
          platform={platform}
          selected={selected.includes(platform)}
          disabled={disabled}
          rowLabel={rowLabel}
          onToggle={() => toggle(platform)}
        />
      ))}
    </div>
  );
}
