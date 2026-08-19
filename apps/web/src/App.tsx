import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useRef,
  type ComponentType,
  type MouseEvent,
  type ReactNode,
} from "react";
import { AuthView } from "./components/AuthView";
import { HomeView } from "./components/HomeView";
import { OnboardingModal } from "./components/OnboardingModal";
import { Toast } from "./components/Toast";
import { useStore, type Route } from "./store";
import { resumeStep, writingTabAction } from "./resume";
import { taskTitle } from "./components/PostCard";
import { STATUS_LABELS, STEPS } from "./constants";

/** 조각을 못 받아 새로고침한 적이 있는가. 되풀이되는 새로고침을 막는 표시다. */
const CHUNK_RELOAD_KEY = "blog-it:chunk-reload";

/**
 * 화면 조각(청크)을 불러온다. **한 번 실패하면 새로고침해서 다시 받는다.**
 *
 * 이 앱은 빌드된 파일을 그대로 내보낸다. 다시 빌드하면 조각의 파일 이름이 바뀌고 예전
 * 파일은 사라지므로, 열어 두었던 탭은 **없는 주소**를 부른다. `lazy`는 그 실패를 조용히
 * 삼켜(아무도 잡지 않으면) 화면이 "화면을 불러오는 중입니다."에 영영 멈춘다 —
 * 실제로 설정 화면이 그렇게 멈췄다(2026-08-06 실사용).
 *
 * 새 index.html을 받아 오면 그대로 낫는 문제라 한 번은 스스로 새로고침한다. 그래도
 * 안 되면(정말 파일이 없거나 네트워크가 끊겼다) 아래 ScreenError가 이유를 보여 준다.
 */
function lazyScreen<T extends ComponentType<object>>(
  load: () => Promise<{ default: T }>,
) {
  return lazy(async () => {
    try {
      const loaded = await load();
      sessionStorage.removeItem(CHUNK_RELOAD_KEY);
      return loaded;
    } catch (error) {
      if (sessionStorage.getItem(CHUNK_RELOAD_KEY) === "1") throw error;
      sessionStorage.setItem(CHUNK_RELOAD_KEY, "1");
      window.location.reload();
      // 새로고침이 시작됐다. 이 약속은 아무도 기다리지 않는다.
      return new Promise<{ default: T }>(() => {});
    }
  });
}

const PostsView = lazyScreen(() =>
  import("./components/PostsView").then((module) => ({
    default: module.PostsView,
  })),
);
const SettingsView = lazyScreen(() =>
  import("./components/SettingsView").then((module) => ({
    default: module.SettingsView,
  })),
);
const WriteView = lazyScreen(() =>
  import("./components/write/WriteView").then((module) => ({
    default: module.WriteView,
  })),
);
const ScheduledView = lazyScreen(() =>
  import("./components/scheduled/ScheduledView").then((module) => ({
    default: module.ScheduledView,
  })),
);
const BulkScheduleView = lazyScreen(() =>
  import("./components/scheduled/BulkScheduleView").then((module) => ({
    default: module.BulkScheduleView,
  })),
);

/**
 * 화면을 끝내 못 띄웠을 때 그 자리에 이유를 적는다.
 *
 * 새로고침 한 번으로 낫는 문제(빌드가 바뀌어 조각 주소가 달라진 경우)는 `lazyScreen`이
 * 이미 처리한다. 여기까지 왔다는 것은 그것으로도 안 됐다는 뜻이라, 흰 화면 대신 무엇을
 * 하면 되는지 보여 준다. 화면을 옮기면(라우트가 바뀌면) 다시 시도한다(`key={route}`).
 */
class ScreenError extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    // 콘솔에는 원래 오류를 그대로 남긴다 — 화면 문구는 사용자용이고, 원인은 여기 있다.
    console.error(error);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="loading-state">
        <p>
          화면을 불러오지 못했습니다. 새로고침해도 그대로면 서버가 실행 중인지
          확인해 주세요.
        </p>
        <button
          className="button"
          type="button"
          onClick={() => window.location.reload()}
        >
          다시 시도
        </button>
      </div>
    );
  }
}

const NAV: { route: Route; href: string; label: string }[] = [
  { route: "home", href: "#/", label: "홈" },
  { route: "write", href: "#/write", label: "새 글 작성" },
  // 여러 소재를 한 번에 걸고 손대지 않는 자리(2026-08-12). 새 글 작성은 한 편을 사용자가
  // 끌고 가고, 여기는 소재·플랫폼만 정하면 서버가 발행까지 간다.
  //
  // 이름이 '예약'이 아니라 **'자동'** 인 이유: 예약은 두 탭 모두의 선택 항목이다(새 글
  // 작성도 작업 시각을 적으면 걸리고, 이 탭도 시각을 비우면 바로 시작한다). 두 탭을
  // 가르는 것은 **제목·방향까지 서버가 정하는가**이고, 이름은 그것을 말해야 한다.
  { route: "bulk", href: "#/bulk", label: "자동 포스팅" },
  // 이 탭은 예약을 '거는' 곳이 아니라 걸어 둔 작업의 큐·진행·발행 내역을 '보는'
  // 곳이다(2026-08-11에 입력 걸음을 걷어냈다). 이름도 하는 일에 맞춘다.
  { route: "scheduled", href: "#/scheduled", label: "예약작업 관리" },
  { route: "posts", href: "#/posts", label: "내 글 목록" },
  { route: "settings", href: "#/settings", label: "설정" },
];

function NavIcon({ route }: { route: Route }) {
  if (route === "write") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="m14.7 5.3 4 4M4 20l3.8-.8L19 8a2.8 2.8 0 0 0-4-4L3.8 15.2 3 19.8 4 20Z" />
      </svg>
    );
  }
  if (route === "bulk") {
    // 줄 세 개를 쌓은 모양 — 소재를 여러 개 걸어 두는 자리라는 뜻이다. 달력(작업 관리)과
    // 한눈에 구분되어야 한다.
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <rect x="3.5" y="4.5" width="17" height="4.5" rx="1.5" />
        <rect x="3.5" y="11" width="17" height="4.5" rx="1.5" />
        <path d="M3.5 19.5h11" />
      </svg>
    );
  }
  if (route === "scheduled") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" />
        <path d="M3.5 9.5h17M8 3.5v3M16 3.5v3" />
        <path d="M12 12.5v2.5l1.8 1.1" />
      </svg>
    );
  }
  if (route === "posts") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <rect x="4" y="3.5" width="16" height="17" rx="2.5" />
        <path d="M8 8h8M8 12h8M8 16h5" />
      </svg>
    );
  }
  if (route === "settings") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="3" />
        <path d="M19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.5 1a7 7 0 0 0-1.7-1L14.3 3h-4.6L9.3 6a7 7 0 0 0-1.7 1L5 6 3 9.4 5.1 11a7 7 0 0 0 0 2L3 14.6 5 18l2.6-1a7 7 0 0 0 1.7 1l.4 3h4.6l.4-3a7 7 0 0 0 1.7-1l2.5 1 2-3.4-2-1.6a7 7 0 0 0 .1-1Z" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="m3.5 10 8.5-7 8.5 7" />
      <path d="M5.5 9v11h13V9M9.5 20v-6h5v6" />
    </svg>
  );
}

function pageHeader(
  route: Route,
  store: ReturnType<typeof useStore>,
): [string, string] {
  const { task, step, session, postLoading, postLoadError, activePostId } = store;

  if (route === "write") {
    if (postLoading && !task)
      return ["글 여는 중", "저장된 진행 상태를 불러오고 있습니다."];
    // 열려던 글을 못 불러온 것과, 새 글을 시작한 것은 다르다. 머리글까지 '새 글 작성'
    // 이라고 말하면 사용자는 그 글이 사라진 줄 안다(2026-08-06 신고).
    if (!task && activePostId && postLoadError)
      return ["글을 열지 못했습니다", "글은 그대로 있습니다. 다시 시도해 주세요."];
    if (!task)
      return ["새 글 작성", "아이디어 하나로 멋진 글을 시작해 보세요."];
    const status = STATUS_LABELS[task.status]?.text ?? task.status;
    return [taskTitle(task), `${step + 1}/${STEPS.length} 단계 · ${status}`];
  }
  if (route === "bulk")
    return ["자동 포스팅", "소재와 플랫폼만 정하면 발행까지 알아서 진행합니다."];
  if (route === "scheduled")
    return ["예약작업 관리", "걸어 둔 작업의 진행 상황과 발행 결과를 봅니다."];
  if (route === "posts")
    return ["내 글 목록", "지금까지 만든 글을 한곳에서 관리하세요."];
  if (route === "settings")
    return ["설정", "새 글에 적용할 기본값을 정할 수 있어요."];
  return [
    "홈",
    `${session?.user.nickname || session?.user.email}님, 오늘은 어떤 글을 쓸까요?`,
  ];
}

function AccountChooserOverlay({ onClose }: { onClose: () => void }) {
  const overlayRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const app = document.querySelector<HTMLElement>(".app");
    if (app) app.inert = true;
    document.body.classList.add("account-chooser-open");

    const handleKeyDown = (event: KeyboardEvent) => {
      // The nested account-removal dialog owns Escape and focus while it is open.
      if (document.querySelector('[role="alertdialog"]')) return;

      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = [
        ...(overlayRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
        ) ?? []),
      ];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last ||
          !overlayRef.current?.contains(document.activeElement))
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      if (app) app.inert = false;
      document.body.classList.remove("account-chooser-open");
      window.removeEventListener("keydown", handleKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [onClose]);

  return (
    <div
      ref={overlayRef}
      className="account-chooser-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="authTitle"
    >
      <button
        className="account-chooser-close"
        type="button"
        aria-label="계정 선택 닫기"
        onClick={onClose}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="m6 6 12 12M18 6 6 18" />
        </svg>
      </button>
      <AuthView />
    </div>
  );
}

export function App() {
  const store = useStore();
  const {
    accountChooserOpen,
    activePostId,
    closeAccountChooser,
    session,
    route,
    posts,
    task,
    openAccountChooser,
    signOut,
    restartWriting,
    setRoute,
    setStep,
  } = store;

  const signedIn = Boolean(session);

  /** 「새 글 작성」 탭. 무엇을 할지는 resume.writingTabAction이 정한다. */
  function openWritingTab(event: MouseEvent<HTMLAnchorElement>) {
    if (writingTabAction(task) === "restart") {
      restartWriting();
      return;
    }
    // 기본 이동(#/write)을 막는다. 그대로 두면 주소에서 postId가 빠져 새로고침·뒤로가기가
    // 그 글로 돌아오지 못한다 — setRoute가 열려 있던 글을 주소에 유지한다.
    event.preventDefault();
    setStep(resumeStep(task));
    setRoute("write");
  }

  const [title, subtitle] = pageHeader(route, store);
  const displayName =
    session?.user.nickname || session?.user.email.split("@")[0] || "blogger";

  return (
    <>
      <div className={`app ${signedIn ? "is-signed-in" : "is-signed-out"}`}>
        {signedIn && (
          <header className="site-header workspace-header">
            <div className="header-inner">
              <a className="brand" href="#/" aria-label="Blog-it 홈">
                <span className="brand-logo sticky-logo" aria-hidden="true">
                  <span className="sticky-tape" />
                  <span className="note front">
                    <span className="logo-text">Blog-it</span>
                  </span>
                </span>
              </a>

              <nav className="nav top-nav" aria-label="상단 메뉴">
                {NAV.map((item) => (
                  <a
                    key={item.route}
                    href={item.href}
                    // 활성 표시(aria-current)는 모든 탭이 같은 방식이다. data-nav는 탭 하나에만
                    // 색을 달리 주고 싶을 때 붙일 자리로, 다른 탭의 기존 표시에는 손대지 않는다.
                    data-nav={item.route}
                    // '새 글 작성' 탭은 새 글의 첫 단계로 간다 — 열려 있던 글은 내 글
                    // 목록에서 이어서 쓸 수 있다('새 글로 시작' 버튼과 같은 동작).
                    // 다만 원고를 만드는 중이면 그 진행을 보여 준다(openWritingTab).
                    onClick={item.route === "write" ? openWritingTab : undefined}
                    aria-current={route === item.route ? "page" : undefined}
                  >
                    <NavIcon route={item.route} />
                    <span>{item.label}</span>
                    {item.route === "posts" && posts.length > 0 && (
                      <span className="count">{posts.length}</span>
                    )}
                  </a>
                ))}
              </nav>

              <div className="header-account">
                <div className="user-chip">
                  <strong>{displayName}님</strong>
                  <button
                    className="link-button account-switch-button"
                    type="button"
                    onClick={openAccountChooser}
                  >
                    계정 전환
                  </button>
                  <button
                    className="link-button"
                    type="button"
                    onClick={() => signOut()}
                  >
                    로그아웃
                  </button>
                </div>
              </div>
            </div>
          </header>
        )}

        <main className="main" data-route={route}>
          {!signedIn && <AuthView />}

          {signedIn && (
            <div className="workspace-shell">
              <section className="workspace-main" aria-label={title}>
                {route === "write" && (
                  <header className="topbar">
                    <div>
                      <h1>{title}</h1>
                      <p className="subtle">{subtitle}</p>
                    </div>
                    <div className="top-actions">
                      {route === "write" && task && (
                        <button
                          className="button small"
                          type="button"
                          onClick={restartWriting}
                        >
                          새 글로 시작
                        </button>
                      )}
                    </div>
                  </header>
                )}

                <div className="content">
                  <ScreenError key={route}>
                    <Suspense
                      fallback={
                        <div className="loading-state">
                          화면을 불러오는 중입니다.
                        </div>
                      }
                    >
                      {route === "home" && <HomeView />}
                      {/* postId를 key로 준다. 다른 글을 열면 작성 화면이 통째로 다시 마운트되어
                        이전 글의 폼 값·타이머·진행 플래그가 넘어오지 않는다. */}
                      {route === "write" && (
                        <WriteView key={activePostId ?? "new"} />
                      )}
                      {route === "bulk" && <BulkScheduleView />}
                      {route === "scheduled" && <ScheduledView />}
                      {route === "posts" && <PostsView />}
                      {route === "settings" && <SettingsView />}
                    </Suspense>
                  </ScreenError>
                </div>
              </section>
            </div>
          )}
        </main>

        <footer className="site-footer">
          <span>Blog-it Studio — 소재부터 발행까지 한 흐름으로</span>
        </footer>
      </div>

      <OnboardingModal />
      <Toast />
      {signedIn && accountChooserOpen && (
        <AccountChooserOverlay onClose={closeAccountChooser} />
      )}
    </>
  );
}
