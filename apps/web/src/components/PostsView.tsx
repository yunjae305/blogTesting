import { useEffect, useRef, useState } from "react";
import type { BlogTaskStatus } from "../api/types";
import {
  matchesPostsFilter,
  STATUS_LABELS,
  sortPosts,
  type PostsFilter,
  type PostsLayout,
  type PostsSort,
} from "../constants";
import { useStore } from "../store";
import { EmptyPosts, PostCard } from "./PostCard";

function FilterIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M4 5h16l-6 7.5V18l-4 2v-7.5L4 5Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * 목록에서 고를 수 있는 상태.
 *
 * **글에 붙는 이름을 그대로, 하나도 빠짐없이 쓴다**(`STATUS_LABELS`). 한때 셋으로
 * 줄였더니 '소재 준비됨'·'원고 만드는 중' 같은 글이 어느 항목에도 안 잡혔다 — 화면에는
 * 그 이름이 떠 있는데 필터에는 없으니 고를 방법이 없었다(2026-08-06 사용자 지적).
 *
 * 이름을 여기서 따로 적지 않는 것이 핵심이다. 상태 이름을 고치면 필터도 함께 따라온다.
 */
const STATUS_FILTERS: { value: PostsFilter | null; label: string }[] = [
  { value: null, label: "전체" },
  ...(Object.keys(STATUS_LABELS) as BlogTaskStatus[]).map((status) => ({
    value: { type: "status", status } as PostsFilter,
    label: STATUS_LABELS[status].text,
  })),
];

const SORT_OPTIONS: { value: PostsSort; label: string }[] = [
  { value: "newest", label: "최신순" },
  { value: "oldest", label: "오래된 순" },
];

function filterKey(filter: PostsFilter | null): string {
  if (!filter) return "all";
  return filter.type === "status" ? `status:${filter.status}` : `group:${filter.kind}`;
}

/** 보기 방식과 정렬은 화면을 옮겨도 유지된다 — 매번 다시 고르게 하지 않는다. */
const LAYOUT_KEY = "blogit.posts.layout";
const SORT_KEY = "blogit.posts.sort";

function remembered<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const saved = localStorage.getItem(key);
    return allowed.includes(saved as T) ? (saved as T) : fallback;
  } catch {
    // 저장소를 못 쓰는 브라우저·시크릿 모드. 기본값으로 그냥 돈다.
    return fallback;
  }
}

function remember(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // 기억하지 못할 뿐, 화면은 그대로 동작한다.
  }
}

function LayoutIcon({ layout }: { layout: PostsLayout }) {
  return layout === "card" ? (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4 5h7v6H4zM13 5h7v6h-7zM4 13h7v6H4zM13 13h7v6h-7z" fill="none" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4 6h16M4 12h16M4 18h16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function PostsView() {
  const { posts, postsLoading, deletePosts, postsFilter, setPostsFilter, reloadPosts } =
    useStore();
  const [selecting, setSelecting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [filterOpen, setFilterOpen] = useState(false);
  const filterMenuRef = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState<PostsLayout>(() => remembered(LAYOUT_KEY, ["card", "list"] as const, "card"));
  const [sort, setSort] = useState<PostsSort>(() => remembered(SORT_KEY, ["newest", "oldest"] as const, "newest"));

  const posts_ = sortPosts(
    posts.filter((task) => matchesPostsFilter(task, postsFilter)),
    sort,
  );
  const initialLoading = postsLoading && posts.length === 0;

  /**
   * 이 화면을 열 때 목록을 다시 읽는다.
   *
   * 목록은 **로그인할 때 한 번** 읽고 그 뒤로는 아무도 다시 읽지 않았다(store의
   * `reloadPosts`를 부르는 곳이 하나도 없었다). 그래서 예약 포스팅이 만든 글은 새로
   * 고치기 전에는 '내 글 목록'에 나타나지 않았다 — 서버에는 멀쩡히 있는데 화면에만
   * 없으니 "글이 안 만들어졌다"로 읽힌다(2026-08-06 신고).
   *
   * 화면을 여는 순간만 읽는다(폴링이 아니다). 목록을 보러 온 사람에게 필요한 것은
   * '지금 무엇이 있나'이고, 그 뒤의 변화는 다시 들어올 때 따라온다.
   */
  useEffect(() => {
    void reloadPosts();
  }, [reloadPosts]);

  // 바깥을 클릭하거나 Esc를 누르면 필터 메뉴를 닫는다.
  useEffect(() => {
    if (!filterOpen) return;
    function onPointerDown(event: MouseEvent) {
      if (filterMenuRef.current && !filterMenuRef.current.contains(event.target as Node)) {
        setFilterOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setFilterOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [filterOpen]);

  const activeFilterLabel = STATUS_FILTERS.find(
    (option) => filterKey(option.value) === filterKey(postsFilter),
  )?.label;

  function chooseLayout(next: PostsLayout) {
    setLayout(next);
    remember(LAYOUT_KEY, next);
  }

  function chooseSort(next: PostsSort) {
    setSort(next);
    remember(SORT_KEY, next);
    setFilterOpen(false);
  }

  const allSelected = posts_.length > 0 && selectedIds.size === posts_.length;

  function toggleSelect(postId: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(postId)) next.delete(postId);
      else next.add(postId);
      return next;
    });
  }

  function toggleSelectAll() {
    setSelectedIds(allSelected ? new Set() : new Set(posts_.map((post) => post.postId)));
  }

  function exitSelection() {
    setSelecting(false);
    setSelectedIds(new Set());
  }

  async function deleteSelected() {
    const ids = [...selectedIds];
    if (!ids.length) return;
    const scope = ids.length === posts_.length ? "전체" : `${ids.length}개`;
    if (!window.confirm(`글 ${scope}를 삭제할까요? 되돌릴 수 없습니다.`)) return;
    await deletePosts(ids);
    exitSelection();
  }

  return (
    <section className="stack posts-view--previous" aria-labelledby="posts-list-title">
      <div className="panel">
        <div className="panel-header">
          <h2 className="panel-title" id="posts-list-title">
            {selecting ? `${selectedIds.size}개 선택` : "내 글"}
            {/* 몇 편이 있는지 제목 옆에 적는다(2026-08-07 사용자 요청).
                **지금 목록에 보이는 수**다 — 필터가 걸려 있으면 걸러진 수이고, 그래야
                아래 줄 수와 같은 말을 한다. 무엇으로 걸렀는지는 옆의 필터 배지가 말한다.

                처음 불러오는 동안에는 적지 않는다. 그때 posts는 아직 비어 있어 '0'이
                나오는데, 그건 "글이 없다"가 아니라 "아직 모른다"이다. */}
            {!selecting && !initialLoading && (
              <span className="posts-count">{posts_.length}편</span>
            )}
            {!selecting && activeFilterLabel && activeFilterLabel !== "전체" && (
              <span className="posts-filter-badge">
                {activeFilterLabel}
                <button
                  type="button"
                  className="posts-filter-badge-clear"
                  aria-label="필터 해제"
                  onClick={() => setPostsFilter(null)}
                >
                  ×
                </button>
              </span>
            )}
          </h2>
          <div className="panel-header-actions">
            {selecting ? (
              <>
                <button
                  className="button small"
                  type="button"
                  onClick={toggleSelectAll}
                  disabled={!posts_.length}
                >
                  {allSelected ? "선택 해제" : "전체 선택"}
                </button>
                <button
                  className="button small danger"
                  type="button"
                  onClick={deleteSelected}
                  disabled={!selectedIds.size}
                >
                  선택 삭제{selectedIds.size ? ` (${selectedIds.size})` : ""}
                </button>
                <button className="button small" type="button" onClick={exitSelection}>
                  취소
                </button>
              </>
            ) : (
              <>
                {/* 카드 ↔ 리스트. 누르면 다른 쪽으로 바뀌므로 버튼 하나면 된다 —
                    라벨은 '지금 무엇인지'가 아니라 '누르면 무엇이 되는지'를 말한다. */}
                <button
                  className="button small icon-button"
                  type="button"
                  aria-label={layout === "card" ? "리스트로 보기" : "카드로 보기"}
                  title={layout === "card" ? "리스트로 보기" : "카드로 보기"}
                  onClick={() => chooseLayout(layout === "card" ? "list" : "card")}
                >
                  <LayoutIcon layout={layout === "card" ? "list" : "card"} />
                </button>
                <div className="posts-filter-menu" ref={filterMenuRef}>
                  <button
                    className={`button small icon-button ${postsFilter ? "active" : ""}`}
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={filterOpen}
                    aria-label="정렬과 상태"
                    title="정렬과 상태"
                    onClick={() => setFilterOpen((open) => !open)}
                  >
                    <FilterIcon />
                  </button>
                  {filterOpen && (
                    <div className="posts-filter-panel" role="menu" aria-label="정렬과 상태">
                      {/* 정렬과 상태는 하는 일이 다르다. 하나를 고르면 다른 하나가
                          풀리면 안 되므로 묶음을 나눠 둔다. */}
                      <p className="posts-filter-group-label">정렬</p>
                      {SORT_OPTIONS.map((option) => (
                        <button
                          key={option.value}
                          type="button"
                          role="menuitemradio"
                          aria-checked={sort === option.value}
                          className={`posts-filter-option ${sort === option.value ? "active" : ""}`}
                          onClick={() => chooseSort(option.value)}
                        >
                          {option.label}
                        </button>
                      ))}
                      <p className="posts-filter-group-label">상태</p>
                      {STATUS_FILTERS.map((option) => {
                        const active = filterKey(option.value) === filterKey(postsFilter);
                        return (
                          <button
                            key={filterKey(option.value)}
                            type="button"
                            role="menuitemradio"
                            aria-checked={active}
                            className={`posts-filter-option ${active ? "active" : ""}`}
                            onClick={() => {
                              setPostsFilter(option.value);
                              setFilterOpen(false);
                            }}
                          >
                            {option.label}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
                {posts_.length > 0 && (
                  <button
                    className="button small"
                    type="button"
                    onClick={() => setSelecting(true)}
                  >
                    글 삭제
                  </button>
                )}
                <a className="button primary small" href="#/write">
                  새 글 쓰기
                </a>
              </>
            )}
          </div>
        </div>
        {/* 리스트에서는 본문 여백을 걷는다. 줄이 직접 좌우 여백을 가져야 오른쪽 끝이
            위의 '새 글 쓰기' 버튼과 같은 자리에서 끝난다(2026-08-06 사용자 지적). */}
        <div className={`panel-body${layout === "list" ? " posts-list-body" : ""}`}>
          {/* 리스트에서만 나오는 머리글. 각 줄이 이미 자기 칸 이름을 들고 있으므로
              (post-card-mobile-label) 읽어 주는 쪽에는 감춘다 — 두 번 읽힌다. */}
          {layout === "list" && !initialLoading && posts_.length > 0 && (
            <div className="posts-list-head" aria-hidden="true">
              <span>순번</span>
              <span>소재</span>
              <span>제목</span>
              <span>상태</span>
              <span className="posts-list-head-purpose">글의 목적</span>
              <span>생성 시각</span>
              <span />
            </div>
          )}
          <div
            className={layout === "card" ? "post-grid posts-card-grid" : "post-grid posts-list"}
            role={!initialLoading && posts_.length ? "list" : undefined}
            aria-label={!initialLoading && posts_.length ? "작성한 글" : undefined}
            aria-busy={postsLoading || undefined}
          >
            {initialLoading ? (
              <div className="empty posts-card-empty posts-card-loading" role="status" aria-live="polite">
                <p>저장된 글을 불러오는 중입니다.</p>
              </div>
            ) : posts_.length ? (
              posts_.map((task, index) => (
                <PostCard
                  task={task}
                  key={task.postId}
                  layout={layout === "card" ? "previous-grid" : "list"}
                  order={index + 1}
                  selectionMode={selecting}
                  selected={selectedIds.has(task.postId)}
                  onToggleSelect={toggleSelect}
                />
              ))
            ) : posts.length > 0 ? (
              // 글은 있지만 지금 필터에 맞는 게 없는 경우. "아직 쓴 글이 없습니다"는
              // 거짓말이 된다 — 필터를 풀 방법을 바로 준다.
              <div className="empty posts-card-empty">
                <p>{activeFilterLabel} 상태인 글이 없습니다.</p>
                <button className="button small" type="button" onClick={() => setPostsFilter(null)}>
                  필터 해제
                </button>
              </div>
            ) : (
              <EmptyPosts layout={layout === "card" ? "previous-grid" : "list"} />
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
