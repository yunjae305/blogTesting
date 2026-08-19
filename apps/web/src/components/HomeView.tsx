import { postStatKind, type PostsFilter } from "../constants";
import { useStore } from "../store";
import { EmptyPosts, PostCard } from "./PostCard";

type StatKind = "draft" | "published" | "attention" | "total";
type ProcessKind = "idea" | "draft" | "post";

const PROCESS_STEPS: { title: string; label: string; kind: ProcessKind }[] = [
  { title: "IDEA", label: "아이디어 정리", kind: "idea" },
  { title: "DRAFT", label: "초안 작성", kind: "draft" },
  { title: "POST", label: "발행 완료", kind: "post" },
];

function StatIcon({ kind }: { kind: StatKind }) {
  if (kind === "published") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="m5 12 4 4L19 6" />
      </svg>
    );
  }
  if (kind === "attention") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M12 7v6M12 17.5v.5" />
      </svg>
    );
  }
  if (kind === "total") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M4 6.5h16M4 12h16M4 17.5h10" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="m14.7 5.3 4 4M4 20l3.8-.8L19 8a2.8 2.8 0 0 0-4-4L3.8 15.2 3 19.8 4 20Z" />
    </svg>
  );
}

function ProcessIcon({ kind }: { kind: ProcessKind }) {
  if (kind === "draft") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M5 5.5h10l4 4v9H5v-13Z" />
        <path d="M15 5.5v4h4M8 13h8M8 16h5" />
      </svg>
    );
  }
  if (kind === "post") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="m5 12 4 4L19 6" />
        <path d="M20 12v6.5H4V5.5h9" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M9 18h6M10 21h4M8.5 14.5a6 6 0 1 1 7 0c-.8.7-1.2 1.3-1.4 2h-4.2c-.2-.7-.6-1.3-1.4-2Z" />
      <path d="M12 2v1.5M4.9 4.9 6 6M2 12h1.5M20.5 12H22M18 6l1.1-1.1" />
    </svg>
  );
}

export function HomeView() {
  const { posts, postsLoading, settings, setPostsFilter, setRoute } = useStore();

  // 홈 카드 숫자와 내 글 목록 필터가 같은 기준(postStatKind)으로 나뉘게 한다 — 각자
  // 계산하면 카드를 눌러 들어간 목록이 카드가 센 숫자와 어긋날 수 있다.
  const published = posts.filter((task) => postStatKind(task.status) === "published").length;
  const attention = posts.filter((task) => postStatKind(task.status) === "attention").length;
  const drafting = posts.filter((task) => postStatKind(task.status) === "draft").length;
  const recentPosts = posts.slice(0, 3);
  const initialPostsLoading = postsLoading && posts.length === 0;

  const stats: {
    label: string;
    helper: string;
    value: number;
    kind: StatKind;
    filter: PostsFilter | null;
  }[] = [
    { label: "작성 중", helper: "이어 쓸 글", value: drafting, kind: "draft", filter: { type: "group", kind: "draft" } },
    {
      label: "발행 완료",
      helper: "블로그로 옮긴 글",
      value: published,
      kind: "published",
      filter: { type: "group", kind: "published" },
    },
    {
      label: "확인 필요",
      helper: "다시 봐야 할 글",
      value: attention,
      kind: "attention",
      filter: { type: "group", kind: "attention" },
    },
    { label: "전체 글", helper: "지금까지 만든 글", value: posts.length, kind: "total", filter: null },
  ];

  function openPostsFilteredBy(filter: PostsFilter | null) {
    setPostsFilter(filter);
    setRoute("posts");
  }

  return (
    <section className="stack home-dashboard">
      {!settings && (
        <div className="notice home-notice">
          <span>기본 설정을 저장하면 해시태그 수와 페르소나가 모든 글에 자동으로 적용됩니다.</span>
          <a className="button small" href="#/settings">
            설정하러 가기
          </a>
        </div>
      )}

      <div className="hero home-hero home-editorial-hero">
        <div className="hero-copy">
          <h1>
            아이디어를 정리하고,
            <br />
            멋진 블로그로 완성하세요!
          </h1>
          <p>
            Blog-it은 아이디어 정리부터 발행까지,
            <br />
            더 나은 글쓰기를 위한 모든 과정을 함께합니다.
          </p>
          <div className="hero-actions" aria-label="홈 주요 작업">
            <a className="button primary large hero-cta" href="#/write">
              <span>새 글 작성</span>
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </a>
            {/* '브랜드로 글쓰기'는 2026-08-11에 뺐다. 브랜드는 이제 새 글 작성 소재
                단계의 선택 항목이라, 여기서 갈라진 입구를 만들면 같은 일을 두 곳에서
                시작하게 된다. */}
            <a className="button secondary large" href="#/posts">
              내 글 목록 보기
            </a>
          </div>
        </div>

        <aside className="process-board process-sticky-row" aria-label="Blog-it 작성 흐름">
          <ol className="process-steps">
            {PROCESS_STEPS.map((step, index) => (
              <li className={`process-note process-note-${step.kind}`} key={step.title}>
                <span className="process-icon">
                  <ProcessIcon kind={step.kind} />
                </span>
                <span className="process-copy">
                  <strong>{step.title}</strong>
                  <span>{step.label}</span>
                </span>
                {index < PROCESS_STEPS.length - 1 && <span className="process-arrow">→</span>}
              </li>
            ))}
          </ol>
        </aside>
      </div>

      <dl className="stat-grid home-stats" aria-label="글 작업 현황">
        {stats.map(({ label, helper, value, kind, filter }) => (
          <div
            className={`stat stat-${kind}`}
            key={label}
            role="button"
            tabIndex={0}
            aria-label={`${label} ${value}개 — 내 글 목록에서 보기`}
            onClick={() => openPostsFilteredBy(filter)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                openPostsFilteredBy(filter);
              }
            }}
          >
            <span className="stat-icon">
              <StatIcon kind={kind} />
            </span>
            <span className="stat-copy">
              <dt>{label}</dt>
              <dd>
                <strong>{value}</strong>
              </dd>
              <span className="stat-helper">{helper}</span>
            </span>
            <span className="stat-chevron" aria-hidden="true">
              ›
            </span>
          </div>
        ))}
      </dl>

      <div className="home-recent-section posts-view--previous">
        <div className="panel">
          <div className="panel-header">
            <h2 className="panel-title">최근 글</h2>
            <a className="button small" href="#/posts">
              전체 보기
            </a>
          </div>
          <div className="panel-body">
            <div
              className="post-grid posts-card-grid"
              role={!initialPostsLoading && recentPosts.length ? "list" : undefined}
              aria-label={!initialPostsLoading && recentPosts.length ? "최근 글" : undefined}
              aria-busy={postsLoading || undefined}
            >
              {initialPostsLoading ? (
                <div className="empty posts-card-empty posts-card-loading" role="status" aria-live="polite">
                  <p>최근 글을 불러오는 중입니다.</p>
                </div>
              ) : recentPosts.length ? (
                recentPosts.map((task) => (
                  <PostCard task={task} key={task.postId} layout="previous-grid" />
                ))
              ) : (
                <EmptyPosts layout="previous-grid" />
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
