/**
 * Application state.
 *
 * The vanilla version kept the subject/purpose/age/persona selections in the DOM
 * and read them back with querySelector at submit time. Here they are real state,
 * which is the one structural change the React port required.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  clearSession,
  configureAuth,
  friendlyError,
  loadSession,
  request,
  storeSession,
} from "./api/client";
import {
  removeAccountSession,
  saveAccountSession,
} from "./accountSessions";
import type {
  AuthSession,
  BlogTask,
  BlogTaskListItem,
  IntentCandidate,
  PersonaCatalogEntry,
  ScheduledJobList,
  TopicCandidate,
  TrendMode,
  TrendRecommendation,
  UserSettings,
} from "./api/types";
// 트렌드 보기 방식의 기본값. 화면(StepTrends)과 같은 상수를 써야 한다 — 두 곳에 적으면
// 한쪽만 바뀌었을 때 탭과 목록이 서로 다른 모드를 말하게 된다.
import { DEFAULT_TREND_MODE } from "./components/write/trends";
import type { PostsFilter } from "./constants";
import { saveRememberedAccount } from "./rememberedAccounts";
import {
  cachePersonaCatalog,
  loadCachedPersonaCatalog,
  normalizePersonaCatalog,
} from "./personas";
import { WRITE_STEP, resumeStep } from "./resume";
import {
  pollTaskUntilSettled,
  type BlogTaskStatusSnapshot,
} from "./taskPolling";

export type Route = "home" | "write" | "bulk" | "scheduled" | "posts" | "settings";

interface SignInResult {
  activated: boolean;
  persisted: boolean;
}

const ONBOARDING_KEY_PREFIX = "blog-it:writing-setup-onboarding-seen:";
const POST_DETAIL_CACHE_LIMIT = 3;

function onboardingSeenKey(userId?: string): string | null {
  return userId ? `${ONBOARDING_KEY_PREFIX}${userId}` : null;
}

function hasSeenOnboarding(key: string): boolean {
  try {
    return localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function rememberOnboardingSeen(key: string): void {
  try {
    localStorage.setItem(key, "true");
  } catch {
    // Storage can be blocked in private or hardened browser contexts. Onboarding
    // must never turn a successful login into a half-failed one.
  }
}

/** 이 글이 열 수 있는 가장 높은 단계.
 *
 * 판단 규칙은 resume.ts 한 곳에 있다 — 목록 카드의 버튼 문구, '이어서 쓰기'가 여는
 * 단계, 스테퍼가 허용하는 범위가 서로 다른 기준으로 움직이면 카드에 적힌 말과 실제로
 * 열리는 화면이 어긋난다.
 *
 * SEARCH_ANALYZING에 자기 단계가 없는 것은 그대로다: 검증은 제목 단계 위에 뜨는
 * 팝업이라, 의도를 고를 때까지 글은 제목 단계에 머문다.
 */
export function maxStep(task: BlogTask | null): number {
  return resumeStep(task);
}

/** 주소창에 남기는 작업실 경로. postId가 있으면 새로고침·뒤로가기에서 같은 글로 돌아온다. */
function writeHash(postId: string | null): string {
  return postId ? `#/write/${encodeURIComponent(postId)}` : "#/write";
}

/** 백엔드가 제목 선택(M2)을 받아 주는 상태.
 *
 * REFERENCE_PROCESSING(첫 선택 전)만 허용하던 시절에는 제목을 한 번 고르면 글이
 * SEARCH_ANALYZING으로 넘어가면서 제목 단계가 통째로 잠겼다 — 검증 팝업의 '수정하기'로
 * 돌아와도 아무것도 누를 수 없었다. 방향(selectedIntent)을 확정하기 전까지는 제목을
 * 다시 고를 수 있다. 확정 뒤에는 원고가 그 제목으로 쓰이므로 되돌리지 않는다.
 */
export function canSelectTrendTopic(task: BlogTask | null): boolean {
  if (!task) return false;
  if (task.status === "REFERENCE_PROCESSING") return true;
  return task.status === "SEARCH_ANALYZING" && !task.selectedIntent;
}

/** A freshly loaded full task also refreshes its lightweight list card. */
function listItemFromTask(task: BlogTask): BlogTaskListItem {
  return {
    postId: task.postId,
    userId: task.userId ?? "",
    status: task.status,
    version: task.version ?? 0,
    createdAt: task.createdAt ?? "",
    updatedAt: task.updatedAt ?? "",
    title:
      task.finalPost?.title ||
      task.trendSelection?.finalTopic ||
      task.input?.topic ||
      "제목 없는 글",
    topic: task.input?.topic ?? "",
    subject: task.input?.subject,
    purposes: task.input?.purpose ?? task.input?.keywords ?? [],
    postUrl: task.postingLogs?.find((log) => log.postUrl)?.postUrl,
    hasFinalPost: Boolean(task.finalPost),
  };
}

/** Runtime guard for the detail cache; tests and callers can pass partial casts. */
function isCompleteBlogTask(task: BlogTask): boolean {
  return Boolean(
    task.userId &&
      task.createdAt &&
      task.updatedAt &&
      task.input?.topic &&
      Array.isArray(task.postingLogs) &&
      Array.isArray(task.statusHistory),
  );
}

function rememberPostDetail(cache: Map<string, BlogTask>, task: BlogTask): void {
  cache.delete(task.postId);
  cache.set(task.postId, task);
  while (cache.size > POST_DETAIL_CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

/**
 * 편마다 고른 것을 **새로고침 뒤에도 들고 있게** 한다(2026-08-12 사용자 지시: "첫번째편에서
 * 선택한 것들은 기록이 유지되어야지").
 *
 * 이 값은 서버에 저장할 수 없다 — 글 하나에는 제목·방향 자리가 하나뿐이라, 편마다 저장하면
 * 다음 편이 앞 편을 덮어쓴다(그래서 마지막에 한 번에 보낸다). 그렇다고 메모리에만 두면
 * 새로고침 한 번에 1·2편째에 고른 것이 통째로 사라진다. 글별로 브라우저에 남겨 둔다.
 *
 * sessionStorage인 이유: 탭을 닫으면 정리되는 것이 맞다. 여기 남는 것은 아직 서버에 없는
 * '고르는 중'인 값이라, 며칠 뒤 다른 세션에서 되살아나면 오히려 혼란스럽다.
 */
const DRAFT_ROUNDS_KEY_PREFIX = "blog-it:draft-rounds:";

function draftRoundsKey(postId: string): string {
  return `${DRAFT_ROUNDS_KEY_PREFIX}${postId}`;
}

function loadDraftRounds(postId: string | null): DraftRound[] {
  if (!postId) return [];
  try {
    const raw = sessionStorage.getItem(draftRoundsKey(postId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    // 손으로 고쳤거나 옛 형식일 수 있다. 모양이 다르면 없는 것으로 친다 — 반쯤 읽어
    // 엉뚱한 편에 채워 넣지 않는다.
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (round): round is DraftRound =>
        Boolean(round) && typeof round === "object" && Array.isArray((round as DraftRound).keywords),
    );
  } catch {
    return [];
  }
}

function saveDraftRounds(postId: string | null, rounds: DraftRound[]): void {
  if (!postId) return;
  try {
    if (rounds.length) sessionStorage.setItem(draftRoundsKey(postId), JSON.stringify(rounds));
    else sessionStorage.removeItem(draftRoundsKey(postId));
  } catch {
    // 저장이 막힌 브라우저(시크릿·강화 모드)에서도 화면은 그대로 동작해야 한다. 이 경우
    // 새로고침하면 편별 선택이 사라지지만, 지금 고르고 있는 흐름은 끊기지 않는다.
  }
}

/** 한 편을 위해 고른 것. 아직 서버에 저장하지 않은 상태다. */
type DraftRound = {
  /** 그 편의 제목(② 단계). 건너뛰면 원고 생성 때 자동으로 붙는다. */
  title?: string;
  /** 그 편에서 고른 트렌드 검색어. 요약 패널이 "원고1 / 원고2"로 보여 준다. */
  keywords: string[];
  /** 그 편의 글 방향(③ 단계). 아직 안 골랐으면 없다. */
  intentId?: string;
  intentTitle?: string;
  /**
   * 고른 방향 **전체**(2026-08-12 사용자 신고: 3편째 방향을 고르니 `/schedule`이 400).
   *
   * `intentId`는 자리번호다(`{postId}_intent_{n}`). 편마다 제목을 다시 골라 검증을 다시
   * 돌리는데, 새 제목을 저장하면 서버가 **옛 검증 결과를 지운다** — 마지막에 예약을 걸
   * 시점의 글에는 마지막 편의 후보만 남아, 앞 편의 자리번호는 다른 방향을 가리킨다.
   * 그래서 화면이 고른 것을 통째로 들고 있다가 함께 보낸다.
   */
  intent?: IntentCandidate;
};

interface Store {
  session: AuthSession | null;
  settings: UserSettings | null;
  personas: PersonaCatalogEntry[];
  personaCatalogLoading: boolean;
  posts: BlogTaskListItem[];
  /** 목록을 처음 읽거나 최신 상태로 갱신하는 중. */
  postsLoading: boolean;
  /**
   * **예약 포스팅이 만든** 글의 postId. 목록 카드가 이 값으로 갈 곳을 정한다.
   *
   * 새 글 작성과 예약 포스팅은 하는 일이 다르다 — 하나는 글 한 편을 손으로 끌고 가는
   * 자리이고, 다른 하나는 소재 여러 개를 걸어 두면 서버가 순서대로 만들어 올리는
   * 자리다. 그래서 예약이 만들고 있는 글의 진행은 예약의 작업 큐에서 봐야 한다.
   */
  scheduledPostIds: Set<string>;
  task: BlogTask | null;
  /** 작업실이 지금 열고 있는 글. null이면 아직 만들지 않은 새 글이다. */
  activePostId: string | null;
  /** 그 글의 최신 상태를 서버에서 가져오는 중. 이전 글 내용을 대신 보여주지 않는다. */
  postLoading: boolean;
  /**
   * 그 글을 **열지 못한** 이유. 성공하거나 다른 글을 열면 지워진다.
   *
   * 이 값이 없던 동안, 조회가 실패하면 `task`가 null인 채로 남아 화면이 **빈 '새 글
   * 작성'**을 그렸다 — 사용자는 '발행하기'를 눌렀는데 아무것도 없는 소재 입력 폼 앞에
   * 서게 됐다(2026-08-06 신고). 그 글이 사라진 것도, 새 글을 시작한 것도 아니고 그저
   * 한 번의 조회가 실패한 것이라, 화면은 그 사실을 말하고 다시 시도할 길을 줘야 한다.
   */
  postLoadError: string | null;
  recommendation: TrendRecommendation | null;
  /**
   * 제목 단계에서 고른 보기 방식(최신순 / 소재 관련순).
   *
   * **recommendation 옆에 있어야 한다.** 예전에는 StepTrends의 지역 상태였는데,
   * 목록(recommendation)은 이 store에 있고 탭은 화면에 있어서 수명이 달랐다 — 소재
   * 단계에 다녀오면 StepTrends가 다시 그려지며 탭만 '최신순'으로 돌아가고, 화면에는
   * 소재 관련순으로 모은 키워드가 그대로 남았다(2026-08-11 사용자 신고). 사용자는
   * 자기가 고른 보기를 다시 눌러야 했다. 한 사실을 두 곳에 나눠 두면 생기는 일이라
   * 목록과 같은 자리로 옮긴다.
   */
  trendMode: TrendMode;
  selectedTrendKeywordIds: string[];
  trendKeywordSelectionTouched: boolean;
  step: number;
  route: Route;
  onboardingOpen: boolean;
  accountChooserOpen: boolean;
  toast: { message: string; isError: boolean; id: number } | null;
  postsFilter: PostsFilter | null;

  signIn: (session: AuthSession) => Promise<SignInResult>;
  signOut: (options?: { silent?: boolean }) => void;
  openAccountChooser: () => void;
  closeAccountChooser: () => void;
  showToast: (message: string, isError?: boolean) => void;
  reportError: (error: unknown) => void;

  setRoute: (route: Route) => void;
  setStep: (step: number) => void;
  /** 원고 단계에서 저절로 생성을 시작해도 되는가. 검증에서 넘어왔을 때만 참이다. */
  draftAutoStart: boolean;
  setDraftAutoStart: (value: boolean) => void;
  /**
   * 한 소재로 여러 편을 만들 때 **편마다 고른 것**(2026-08-12).
   *
   * 글 하나에는 제목·방향 자리가 하나뿐이라, 2·3편째로 넘어가면 앞서 고른 것이 덮어써진다.
   * 그래서 서버에 저장하지 않고 화면이 배열로 들고 있다가 마지막에 한 번에 보낸다 —
   * 요약 패널이 "원고1 / 원고2"로 그것을 그린다.
   */
  draftRounds: DraftRound[];
  setDraftRounds: (rounds: DraftRound[]) => void;
  setPostsFilter: (filter: PostsFilter | null) => void;
  setTask: (task: BlogTask) => void;
  openPost: (postId: string) => Promise<void>;
  restartWriting: () => void;

  setSettings: (settings: UserSettings) => void;
  setRecommendation: (recommendation: TrendRecommendation | null) => void;
  setTrendMode: (mode: TrendMode) => void;
  setTopicCandidates: (candidates: TopicCandidate[], generatedAt: string) => void;
  selectTrendKeyword: (id: string) => void;
  setSelectedTrendKeywordIds: (ids: string[], touched: boolean) => void;

  dismissOnboarding: () => void;
  markOnboardingSeen: () => void;

  followTask: (postId: string) => Promise<BlogTask | null>;

  reloadPosts: () => Promise<void>;
  reloadPersonaCatalog: () => Promise<void>;
  deletePosts: (postIds: string[]) => Promise<void>;
}

const StoreContext = createContext<Store | null>(null);

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useStore must be used inside <StoreProvider>");
  return store;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(loadSession);
  const [settings, setSettingsState] = useState<UserSettings | null>(null);
  const [personas, setPersonas] = useState<PersonaCatalogEntry[]>(loadCachedPersonaCatalog);
  const [personaCatalogLoading, setPersonaCatalogLoading] = useState(true);
  const [posts, setPosts] = useState<BlogTaskListItem[]>([]);
  const [postsLoading, setPostsLoading] = useState(() => session !== null);
  const [scheduledPostIds, setScheduledPostIds] = useState<Set<string>>(() => new Set());
  const [task, setTaskState] = useState<BlogTask | null>(null);
  const [activePostId, setActivePostIdState] = useState<string | null>(null);
  const [postLoading, setPostLoading] = useState(false);
  const [postLoadError, setPostLoadError] = useState<string | null>(null);
  const [recommendation, setRecommendation] = useState<TrendRecommendation | null>(null);
  const [trendMode, setTrendModeState] = useState<TrendMode>(DEFAULT_TREND_MODE);
  const [selectedTrendKeywordIds, setSelectedIds] = useState<string[]>([]);
  const [trendKeywordSelectionTouched, setTouched] = useState(false);
  const [step, setStepState] = useState(0);
  const [route, setRouteState] = useState<Route>("home");
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [accountChooserOpen, setAccountChooserOpen] = useState(false);
  const [toast, setToast] = useState<Store["toast"]>(null);
  const [postsFilter, setPostsFilter] = useState<PostsFilter | null>(null);

  const toastTimer = useRef<number>(0);
  const bootstrapped = useRef(false);
  const authEpochRef = useRef(0);
  const sessionRef = useRef(session);
  const taskRef = useRef<BlogTask | null>(null);
  const personasRef = useRef(personas);
  const postsRef = useRef(posts);
  /** 전체 글은 상세 GET으로 확인한 것만 보관한다. 목록 summary를 작업실에 적용하지 않는다. */
  const postDetailsRef = useRef<Map<string, BlogTask>>(new Map());
  /** 같은 계정에서 목록 재요청이 겹쳐도 마지막 요청만 loading/data를 끝낼 수 있다. */
  const postsRequestRef = useRef(0);
  const routeRef = useRef<Route>("home");
  /**
   * 작업실이 지금 붙어 있는 글. 화면에 보이는 것이 어느 글인지 판단하는 단일 기준이며,
   * setTask/폴링/열기가 전부 이것과 대조한다. 렌더 사이의 지연 없이 즉시 읽혀야 해서
   * state가 아니라 ref가 원본이다.
   */
  const activePostIdRef = useRef<string | null>(null);
  /**
   * '새 글로 시작'으로 작업실에서 내보낸 글. 그 글에 대해 이미 날아가 있던 요청이
   * 늦게 도착해도 빈 작업실을 다시 그 글로 채우면 안 된다. 다시 열면 풀린다.
   */
  const discardedPostIdsRef = useRef<Set<string>>(new Set());
  /** postId별로 지금 돌고 있는 폴링. 같은 글을 두 번 따라가지 않는다. */
  const followingRef = useRef<Map<string, Promise<BlogTask | null>>>(new Map());
  /** 주소 라우팅 effect가 최신 openPost를 부를 수 있게 하는 통로(아래에서 채운다). */
  const openPostRef = useRef<((postId: string) => Promise<void>) | null>(null);
  sessionRef.current = session;
  taskRef.current = task;
  personasRef.current = personas;
  postsRef.current = posts;
  routeRef.current = route;
  const renderedAuthEpoch = authEpochRef.current;

  useEffect(() => {
    const versions = new Map(posts.map((item) => [item.postId, item.version]));
    for (const [postId, detail] of postDetailsRef.current) {
      if (versions.get(postId) !== detail.version) postDetailsRef.current.delete(postId);
    }
  }, [posts]);

  const showToast = useCallback((message: string, isError = false) => {
    setToast({ message, isError, id: Date.now() });
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
  }, []);

  const reportError = useCallback(
    (error: unknown) => showToast(friendlyError(error), true),
    [showToast],
  );

  const resetUserWorkspace = useCallback(() => {
    setSettingsState(null);
    setPosts([]);
    postsRequestRef.current += 1;
    postDetailsRef.current.clear();
    setPostsLoading(false);
    taskRef.current = null;
    setTaskState(null);
    activePostIdRef.current = null;
    setActivePostIdState(null);
    discardedPostIdsRef.current.clear();
    followingRef.current.clear();
    setPostLoading(false);
    setPostLoadError(null);
    setRecommendation(null);
    setTrendModeState(DEFAULT_TREND_MODE);
    setSelectedIds([]);
    setTouched(false);
    setDraftRoundsState([]);
    setStepState(0);
    setPostsFilter(null);
    setOnboardingOpen(false);
    setAccountChooserOpen(false);
    setRouteState("home");
    location.hash = "#/";
  }, []);

  const deactivateSession = useCallback(() => {
    authEpochRef.current += 1;
    configureAuth(null, () => undefined);
    const activeSessionCleared = clearSession();
    sessionRef.current = null;
    setSession(null);
    resetUserWorkspace();
    return activeSessionCleared;
  }, [resetUserWorkspace]);

  const openAccountChooser = useCallback(() => {
    // Opening the chooser must not disturb an in-progress draft. The active
    // session and workspace remain intact until another account is verified.
    setAccountChooserOpen(true);
  }, []);

  const closeAccountChooser = useCallback(() => {
    setAccountChooserOpen(false);
  }, []);

  const signOut = useCallback(
    (options: { silent?: boolean } = {}) => {
      const active = sessionRef.current;
      void request("/auth/logout", { method: "POST" }).catch(() => {
        /* best effort */
      });
      const removal = active ? removeAccountSession(active.user.userId) : null;
      const activeSessionCleared = deactivateSession();

      if ((removal && !removal.ok) || !activeSessionCleared) {
        showToast(
          "로그아웃했지만 이 기기에 저장된 계정 세션을 지우지 못했습니다.",
          true,
        );
        return;
      }
      if (!options.silent) showToast("로그아웃했습니다.");
    },
    [deactivateSession, showToast],
  );

  // Keep the API client's token in sync, and let a 401 drop the session.
  useEffect(() => {
    const token = session?.accessToken ?? null;
    configureAuth(token, () => {
      const active = sessionRef.current;
      if (!active || active.accessToken !== token) return;

      const removal = removeAccountSession(active.user.userId);
      const activeSessionCleared = deactivateSession();
      showToast(
        removal.ok && activeSessionCleared
          ? "로그인이 만료되었습니다. 비밀번호로 다시 로그인해 주세요."
          : "로그인이 만료됐지만 이 기기의 저장 정보를 완전히 지우지 못했습니다.",
        true,
      );
    });
  }, [deactivateSession, session, showToast]);

  const reloadPersonaCatalog = useCallback(async () => {
    setPersonaCatalogLoading(true);
    try {
      const payload = await request<unknown>("/personas");
      const loaded = normalizePersonaCatalog(payload);
      if (
        !loaded.some((persona) => persona.kind === "preset") ||
        !loaded.some((persona) => persona.kind === "custom")
      ) {
        throw new Error("페르소나 카탈로그가 올바르지 않습니다.");
      }
      cachePersonaCatalog(loaded);
      setPersonas(loaded);
    } catch (error) {
      // 정상 캐시가 있으면 화면은 계속 동작하므로 불필요한 오류 토스트를 띄우지 않는다.
      const cached = personasRef.current;
      if (
        !cached.some((persona) => persona.kind === "preset") ||
        !cached.some((persona) => persona.kind === "custom")
      ) {
        reportError(error);
      }
    } finally {
      setPersonaCatalogLoading(false);
    }
  }, [reportError]);

  // 프리셋과 custom 선택 항목은 백엔드 카탈로그가 단일 소스다. 마지막 정상 응답은 캐시해 순차 배포나
  // 일시적인 네트워크 오류에도 설정 화면의 기존 목록을 유지한다.
  useEffect(() => {
    void reloadPersonaCatalog();
  }, [reloadPersonaCatalog]);

  const loadPostsForSession = useCallback(
    async (active: AuthSession, epoch: number) => {
      const requestId = ++postsRequestRef.current;
      const versionsAtStart = new Map(
        postsRef.current.map((item) => [item.postId, item.version]),
      );
      setPostsLoading(true);
      const isCurrent = () =>
        requestId === postsRequestRef.current &&
        epoch === authEpochRef.current &&
        sessionRef.current?.user.userId === active.user.userId;

      // 두 조회를 **동시에 띄운다.** 서로 필요로 하는 것이 없는데도 순서대로 기다리고
      // 있어서, 목록 화면이 뜨는 데 왕복 두 번이 그대로 더해졌다(2026-08-06).
      // 예약 목록은 아래에서 따로 받는다 — 그쪽이 실패해도 목록은 그대로 떠야 한다.
      const scheduledRequest = request<ScheduledJobList | null>(
        "/scheduled/naver/jobs",
      ).catch((error) => {
        console.error(error);
        return null;
      });

      try {
        const loaded = await request<Array<BlogTaskListItem | BlogTask>>(
          "/posts?view=summary",
        );
        if (!isCurrent()) return;

        // During a rolling deploy an older API may ignore `view=summary` and return
        // full BlogTask objects. Keep the screen compatible while still preferring
        // the lightweight contract as soon as the API supports it.
        const next = (loaded ?? []).map((item) =>
          "hasFinalPost" in item ? item : listItemFromTask(item),
        );
        setPosts((current) => {
          const currentById = new Map(current.map((item) => [item.postId, item]));
          const loadedIds = new Set(next.map((item) => item.postId));
          const reconciled = next
            // DELETE can complete after Mongo produced this snapshot. Do not revive
            // an item that existed when the request began but was removed meanwhile.
            .filter(
              (serverItem) =>
                !versionsAtStart.has(serverItem.postId) || currentById.has(serverItem.postId),
            )
            .map((serverItem) => {
              const local = currentById.get(serverItem.postId);
              return local && local.version >= serverItem.version ? local : serverItem;
            });
          // A task created or advanced while this GET was in flight must not vanish
          // merely because the response is an older snapshot.
          const changedWhileLoading = current.filter((item) => {
            if (loadedIds.has(item.postId)) return false;
            const previousVersion = versionsAtStart.get(item.postId);
            return previousVersion === undefined || item.version > previousVersion;
          });
          return [...changedWhileLoading, ...reconciled];
        });
      } catch (error) {
        if (isCurrent()) reportError(error);
      } finally {
        if (isCurrent()) setPostsLoading(false);
      }

      /**
       * **어느 글이 예약 포스팅에서 나온 것인가.**
       *
       * 두 기능은 하는 일이 다르다. 새 글 작성은 글 하나를 손으로 끌고 가는 자리이고,
       * 예약 포스팅은 소재 여러 개를 걸어 두면 서버가 순서대로 만들어 올리는 자리다.
       * 그래서 예약이 만들고 있는 글의 카드에서 '생성 진행 보기'를 누르면 새 글 작성의
       * 3단계가 아니라 **예약의 작업 큐**로 가야 한다(2026-08-06 사용자 요청).
       *
       * 판단은 예약 작업이 가리키는 postId로 한다 — 그것이 유일하게 정확한 근거다.
       * (글의 '목적' 문자열로도 짐작할 수 있지만, 그 값은 원고 방향을 정하는 값이라
       * 언제든 바뀔 수 있어 화면 이동을 걸어 둘 근거가 못 된다.)
       *
       * 실패해도 조용히 넘어간다. 못 읽으면 표시가 빠질 뿐 목록 자체는 멀쩡하다.
       */
      const list = await scheduledRequest;
      if (!isCurrent()) return;
      const ids = new Set<string>();
      for (const item of list?.items ?? []) {
        if (item.job.postId) ids.add(item.job.postId);
      }
      setScheduledPostIds(ids);
    },
    [reportError],
  );

  const reloadPosts = useCallback(async () => {
    if (!session || renderedAuthEpoch !== authEpochRef.current) return;
    await loadPostsForSession(session, renderedAuthEpoch);
  }, [loadPostsForSession, renderedAuthEpoch, session]);

  /**
   * Deletes posts and drops them from the list. Each DELETE stands alone (the
   * server has no bulk route), so one failing does not sink the rest — only the
   * ones the server actually removed leave the list, and a partial failure says so.
   */
  const deletePosts = useCallback(
    async (postIds: string[]) => {
      if (!postIds.length || renderedAuthEpoch !== authEpochRef.current) return;
      const epoch = renderedAuthEpoch;

      const results = await Promise.allSettled(
        postIds.map((id) => request<void>(`/posts/${id}`, { method: "DELETE" })),
      );
      if (epoch !== authEpochRef.current) return;

      const deleted = new Set(
        postIds.filter((_, index) => results[index].status === "fulfilled"),
      );

      if (deleted.size) {
        setPosts((current) => current.filter((post) => !deleted.has(post.postId)));
        deleted.forEach((postId) => {
          followingRef.current.delete(postId);
          postDetailsRef.current.delete(postId);
        });
        // The wizard may be open on a post that just vanished — send it back to a
        // blank start rather than leave it editing a task the server no longer has.
        const openPostId = activePostIdRef.current;
        if (openPostId && deleted.has(openPostId)) {
          taskRef.current = null;
          setTaskState(null);
          activePostIdRef.current = null;
          setActivePostIdState(null);
          setPostLoading(false);
          setStepState(0);
          if (routeRef.current === "write") location.hash = writeHash(null);
        }
      }

      const failed = postIds.length - deleted.size;
      if (failed === 0) {
        showToast(`글 ${deleted.size}개를 삭제했습니다.`);
      } else if (deleted.size === 0) {
        showToast("글을 삭제하지 못했습니다.", true);
      } else {
        showToast(`${deleted.size}개는 삭제하고 ${failed}개는 실패했습니다.`, true);
      }
    },
    [renderedAuthEpoch, showToast],
  );

  const loadUserData = useCallback(
    async (active: AuthSession) => {
      const epoch = authEpochRef.current;
      const settingsRequest = (async () => {
        try {
          const loaded = await request<UserSettings>(`/users/${active.user.userId}/settings`);
          if (epoch !== authEpochRef.current) return;
          setSettingsState(loaded);
        } catch {
          if (epoch !== authEpochRef.current) return;
          // 404 simply means the user has not saved settings yet.
          setSettingsState(null);
        }
      })();

      // 설정과 목록은 서로 의존하지 않는다. 각각 끝나는 즉시 화면에 반영한다.
      await Promise.all([settingsRequest, loadPostsForSession(active, epoch)]);
    },
    [loadPostsForSession],
  );

  // Onboarding only opens on a first visit with no settings saved, and is marked
  // seen the moment it displays so a refresh does not re-show it.
  const openOnboardingIfFirstVisit = useCallback((active: AuthSession, saved: UserSettings | null) => {
    const key = onboardingSeenKey(active.user.userId);
    if (!key || saved || hasSeenOnboarding(key)) return;
    rememberOnboardingSeen(key);
    setOnboardingOpen(true);
  }, []);

  const signIn = useCallback(
    async (next: AuthSession) => {
      authEpochRef.current += 1;
      const epoch = authEpochRef.current;
      resetUserWorkspace();

      const activeSessionPersisted = storeSession(next);
      const usedAt = new Date();
      const accountSessionPersisted = saveAccountSession(next, usedAt);
      saveRememberedAccount(
        {
          userId: next.user.userId,
          email: next.user.email,
          displayName: next.user.nickname,
          profileImage: null,
        },
        usedAt,
      );
      sessionRef.current = next;
      setSession(next);
      configureAuth(next.accessToken, () => signOut({ silent: true }));
      // We are about to load everything ourselves; stop the restore-on-boot effect
      // from firing for this session and fetching it all a second time.
      bootstrapped.current = true;

      const settingsRequest = request<UserSettings>(`/users/${next.user.userId}/settings`).catch(
        () => null,
      );
      // 설정 응답을 기다리는 동안 목록 요청과 JSON 파싱을 바로 시작한다.
      const postsRequest = loadPostsForSession(next, epoch);
      const saved = await settingsRequest;
      const persisted = activeSessionPersisted && accountSessionPersisted.ok;
      if (epoch !== authEpochRef.current) {
        return { activated: false, persisted };
      }
      setSettingsState(saved);
      openOnboardingIfFirstVisit(next, saved);
      await postsRequest;
      if (epoch !== authEpochRef.current) return { activated: false, persisted };
      return { activated: true, persisted };
    },
    [
      openOnboardingIfFirstVisit,
      loadPostsForSession,
      resetUserWorkspace,
      signOut,
    ],
  );

  // Migrate the already active legacy single session into the account vault.
  // This keeps existing users signed in and makes that account available in the
  // new chooser without changing the legacy active-session key.
  useEffect(() => {
    if (!session) return;
    const usedAt = new Date();
    saveAccountSession(session, usedAt);
    saveRememberedAccount(
      {
        userId: session.user.userId,
        email: session.user.email,
        displayName: session.user.nickname,
        profileImage: null,
      },
      usedAt,
    );
  }, [session]);

  // Restore a stored session on first paint.
  useEffect(() => {
    if (bootstrapped.current || !session) return;
    bootstrapped.current = true;
    void loadUserData(session);
  }, [loadUserData, session]);

  // Hash routing. 작업실 경로는 #/write/{postId}까지 갖는다 — 새로고침하거나 뒤로
  // 돌아왔을 때 어느 글이었는지가 주소에 남아 있어야 서버 상태에서 정확히 복구된다.
  useEffect(() => {
    const apply = () => {
      const path = location.hash.replace(/^#/, "");
      const writePost = /^\/write\/(.+)$/.exec(path);
      if (writePost) {
        setRouteState("write");
        const postId = decodeURIComponent(writePost[1]);
        // 이미 그 글을 보고 있으면(openPost가 방금 주소를 바꾼 경우) 다시 불러오지 않는다.
        if (sessionRef.current && activePostIdRef.current !== postId) {
          void openPostRef.current?.(postId);
        }
        return;
      }

      // #/brand는 2026-08-11에 없앴다. 브랜드는 새 글 작성 소재 단계의 선택 항목이 됐고
      // 자료 편집은 그 자리에서 모달로 연다. 옛 주소(북마크·뒤로가기)로 들어오면 홈이
      // 아니라 **새 글 작성**으로 보낸다 — 그게 그 주소가 하려던 일이다.
      // 주소도 함께 고쳐 둔다. 그대로 두면 새로고침·뒤로가기 때마다 없어진 경로를 다시
      // 밟고, 화면과 주소가 계속 어긋난 채로 남는다.
      if (/^\/brand(\/.*)?$/.test(path)) {
        location.replace("#/write");
        return;
      }

      // 예약 포스팅도 한 경로 아래 여러 탭을 둔다(#/scheduled, #/scheduled/queue).
      // 어느 탭인지는 화면이 주소에서 읽는다 — 내 글 목록에서 '예약 작업 보기'를
      // 눌렀을 때 곧바로 작업 큐가 열려야 하기 때문이다(2026-08-06 사용자 요청).
      if (/^\/scheduled(\/.*)?$/.test(path)) {
        setRouteState("scheduled");
        return;
      }

      const routes: Record<string, Route> = {
        "": "home",
        "/": "home",
        "/write": "write",
        // 「자동 포스팅」 — 여러 소재를 한 번에 거는 화면(2026-08-12). 걸어 둔 뒤의
        // 진행은 #/scheduled(작업 관리)가 보여 준다. 두 화면은 하는 일이 다르다.
        "/bulk": "bulk",
        "/scheduled": "scheduled",
        "/posts": "posts",
        "/settings": "settings",
      };
      const next = routes[path] ?? "home";
      setRouteState(next);
      if (next === "write" && !taskRef.current) setStepState(0);
    };
    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, []);

  const setRoute = useCallback((next: Route) => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    // The state is set here rather than left to the hashchange listener, because
    // assigning a hash that is already there fires no hashchange at all. That is
    // the bug where 새 글 쓰기 did nothing right after logging in: signing out
    // leaves the hash on #/write while resetting the route to home, so the two
    // disagree and clicking 새 글 쓰기 writes a hash identical to the current one.
    // The listener stays for the back button and hand-edited URLs.
    setRouteState(next);
    if (next === "home") {
      location.hash = "#/";
      return;
    }
    // 작업실로 돌아갈 때는 열려 있던 글을 주소에 유지한다.
    location.hash = next === "write" ? writeHash(activePostIdRef.current) : `#/${next}`;
  }, [renderedAuthEpoch]);

  const setStep = useCallback((next: number) => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    const limit = maxStep(taskRef.current);
    // **원고 생성 단계에 닿고 나면** 그 앞 단계로는 돌아가지 못한다. 원고는 그때 고른
    // 제목과 방향으로 쓰이고 있어서, 뒤로 가 그것을 바꾸면 화면과 실제 원고가 다른 말을
    // 하게 된다. limit은 task 상태를 따라 늘어나기만 하므로 한 번 넘으면 계속 그렇다.
    //
    // **이 값은 숫자로 적으면 안 된다.** 예전에는 `2`로 박혀 있었는데, 2026-08-06에
    // 검증이 팝업에서 자기 단계(VERIFY=2)로 독립하면서 원고가 3으로 밀렸다. 상수는 그대로
    // 남아 검증 단계에 서기만 하면 바닥이 2가 됐고, 그래서 '제목 다시 고르기'가 눌러도
    // 아무 일도 하지 않았다(2026-08-07 신고). 단계 번호가 또 바뀌어도 따라오도록 이름으로 쓴다.
    const floor = limit >= WRITE_STEP.DRAFT ? WRITE_STEP.DRAFT : WRITE_STEP.TOPIC;
    setStepState(Math.max(floor, Math.min(next, limit)));
  }, [renderedAuthEpoch]);

  /** 목록에 이 글의 최신본을 반영한다. postId로 자리를 찾으므로 글끼리 섞이지 않는다. */
  const mergePost = useCallback((next: BlogTask) => {
    if (isCompleteBlogTask(next)) rememberPostDetail(postDetailsRef.current, next);
    const item = listItemFromTask(next);
    setPosts((current) => {
      const index = current.findIndex((item) => item.postId === next.postId);
      if (index < 0) return [item, ...current];
      const copy = [...current];
      copy[index] = item;
      return copy;
    });
  }, []);

  /**
   * 이 글을 작업실의 현재 글로 삼는다.
   *
   * 지금 다른 글이 열려 있는데 다른 postId가 들어오면 화면은 건드리지 않고 목록만
   * 갱신한다. 글 A의 원고 생성을 따라가는 폴링이나, A로 날린 요청의 늦은 응답이
   * 방금 연 글 B의 화면을 덮어쓰던 것이 사용자가 겪은 문제의 원인이다.
   */
  const setTask = useCallback((next: BlogTask) => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    mergePost(next);

    const openPostId = activePostIdRef.current;
    if (openPostId !== null && openPostId !== next.postId) return;
    // '새 글로 시작'으로 내보낸 글의 늦은 응답이 빈 작업실을 다시 채우면 안 된다.
    if (openPostId === null && discardedPostIdsRef.current.has(next.postId)) return;

    // Callers routinely do setTask(t); setStep(n) in one go. setStep clamps against
    // taskRef, which React would not refresh until the next render — so the new
    // task has to land in the ref now, or the step gets clamped to the old task's
    // limit and the wizard silently refuses to advance.
    taskRef.current = next;
    setTaskState(next);
    if (openPostId === null) {
      activePostIdRef.current = next.postId;
      setActivePostIdState(next.postId);
      // 새로고침·뒤로가기가 같은 글로 돌아오도록 주소에 postId를 남긴다.
      const hash = writeHash(next.postId);
      if (routeRef.current === "write" && location.hash !== hash) location.hash = hash;
    }
  }, [mergePost, renderedAuthEpoch]);

  /**
   * 가벼운 status 응답은 원고 본문·base64 이미지를 싣지 않는다. 기존 상세 객체를
   * 부분 갱신해 진행률만 즉시 보여 주고, 전체 객체는 작업이 끝났을 때 한 번만 받는다.
   */
  const applyTaskStatus = useCallback(
    (snapshot: BlogTaskStatusSnapshot) => {
      if (renderedAuthEpoch !== authEpochRef.current) return;

      setPosts((current) =>
        current.map((item) =>
          item.postId === snapshot.postId && snapshot.version >= item.version
            ? { ...item, status: snapshot.status, version: snapshot.version }
            : item,
        ),
      );

      const cached = postDetailsRef.current.get(snapshot.postId);
      if (cached && snapshot.version >= cached.version) {
        rememberPostDetail(postDetailsRef.current, {
          ...cached,
          status: snapshot.status,
          version: snapshot.version,
          progress: snapshot.progress,
          // 작업 현황 로그는 status에만 실려 온다. 스냅샷이 빈 손으로 오면(완료 직후 등)
          // 마지막으로 본 로그를 유지한다 — 화면이 깜빡 비었다 차는 것을 막는다.
          activityLog: snapshot.activityLog?.length
            ? snapshot.activityLog
            : cached.activityLog,
        });
      }

      const current = taskRef.current;
      if (
        current?.postId !== snapshot.postId ||
        snapshot.version < current.version
      ) {
        return;
      }
      const next: BlogTask = {
        ...current,
        status: snapshot.status,
        version: snapshot.version,
        progress: snapshot.progress,
        activityLog: snapshot.activityLog?.length
          ? snapshot.activityLog
          : current.activityLog,
      };
      taskRef.current = next;
      setTaskState(next);
    },
    [renderedAuthEpoch],
  );

  /**
   * Follows a post while the server works on it, and resolves with the settled one.
   *
   * M3 and M4 answer 202 and run in the background, so the only way to learn what
   * happened — and how far along it is — is to ask.
   *
   * A missing `progress` is not enough to call the job finished. The first 202
   * response intentionally hides progress, and a poll can also land in a tiny
   * window before the reporter writes its first label. M4 is settled only after it
   * leaves GENERATING; M3 is settled once the validation result exists.
   */
  const followTask = useCallback(
    (postId: string): Promise<BlogTask | null> => {
      // 같은 글에 폴링 루프가 두 개 붙지 않게 한다. 생성 중인 글로 다시 들어오거나
      // 새로고침해도 이미 돌고 있는 추적을 그대로 넘겨준다.
      const running = followingRef.current.get(postId);
      if (running) return running;

      // 화면을 어느 글에 두고 있든 폴링은 계속 돈다 — 다른 글을 열어도 이 글의 생성은
      // 백그라운드에서 이어진다. 결과가 화면에 반영될지는 setTask가 판단한다.
      const tracked: Promise<BlogTask | null> = pollTaskUntilSettled(postId, setTask, {
        onStatus: applyTaskStatus,
        shouldContinue: () =>
          renderedAuthEpoch === authEpochRef.current &&
          sessionRef.current !== null,
      }).finally(() => {
        if (followingRef.current.get(postId) === tracked) {
          followingRef.current.delete(postId);
        }
      });
      followingRef.current.set(postId, tracked);
      return tracked;
    },
    [applyTaskStatus, renderedAuthEpoch, setTask],
  );

  /** 서버가 준 글로 작업실을 채운다. 단계는 그 글에 저장된 진행 상태에서만 나온다. */
  /**
   * 원고 단계에 들어갔을 때 **저절로 생성을 시작해도 되는가**(2026-08-12 사용자 신고).
   *
   * 검증에서 방향을 확인하고 넘어온 경우에만 참이다. 목록에서 반쯤 만든 글을 열면
   * 거짓이라, 화면이 '원고 생성 시작' 버튼을 보여 주고 사람이 누를 때까지 기다린다.
   *
   * 왜 필요한가: 서버가 꺼졌다 켜진 뒤 그 글을 열면 **중단된 자리에서 저절로 이어서**
   * 돌았다. 사용자가 새 글을 쓰는 동안 옛 글의 원고 생성이 함께 도는 일이 생긴다.
   */
  const [draftAutoStart, setDraftAutoStartState] = useState(false);
  const [draftRounds, setDraftRoundsState] = useState<DraftRound[]>([]);
  const setDraftRounds = useCallback((rounds: DraftRound[]) => {
    // 브라우저에도 함께 남긴다 — 새로고침해도 앞 편에서 고른 것이 그대로 있어야 한다.
    saveDraftRounds(activePostIdRef.current, rounds);
    setDraftRoundsState(rounds);
  }, []);
  const setDraftAutoStart = useCallback((value: boolean) => {
    setDraftAutoStartState(value);
  }, []);

  const applyOpenedPost = useCallback(
    (opened: BlogTask) => {
      taskRef.current = opened;
      setTaskState(opened);
      setRecommendation(null);
      // 목록을 비웠으면 그 목록을 설명하던 보기 방식도 함께 되돌린다. 둘은 한 사실의
      // 두 면이라, 한쪽만 남으면 탭과 카드가 서로 다른 모드를 말하게 된다.
      setTrendModeState(DEFAULT_TREND_MODE);
      setSelectedIds(opened.trendSelection?.selectedTrendKeywordIds ?? []);
      setTouched(false);
      // 목록에서 연 글이다 — 사람이 '원고 생성 시작'을 누를 때까지 기다린다.
      setDraftAutoStartState(false);
      // 이 글에서 편별로 고르던 것이 있으면 되살린다(새로고침·뒤로가기). 없으면 빈 배열이라,
      // 다른 글의 라운드가 딸려 오지 않는다.
      setDraftRoundsState(loadDraftRounds(opened.postId));
      setStepState(resumeStep(opened));
      mergePost(opened);
    },
    [mergePost],
  );

  /**
   * 목록에서 고른 글을 연다.
   *
   * 이전에는 메모리에 있던 목록 사본을 그대로 썼다. 그 사본은 다른 화면에 있는 동안
   * 서버에서 벌어진 일(원고 생성 완료 등)을 모르므로, 이미 원고가 나온 글을 검증
   * 단계로 열어 버리는 일이 생겼다. 이제 postId로 서버에 다시 물어보고, 그 응답이
   * 도착했을 때도 여전히 이 글을 보고 있을 때만 화면에 반영한다.
   */
  const openPost = useCallback(
    async (postId: string) => {
      if (!postId || renderedAuthEpoch !== authEpochRef.current) return;
      const epoch = renderedAuthEpoch;

      // 이전 글의 화면 데이터를 먼저 끊는다. 새 글 데이터가 도착하기 전에 이전 글의
      // 소재·제목·원고가 잠깐이라도 보이면 안 된다.
      discardedPostIdsRef.current.delete(postId);
      activePostIdRef.current = postId;
      setActivePostIdState(postId);
      taskRef.current = null;
      setTaskState(null);
      setRecommendation(null);
      setTrendModeState(DEFAULT_TREND_MODE);
      setSelectedIds([]);
      setTouched(false);
      // 앞 글의 라운드를 끊는다. 이 글의 것은 applyOpenedPost가 되살린다 — 그 사이에
      // setDraftRounds가 불려도 남의 라운드가 이 글의 자리에 저장되지 않는다.
      setDraftRoundsState([]);
      setStepState(0);
      setPostLoading(true);
      setPostLoadError(null);
      setRouteState("write");
      const hash = writeHash(postId);
      if (location.hash !== hash) location.hash = hash;

      const stale = () =>
        epoch !== authEpochRef.current || activePostIdRef.current !== postId;

      try {
        const latest = await request<BlogTask>(`/posts/${encodeURIComponent(postId)}`);
        // 느린 응답이 그 사이에 연 다른 글을 덮어쓰지 않도록, 아직 이 글을 보고 있을
        // 때만 반영한다.
        if (stale()) return;
        applyOpenedPost(latest);
      } catch (error) {
        if (stale()) return;
        // summary에는 원고 본문이 없으므로 작업실에 적용하지 않는다. 같은 version의 전체
        // 상세를 이 세션에서 이미 받은 적이 있을 때만 안전한 오프라인 fallback이 된다.
        const listItem = postsRef.current.find((item) => item.postId === postId);
        const cached = postDetailsRef.current.get(postId);
        if (cached && listItem?.version === cached.version) applyOpenedPost(cached);
        else {
          // **빈 '새 글 작성'으로 떨어뜨리지 않는다.** 그냥 두면 task가 null인 채로
          // 남아 화면이 소재 입력 폼을 그리는데, 사용자는 '발행하기'를 눌렀을 뿐이고
          // 그 글은 멀쩡히 있다(2026-08-06 신고 — Mongo 조회가 20초 만에 시간 초과해
          // 500이 났다). 실패했다는 사실을 남겨 화면이 다시 시도할 길을 준다.
          setPostLoadError(friendlyError(error));
          reportError(error);
        }
      } finally {
        if (!stale()) setPostLoading(false);
      }
    },
    [applyOpenedPost, renderedAuthEpoch, reportError],
  );
  // 주소가 #/write/{postId}로 바뀌었을 때(새로고침·뒤로가기) 라우팅 effect가 부른다.
  openPostRef.current = openPost;

  const restartWriting = useCallback(() => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    const leaving = activePostIdRef.current;
    if (leaving) discardedPostIdsRef.current.add(leaving);
    setDraftAutoStartState(false);
    // 새 글은 1편째부터다. 옛 글의 라운드를 들고 가면 첫 편이 2편째로 기록된다.
    setDraftRoundsState([]);
    activePostIdRef.current = null;
    setActivePostIdState(null);
    taskRef.current = null;
    setTaskState(null);
    setPostLoading(false);
    // 새 글로 시작하면 옛 글을 못 열었다는 사실은 더 이상 이 화면의 이야기가 아니다.
    setPostLoadError(null);
    setRecommendation(null);
    setTrendModeState(DEFAULT_TREND_MODE);
    setSelectedIds([]);
    setTouched(false);
    setStepState(0);
    if (routeRef.current === "write" && location.hash !== writeHash(null)) {
      location.hash = writeHash(null);
    }
  }, [renderedAuthEpoch]);

  /** Swaps the title list without touching the collected keywords, so pressing
      제목 추천 does not re-collect the keyword cards underneath it. */
  const setTopicCandidates = useCallback((candidates: TopicCandidate[], generatedAt: string) => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    setRecommendation((current) =>
      current ? { ...current, topicCandidates: candidates, generatedAt } : current,
    );
  }, [renderedAuthEpoch]);

  const selectTrendKeyword = useCallback((id: string) => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    setSelectedIds([id]);
    setTouched(true);
  }, [renderedAuthEpoch]);

  const setSelectedTrendKeywordIds = useCallback((ids: string[], touched: boolean) => {
    if (renderedAuthEpoch !== authEpochRef.current) return;
    setSelectedIds(ids);
    setTouched(touched);
  }, [renderedAuthEpoch]);

  const setSettings = useCallback(
    (next: UserSettings) => {
      if (renderedAuthEpoch !== authEpochRef.current) return;
      setSettingsState(next);
    },
    [renderedAuthEpoch],
  );

  const setRecommendationForActiveAccount = useCallback(
    (next: TrendRecommendation | null) => {
      if (renderedAuthEpoch !== authEpochRef.current) return;
      setRecommendation(next);
    },
    [renderedAuthEpoch],
  );

  const setTrendMode = useCallback(
    (next: TrendMode) => {
      if (renderedAuthEpoch !== authEpochRef.current) return;
      setTrendModeState(next);
    },
    [renderedAuthEpoch],
  );

  const markOnboardingSeen = useCallback(() => {
    const key = onboardingSeenKey(session?.user.userId);
    if (key) rememberOnboardingSeen(key);
    setOnboardingOpen(false);
  }, [session]);

  const store = useMemo<Store>(
    () => ({
      session,
      settings,
      personas,
      personaCatalogLoading,
      posts,
      postsLoading,
      scheduledPostIds,
      task,
      activePostId,
      postLoading,
      postLoadError,
      recommendation,
      trendMode,
      selectedTrendKeywordIds,
      trendKeywordSelectionTouched,
      step,
      route,
      onboardingOpen,
      accountChooserOpen,
      toast,
      postsFilter,

      signIn,
      signOut,
      openAccountChooser,
      closeAccountChooser,
      showToast,
      reportError,

      setRoute,
      setStep,
      draftAutoStart,
      setDraftAutoStart,
      draftRounds,
      setDraftRounds,
      setPostsFilter,
      setTask,
      openPost,
      restartWriting,

      setSettings,
      setRecommendation: setRecommendationForActiveAccount,
      setTrendMode,
      setTopicCandidates,
      selectTrendKeyword,
      setSelectedTrendKeywordIds,

      dismissOnboarding: () => setOnboardingOpen(false),
      markOnboardingSeen,

      followTask,

      reloadPosts,
      reloadPersonaCatalog,
      deletePosts,
    }),
    [
      deletePosts,
      accountChooserOpen,
      activePostId,
      closeAccountChooser,
      followTask,
      markOnboardingSeen,
      onboardingOpen,
      openAccountChooser,
      openPost,
      personaCatalogLoading,
      personas,
      postLoading,
      postLoadError,
      postsLoading,
      postsFilter,
      posts,
      recommendation,
      reloadPosts,
      reloadPersonaCatalog,
      reportError,
      restartWriting,
      route,
      selectTrendKeyword,
      selectedTrendKeywordIds,
      session,
      setRoute,
      setSelectedTrendKeywordIds,
      setStep,
      setPostsFilter,
      setTask,
      setSettings,
      setRecommendationForActiveAccount,
      setTrendMode,
      settings,
      showToast,
      signIn,
      signOut,
      step,
      task,
      toast,
      setTopicCandidates,
      trendKeywordSelectionTouched,
      trendMode,
    ],
  );

  return <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;
}
