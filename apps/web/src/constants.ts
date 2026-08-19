/** 공용 화면 상수. 페르소나 프리셋은 GET /personas에서 불러온다. */

import type { BlendMode, BlogTaskStatus } from "./api/types";

export const WRITING_PURPOSES = [
  "정보 전달",
  "입문·소개",
  "일상·경험 공유",
  "사용법·가이드",
  "후기·리뷰 작성",
  "비교·추천",
  "문제 해결",
  "트렌드·이슈 소개",
  "제품·서비스 홍보",
];

/**
 * 대상 연령과, 그 연령을 고르면 **글이 실제로 어떻게 달라지는지**.
 *
 * 여기 적는 연령은 **글을 읽는 사람**의 나이다. 글쓴이의 나이가 아니다 — 그것을
 * 말하지 않았더니 제목에 '20대의 시각으로 본 후기'가 나왔다(2026-08-07 사용자 신고).
 *
 * description은 **한 줄로 짧게** 적는다(2026-08-07 사용자 요청). 지어낸 설명이 아니라
 * 서버가 그 연령에 실제로 적용하는 지침을 줄인
 * 것이다(`apps/api/app/llm/prompts.py`의 `READER_AGE_GUIDES`). 지침을 고치면 여기도
 * 함께 고쳐야 한다 — 안 그러면 화면이 하지 않는 일을 약속하게 된다.
 *
 * 카드 아래에 늘 붙여 두지 않고 **고른 뒤에** 보여 준다. 다섯 개를 한꺼번에 깔면
 * 고르기 전에 읽을 것이 너무 많다(2026-08-07 사용자 요청).
 */
export const READER_AGE_RANGES = [
  {
    // 여섯 개 중 이것만 길어서 안내가 두 줄로 끊겼다(2026-08-11 사용자 지적). 뜻은
    // 그대로 두고 나머지와 같은 길이로 줄인다 — 서버가 연령 미지정에 실제로 거는
    // 지침("특정 세대만 아는 표현·유행어를 쓰지 않고, 누가 읽어도 통하는 기준으로
    // 설명한다", prompts.py age_guide_lines)을 줄인 문장이다.
    value: "",
    label: "전체",
    description: "특정 세대의 표현 없이, 누가 읽어도 통하게 씁니다.",
  },
  {
    value: "10s",
    label: "10대",
    description: "질문으로 열고 결론부터. 짧은 문장, 쉬운 말로 씁니다.",
  },
  {
    value: "20s",
    label: "20대",
    description: "고민을 먼저 꺼내고, 시간·비용을 기준으로 정리합니다.",
  },
  {
    value: "30s",
    label: "30대",
    description: "결론과 판단 기준을 먼저, 조건별로 적용해 줍니다.",
  },
  {
    value: "40s",
    label: "40대",
    description: "배경과 근거부터, 장기적인 영향까지 비교합니다.",
  },
  // 50대와 60대 이상을 하나로 합쳤다(2026-08-05 사용자 결정). 두 구간이 궁금해하는 것이
  // 사실상 같아, 나눠도 글이 달라지지 않았다.
  {
    value: "50plus",
    label: "50대 이상",
    description: "왜 필요한지부터 준비물과 순서를 하나씩 적습니다.",
  },
];

/** 선택지에서 사라진 옛 저장값. 이미 저장된 글이 계속 읽혀야 하므로 지우지 않는다. */
const LEGACY_READER_AGE_LABELS: Record<string, string> = {
  "50s": "50대 이상",
  "60plus": "50대 이상",
};

/** 저장값("30s") → 화면 이름("30대").
 *
 * 저장값을 그대로 화면에 내보내면 요약에 "30s"가 뜬다(2026-08-05 사용자 지적). 여러 개를
 * 고른 경우(콤마로 이어 저장) 각각 바꿔 이어 붙이고, 목록에 없는 값은 그대로 둔다 —
 * 라벨을 못 찾은 것을 '전체'로 감추면 무엇이 저장돼 있는지 화면에서 알 수 없다.
 */
export function readerAgeLabel(value: string | null | undefined): string {
  const raw = (value ?? "").trim();
  if (!raw) return "전체";
  return raw
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map(
      (part) =>
        READER_AGE_RANGES.find((range) => range.value === part)?.label ??
        LEGACY_READER_AGE_LABELS[part] ??
        part,
    )
    .join(", ");
}

/**
 * 소재가 **어느 분야의 것인가**(2026-08-11).
 *
 * '오디세이'는 영화이고 게임이고 모니터다. 소재 글자만 보내면 모델이 어느 쪽인지 스스로
 * 고르고, 사용자가 원한 분야와 다르면 제목·자료 수집·이미지가 전부 그 판단 위에 얹혀
 * 뒤에서 되돌릴 수 없다. 그래서 처음 한 번 직접 묻는다.
 *
 * **서버의 SUBJECT_CATEGORIES와 글자까지 같아야 한다**(apps/api/app/shared/blog_task.py).
 * 서버가 목록 밖의 값을 거절하므로, 어긋나면 그 버튼이 그대로 저장 실패가 된다.
 *
 * 2026-07-20에 없앤 '주제(네이버 카테고리 32개)'로 돌아가는 것이 아니다. 그때 없앤 것은
 * 발행 분류였고, 이것은 소재 해석 조건이라 목록도 동명이의어가 갈리는 축으로 짰다.
 */
export const SUBJECT_CATEGORIES = [
  "인물·연예인",
  "영화·드라마·방송",
  "게임",
  "IT·컴퓨터·AI",
  "브랜드·기업",
  "제품·쇼핑·리뷰",
  "음식·맛집",
  "여행·장소",
  "스포츠",
  "자동차·모빌리티",
  "건강·생활",
  "정책·시사",
] as const;

export const SAMPLE_INPUT = {
  topic: "AIONA",
  purpose: "트렌드·이슈 소개",
  readerAgeRange: "",
  subjectCategory: "브랜드·기업",
};

/**
 * 예약 포스팅이 글을 만들 때 채워 넣는 목적 값. **서버의 SCHEDULED_DEFAULT_PURPOSE와
 * 같은 문자열이어야 한다**(apps/api/app/modules/scheduled_posting/validation.py).
 *
 * 사용자가 고른 목적이 아니라 create_blog_task가 요구하는 자리를 채우는 내부 기본값이라
 * 화면에는 싣지 않는다 — 예약으로 만든 글도 일반 글과 같은 모습으로 보여야 한다
 * (2026-08-04 사용자 요청).
 */
const SCHEDULED_DEFAULT_PURPOSE = "소재에 맞는 유용한 정보 제공 및 검색 의도 충족";

/** 화면에 보여 줄 목적만 남긴다. 예약의 내부 기본값은 걸러진다. */
export function visiblePurposes(purposes: string[] | undefined | null): string[] {
  return (purposes ?? []).filter((purpose) => purpose !== SCHEDULED_DEFAULT_PURPOSE);
}

/** 작성 단계. `resume.ts`의 WRITE_STEP과 **같은 순서여야 한다** — 번호로 맞물린다. */
export const STEPS = [
  { title: "소재", hint: "이름이나 대상을 적습니다" },
  { title: "제목", hint: "트렌드 키워드로 다듬습니다" },
  { title: "검증", hint: "찾은 자료와 글의 방향을 확인합니다" },
  { title: "원고", hint: "초안을 만들고 다듬습니다" },
  { title: "발행", hint: "복사하거나 올립니다" },
];

/**
 * Only what is shown before the first progress update arrives — after that the
 * steps come from the server, which is the one that knows where the work is.
 *
 * Must match PHASE_STEPS[DRAFT] in apps/api/app/shared/blog_task.py, or the list
 * visibly changes under the user the moment the first poll lands.
 */
export const DRAFT_FLOW_STEPS = [
  "원고 구조 설계",
  "본문 원고 작성",
  "카드 이미지 생성",
  "사실 검수·문장 다듬기",
];

export const STATUS_LABELS: Record<string, { text: string; tone: string }> = {
  INPUT: { text: "작성 중", tone: "" },
  REFERENCE_PROCESSING: { text: "소재 준비됨", tone: "" },
  SEARCH_ANALYZING: { text: "원고 준비 중", tone: "" },
  INTENT_SELECTED: { text: "원고 대기", tone: "" },
  GENERATING: { text: "원고 만드는 중", tone: "" },
  // 필터가 '원고 생성 완료'라 부르는 상태다. 카드에 '발행 대기'라고 적혀 있으면 같은
  // 것에 이름이 둘이 되어, 필터로 고른 결과가 다른 상태처럼 보인다(2026-08-06 사용자 요청).
  READY_TO_PUBLISH: { text: "원고 생성 완료", tone: "warn" },
  POSTING: { text: "발행 중", tone: "warn" },
  POSTED: { text: "발행 완료", tone: "ok" },
  POSTING_NEEDS_HUMAN: { text: "확인 필요", tone: "warn" },
  FAILED: { text: "실패", tone: "danger" },
  CONTENT_POLICY_VIOLATION: { text: "정책 위반", tone: "danger" },
};

const DEAD_END_STATUSES = new Set(["FAILED", "CONTENT_POLICY_VIOLATION"]);

type PostStatKind = "draft" | "published" | "attention";

/** 홈의 현황 카드(작성 중/발행 완료/확인 필요)와 내 글 목록의 필터가 같은 기준으로
    나누게 하는 단일 출처. 둘이 각자 계산하면 카드를 눌러 들어간 목록이 카드가 센 숫자와
    어긋날 수 있다. */
export function postStatKind(status: BlogTaskStatus): PostStatKind {
  if (status === "POSTED") return "published";
  if (status === "POSTING_NEEDS_HUMAN" || DEAD_END_STATUSES.has(status)) return "attention";
  return "draft";
}

/**
 * 내 글 목록 필터. 두 세기(granularity)를 하나로 다룬다:
 * - group: 홈 현황 카드가 쓰는 넓은 묶음(작성 중/발행 완료/확인 필요).
 * - status: 목록 화면의 깔때기 필터가 쓰는 개별 상태(원고 생성 완료·발행 완료 등).
 * null이면 전체 보기.
 */
export type PostsFilter =
  | { type: "group"; kind: PostStatKind }
  | { type: "status"; status: BlogTaskStatus };

export function matchesPostsFilter(
  task: { status: BlogTaskStatus },
  filter: PostsFilter | null,
): boolean {
  if (!filter) return true;
  if (filter.type === "status") return task.status === filter.status;
  return postStatKind(task.status) === filter.kind;
}

/**
 * 내 글 목록의 정렬.
 *
 * "최신순"은 **서버가 주는 순서 그대로**다(만든 날짜 내림차순). 여기서 다른 키로 다시
 * 정렬하지 않는다 — 목록이 어떤 기준으로 늘어서 있는지가 화면과 서버에서 달라지면,
 * 새로고침할 때마다 순서가 바뀌는 것처럼 보인다.
 */
export type PostsSort = "newest" | "oldest";

export function sortPosts<T>(posts: T[], sort: PostsSort): T[] {
  return sort === "oldest" ? [...posts].reverse() : posts;
}

/** 내 글 목록의 보기 방식. 카드는 제목이 크게 보이고, 리스트는 한 화면에 많이 들어간다. */
export type PostsLayout = "card" | "list";

export const ERROR_MESSAGES: Record<string, string> = {
  INVALID_STATUS_TRANSITION: "이 단계는 지금 진행할 수 없습니다. 화면을 새로고침해 주세요.",
  EMAIL_ALREADY_EXISTS: "이미 가입된 이메일입니다. 로그인해 주세요.",
  INVALID_CREDENTIALS: "이메일 또는 비밀번호가 올바르지 않습니다.",
  UNAUTHORIZED: "로그인이 필요합니다.",
  FORBIDDEN: "이 글에 접근할 권한이 없습니다.",
  NOT_FOUND: "찾을 수 없습니다.",
};

export const TREND_SOURCE_LABELS: Record<string, string> = {
  GOOGLE_TRENDS: "GOOGLE",
  YOUTUBE: "YOUTUBE",
  NAVER_DATALAB: "NAVER",
  INSTAGRAM: "INSTAGRAM",
  // 외부 검색이 아니라 소재에서 파생한 후보. 네이버·유튜브로 표시하면 사용자가 관측된
  // 검색 수요로 오인하므로, 근거가 다르다는 것을 라벨로 드러낸다.
  RELATED_EXPANSION: "소재 확장",
};

/**
 * 소재에 적을 수 있는 글자 수.
 *
 * **서버의 `MAX_TOPIC_CHARS`와 같아야 한다**(apps/api/app/modules/blog_task/validation.py).
 * 화면이 더 크게 잡으면 다 적고 나서 저장할 때 거절당하고, 더 작게 잡으면 서버가
 * 받아 주는 것을 못 적는다.
 *
 * 소재는 단어만이 아니다 — '스파이더맨 4편'처럼 짧을 수도, 한 문장일 수도 있다
 * (2026-08-06 사용자 지적).
 */
export const MAX_TOPIC_CHARS = 300;

// 개수 제한을 두지 않는다(2026-08-11 사용자 요청: "올리는 개수 제한 없애고 그냥 최대
// 20MB까지만"). **실제 한계는 용량**이고, 그것은 파일 상한(10MB)이 이미 지킨다.
//
// 0으로 두어 '무제한'을 뜻하게 하지 않은 이유: 그러면 비교하는 자리마다 예외를 써야
// 한다. 사람이 손으로 올릴 수 있는 수를 훨씬 넘는 값을 두어, 코드는 그대로 두고
// 화면에서는 이 숫자를 **보여 주지 않는다**.
export const MAX_REFERENCE_MATERIALS = 1000;

/**
 * How the title prompt weights 소재 against the trend keyword. This used to be a
 * fixed 3:7 ratio the panel advertised, but the model cannot hold an exact
 * percentage and promising a number the output never keeps reads as dishonest
 * (review 3.6). It is now a direction — what sits at the center of the title —
 * chosen in settings and enforced qualitatively by the prompt
 * (apps/api/app/llm/prompts.py blend_rules).
 */
export const BLEND_MODE_OPTIONS: { id: BlendMode; label: string; description: string }[] = [
  {
    id: "subject",
    label: "소재 중심",
    description: "소재가 제목의 중심. 트렌드 키워드는 소재를 부각하는 최신 각도로 곁들입니다.",
  },
  {
    id: "balanced",
    label: "균형",
    description: "소재와 트렌드 키워드를 비슷한 비중으로 엮습니다.",
  },
  {
    id: "trend",
    label: "트렌드 중심",
    description: "트렌드 키워드가 제목의 중심. 소재는 그 키워드를 풀어내는 각도로 등장합니다.",
  },
];

export const DEFAULT_BLEND_MODE: BlendMode = "trend";

/**
 * 한 소재로 한 번에 만들 수 있는 원고 수(2026-08-12 사용자 결정).
 *
 * 3으로 잡은 이유는 방향 후보가 4개이기 때문이다 — 3편을 만들 때도 **하나는 버리는
 * 선택**이 남아야 고르는 일이 형식적이지 않다. 서버의 MAX_DRAFT_COUNT와 같은 값이다.
 */
export const MAX_DRAFT_COUNT = 3;

/**
 * 검증이 만들어 주는 글 방향 후보 수. 서버의 `INTENT_CANDIDATE_COUNT`(llm/schemas.py)와
 * 같은 값이어야 한다 — 화면이 "방향 후보 4개"라고 적는데 서버가 3개를 만들면 거짓이 된다.
 *
 * 검증이 **실패했을 때** 화면이 이 숫자를 쓴다: 후보가 줄어든 것이 아니라 자료를 못 모아
 * 후보 자체를 만들지 못했다는 것을 알려야 하기 때문이다(2026-08-12 사용자 신고).
 */
export const INTENT_CANDIDATE_COUNT = 4;
