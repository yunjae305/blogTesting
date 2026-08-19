/**
 * 원고 생성 진행률 계산.
 *
 * 서버는 단계가 **바뀔 때만** 알려 준다(4단계, 전체 2~4분). 그래서 완료된 단계 수만으로
 * 막대를 그리면 한 칸에서 1분 넘게 멈춰 있어, 사용자는 멈춘 것인지 도는 것인지 알 수 없다 —
 * 지금 화면이 그렇다.
 *
 * 여기서는 두 가지를 합쳐 실제로 움직이는 막대를 만든다.
 *
 * 1. **확정분** — 서버가 끝났다고 말한 단계는 그 단계의 몫을 그대로 채운다. 이건 사실이다.
 * 2. **진행 중인 단계의 추정분** — 그 단계에 머문 시간으로 채우되, 포화 곡선을 써서 **절대
 *    그 단계를 다 채우지 않는다**(상한 SATURATION). 오래 걸리면 느리게 계속 차오르기만 한다.
 *
 * 이 구조라서 막대는 거짓말을 하지 않는다: 100%는 서버가 완료를 알렸을 때만 나오고, 예상보다
 * 오래 걸려도 되돌아가거나 멈춰 보이지 않는다.
 */

/**
 * 단계별 대략적인 소요 시간(초). 여기서 막대의 몫(가중치)과 차오르는 속도가 함께 나온다 —
 * 두 값을 따로 두면 서로 어긋난다.
 *
 * **2026-08-11 사용자 실측으로 교체했다.** 그전 값은 `[35, 45, 45, 25]`(합 150초)였는데,
 * 근거로 남아 있던 것은 "구조 설계 + 본문 작성이 약 80초"라는 관측 하나뿐이었고 이미지
 * 45초는 근거가 없었다. 같은 주석이 "카드 이미지가 가장 오래 걸린다"고 적어 놓고 본문과
 * 같은 값을 준 상태였다. 실제로 잰 값은 이렇다:
 *
 *     원고 구조 설계   115초 (100~120초)
 *     본문 원고 작성    50초 (40~60초)
 *     카드 이미지 생성   94초
 *     사실 검수·다듬기  155초
 *
 * 합 414초(약 6분 54초)로, 예약 워커 주석의 "원고 한 편 실측 6분 27초(중앙값)"와 같은
 * 자리다. 옛 값은 실제의 36%였고, 그래서 각 칸이 금세 포화(92%)에 붙어 버티다 다음 칸으로
 * 툭 넘어갔다.
 *
 * 두 가지가 뒤집혔다: **가장 긴 칸은 이미지가 아니라 사실 검수**(37%)이고, 구조 설계가
 * 그다음(28%)이다. 본문 작성은 12%로 가장 짧다.
 *
 * 이 값은 **첫 실행용 기본값**이다. 실행이 끝날 때마다 서버가 실제 소요를 지수이동평균으로
 * 쌓아 다음 실행의 가중치로 보내므로(jobs.record_step_seconds), 쓸수록 그 환경의 값으로
 * 대체된다.
 */
export const DRAFT_STEP_SECONDS = [115, 50, 94, 155];

/** 관측값 저장 키. 형식이 바뀌면 뒤의 번호를 올려 옛 값을 무시한다. */
const OBSERVED_KEY = "blogit.draftStepSeconds.v1";

/**
 * 새 관측이 평균을 얼마나 끌어당기는가. 1이면 매번 마지막 실행값이 되어 흔들리고, 0에
 * 가까우면 영영 첫 추정치에 머문다. 0.4면 서너 번 만에 실제 값으로 옮겨 간다.
 */
const OBSERVATION_WEIGHT = 0.4;

/** 말이 되는 단계 소요의 범위(초). 밖의 값은 버린다 — 잠자기·탭 정지가 만든 값이다. */
const MIN_OBSERVED_SECONDS = 2;
const MAX_OBSERVED_SECONDS = 900;

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // 사생활 보호 모드·설정에 따라 접근 자체가 예외다. 그때는 첫 추정치로 돈다.
    return null;
  }
}

/** 저장된 관측값(단계 수가 맞을 때만). 없거나 깨졌으면 null이다. */
export function loadObservedStepSeconds(count: number): number[] | null {
  const store = storage();
  if (!store || count !== DRAFT_STEP_SECONDS.length) return null;
  try {
    const raw = store.getItem(OBSERVED_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length !== count) return null;
    const values = parsed.map((value) => (typeof value === "number" ? value : NaN));
    return values.every((value) => Number.isFinite(value) && value > 0) ? values : null;
  } catch {
    return null;
  }
}

/**
 * 방금 끝난 단계의 실제 소요를 기록한다. 기존 값이 있으면 지수이동평균으로 섞는다 —
 * 한 번 유난히 길었던 실행이 다음 실행의 막대를 통째로 망치지 않게 한다.
 */
export function recordObservedStepSeconds(index: number, seconds: number, count: number): void {
  const store = storage();
  if (!store || count !== DRAFT_STEP_SECONDS.length) return;
  if (index < 0 || index >= count) return;
  if (!Number.isFinite(seconds)) return;
  if (seconds < MIN_OBSERVED_SECONDS || seconds > MAX_OBSERVED_SECONDS) return;

  const current = loadObservedStepSeconds(count) ?? [...DRAFT_STEP_SECONDS];
  current[index] =
    current[index] * (1 - OBSERVATION_WEIGHT) + seconds * OBSERVATION_WEIGHT;
  try {
    store.setItem(OBSERVED_KEY, JSON.stringify(current.map((v) => Math.round(v * 10) / 10)));
  } catch {
    // 저장 실패(용량·권한)는 막대의 정확도만 떨어뜨린다. 생성에는 영향이 없다.
  }
}

/** 지금 쓸 단계별 소요(초). 관측값이 있으면 그것, 없으면 첫 추정치다. */
export function draftStepSeconds(count: number, measured?: number[]): number[] {
  if (count !== DRAFT_STEP_SECONDS.length) {
    // 서버가 다른 단계 목록을 보냈다. 길이를 모르니 균등 배분한다.
    return Array.from({ length: Math.max(0, count) }, () => 1);
  }
  // 서버가 잰 값이 먼저다(2026-08-11). 이 브라우저의 관측(localStorage)보다 표본이
  // 넓고, 예약 워커처럼 **브라우저가 보지 못한 실행**까지 들어 있다. 길이가 맞고 값이
  // 모두 양수일 때만 믿는다 — 반쯤 채워진 값으로 그리면 어림값보다 나쁠 수 있다.
  if (measured?.length === count && measured.every((value) => value > 0)) return measured;
  return loadObservedStepSeconds(count) ?? DRAFT_STEP_SECONDS;
}

/** 진행 중인 단계에서 채울 수 있는 최대 비율. 1이면 "다 됐다"는 거짓말이 된다. */
const SATURATION = 0.92;

/**
 * 포화 시점 계수. 예상 시간에 도달했을 때 그 단계의 약 70%가 차도록 잡았다 — 예상보다
 * 빨리 끝나면 남은 30%가 한 번에 채워지고, 늦어져도 천천히 계속 움직인다.
 */
const CURVE = 1.2;

const HOUR_MS = 60 * 60 * 1000;
/** 서버·클라이언트 시계가 어긋났을 때 말도 안 되는 경과시간이 나오지 않게 막는 상한. */
const MAX_ELAPSED_MS = 3 * HOUR_MS;

interface DraftProgressInput {
  /**
   * 진행 중인 단계가 **실제로 몇 개 중 몇 개를 끝냈는지**(2026-08-11). 있으면 시간 추정
   * 대신 이 비율로 그 칸을 채운다 — 이미지 3/5장은 짐작이 아니라 사실이다.
   */
  unitsDone?: number;
  unitsTotal?: number;
  /**
   * 서버가 이 환경에서 실제로 잰 단계별 소요(초). 있으면 아래 상수 대신 이것으로 몫과
   * 차오르는 속도를 정한다 — 상수는 어림값이고 이쪽은 실측이다(2026-08-11).
   */
  stepSeconds?: number[];
  /** 서버가 보고한 단계 이름들. 비어 있으면 균등 배분으로 계산한다. */
  steps: string[];
  /** 진행 중인 단계(0부터). 아직 시작 전이면 -1. */
  stepIndex: number;
  /** 그 단계에 머문 시간(ms). */
  elapsedInStepMs: number;
  /** 원고가 완성됐다 — 서버가 아니라 결과물이 근거다. */
  done?: boolean;
  /** 실패해서 멈춰 있다. 더 채우지 않는다. */
  failed?: boolean;
}

/** 단계별 몫(합이 1). 서버 단계 수가 예상과 다르면 균등 배분한다. */
export function stepWeights(count: number, measured?: number[]): number[] {
  if (count <= 0) return [];
  const seconds = draftStepSeconds(count, measured);
  const total = seconds.reduce((sum, value) => sum + value, 0);
  return seconds.map((value) => value / total);
}

/**
 * 진행 중인 단계가 얼마나 찼는지(0~SATURATION). 시간이 지날수록 차오르지만 결코 1이
 * 되지 않는 포화 곡선이다.
 */
function stepFill(
  stepIndex: number,
  elapsedInStepMs: number,
  count: number,
  measured?: number[],
): number {
  const seconds =
    count === DRAFT_STEP_SECONDS.length
      ? draftStepSeconds(count, measured)[stepIndex]
      : undefined;
  const expectedMs = (seconds ?? 40) * 1000;
  const ratio = Math.max(0, elapsedInStepMs) / expectedMs;
  return SATURATION * (1 - Math.exp(-CURVE * ratio));
}

/**
 * 사실로 채울 수 있으면 그것으로, 아니면 시간 추정으로.
 *
 * 개수를 아는 단계(이미지 생성)에서 시간 곡선을 쓰면, 빨리 끝나도 천천히 차고 늦어지면
 * 92%에 붙어 버틴다. 사실이 있는데 짐작할 이유가 없다. 다만 **끝까지 채우지는 않는다** —
 * 마지막 한 장이 저장·후처리를 남겨 두는 동안 100%가 보이면 거짓말이 된다.
 */
export function unitFill(
  done: number | undefined,
  total: number | undefined,
  fallback: () => number,
): number {
  if (typeof done !== "number" || typeof total !== "number" || total <= 0) return fallback();
  return Math.min(SATURATION, Math.max(0, done) / total);
}

/** 전체 진행률(0~100). 소수점은 버린다 — 화면에 숫자로 나가는 값이다. */
export function draftProgressPercent(input: DraftProgressInput): number {
  const count = input.steps.length;
  if (input.done) return 100;
  if (count === 0 || input.stepIndex < 0) return 0;

  const weights = stepWeights(count, input.stepSeconds);
  const index = Math.min(input.stepIndex, count - 1);
  const settled = weights.slice(0, index).reduce((sum, weight) => sum + weight, 0);
  // 실패한 원고는 그 자리에서 멈춘다. 멈춘 막대가 계속 차오르면 아직 도는 것처럼 보인다.
  const running = input.failed
    ? 0
    : weights[index] *
      unitFill(input.unitsDone, input.unitsTotal, () =>
        stepFill(index, input.elapsedInStepMs, count, input.stepSeconds),
      );

  return Math.min(99, Math.floor((settled + running) * 100));
}

/** 목록의 각 단계가 얼마나 찼는지(0~100). 끝난 단계는 100, 아직인 단계는 0. */
export function stepPercent(index: number, input: DraftProgressInput): number {
  const count = input.steps.length;
  if (input.done) return 100;
  if (count === 0 || input.stepIndex < 0 || index > input.stepIndex) return 0;
  if (index < input.stepIndex) return 100;
  if (input.failed) return 0;
  return Math.min(
    99,
    Math.floor(stepFill(index, input.elapsedInStepMs, count, input.stepSeconds) * 100),
  );
}

/**
 * ISO 시각부터 지금까지의 경과 ms. 파싱할 수 없거나 시계가 어긋나 음수/비현실적인 값이
 * 나오면 0으로 둔다 — 진행 표시 때문에 "-3초 경과" 같은 것을 보여줄 수는 없다.
 */
export function elapsedSince(isoTime: string | undefined, now: number): number {
  if (!isoTime) return 0;
  const started = Date.parse(isoTime);
  if (Number.isNaN(started)) return 0;
  const elapsed = now - started;
  if (elapsed < 0 || elapsed > MAX_ELAPSED_MS) return 0;
  return elapsed;
}

/** "1분 20초 경과"에 쓸 문자열. 분이 0이면 초만 쓴다. */
export function formatElapsed(elapsedMs: number): string {
  const totalSeconds = Math.floor(Math.max(0, elapsedMs) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}분 ${seconds}초` : `${seconds}초`;
}
