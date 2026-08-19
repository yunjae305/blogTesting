import { useMemo, useState } from "react";

import type { PostingChannel, ScheduledJob } from "../../api/types";
import { failureReason } from "./failureReason";
import { draftLabels } from "./draftLabels";
import { byFinishedOrder, finishedAt } from "./jobOrder";
import { BarsIcon, EyeIcon, NaverMark, ThreadsMark, TrashIcon } from "./icons";
import type { JobPostState } from "./useScheduledPosting";

type Props = {
  jobs: ScheduledJob[];
  /** jobId → 그 작업이 만든 글의 실제 상태(제목·상태·발행 주소). */
  posts: Record<string, JobPostState>;
  busy: boolean;
  onPreview: (postId: string) => void;
  onRetry: (jobId: string) => void;
  onRemove: (jobId: string) => void;
};

/**
 * 작업은 실패로 끝났는데 **글은 그렇지 않은** 경우를 한 줄로 적는다.
 *
 * 작업의 상태는 그 실행이 끝났을 때의 마지막 기억이고, 같은 글이 그 뒤에 다른 경로로
 * 완성되거나 발행될 수 있다. 그래서 사용자는 발행 내역의 '실패' 옆에서 '내 글 목록'의
 * 완성된 글을 보고 두 화면이 서로 다른 말을 한다고 느꼈다(2026-08-06 신고).
 *
 * 이제 그 사실을 **여기서** 말한다 — 두 화면을 오가며 짝을 맞추지 않아도 되게.
 */
function articleNote(job: ScheduledJob, post?: JobPostState): string {
  if (!post) {
    // 글이 아예 없다. 사용자가 '내 글 목록'에서 지웠거나 만들기 전에 끝난 작업이다.
    return job.postId ? "이 작업의 글은 남아 있지 않습니다." : "";
  }
  if (job.status === "COMPLETED") return "";
  if (post.publishedUrl) {
    return "이 글은 실제로 발행되어 있습니다. 예약 기록만 실패로 남았습니다.";
  }
  if (post.status === "READY_TO_PUBLISH") {
    return "원고는 완성돼 있습니다. '내 글 목록'에서 바로 발행할 수 있어요.";
  }
  if (post.status === "POSTING" || post.status === "POSTING_NEEDS_HUMAN") {
    return "지금 발행하는 중인 글입니다.";
  }
  if (post.title) {
    return "원고가 '내 글 목록'에 남아 있습니다.";
  }
  return "";
}

/** 이 작업이 올라간 곳. 옛 작업(publishNaver 없음)은 네이버다. */
function channelsOf(job: ScheduledJob): PostingChannel[] {
  const channels: PostingChannel[] = [];
  if (job.publishNaver ?? true) channels.push("naver");
  if (job.publishThreads) channels.push("threads");
  return channels;
}

type Filter = "all" | "COMPLETED" | "FAILED" | "CANCELED";

/** 실패 계열은 두 상태다 — 사용자에게는 둘 다 '실패'로 묶여 보이는 것이 맞다. */
function bucketOf(job: ScheduledJob): Exclude<Filter, "all"> {
  if (job.status === "COMPLETED") return "COMPLETED";
  if (job.status === "CANCELED") return "CANCELED";
  return "FAILED";
}

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

/** "8월 6일(목)" — 날짜 소제목. 읽을 수 없으면 빈 문자열. */
function dayLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getMonth() + 1}월 ${date.getDate()}일(${WEEKDAYS[date.getDay()]})`;
}

/** "오후 4:30" — 줄의 시각. */
export function clockLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const hours = date.getHours();
  const meridiem = hours < 12 ? "오전" : "오후";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${meridiem} ${hour12}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/**
 * 발행 내역.
 *
 * 작업 큐와 **같은 표를 쓰지 않는다**(2026-08-06 사용자 요청 — "너무 지저분하고 사용자가
 * 뭘 확인하라는건지 몰라"). 끝난 일과 남은 일은 읽는 목적이 달라서다:
 *
 * - 큐는 "다음에 무엇이 언제 올라가나"라서 **예정 시각**이 필요하다.
 * - 내역은 "무엇이 실제로 올라갔나"라서 **실제 발행 시각과 그 글의 주소**가 필요하다.
 *   같은 표를 쓰던 동안에는 끝난 줄까지 발행 시각 칸에 '간격에 따라'라고 적혀 있었다 —
 *   이미 올라간 글 앞에서 아무 뜻도 없는 문구다.
 *
 * 그 밖에 이 화면이 하는 일:
 *
 * - **실패 사유를 한 줄로 줄인다.** 원문은 접어 두고 '자세히'로 펼친다(failureReason).
 *   셀레니움 스택 1,520자가 그대로 붉게 깔려 있던 자리다.
 * - **날짜로 묶는다.** 며칠치가 한 덩어리로 쌓이면 어디까지가 오늘인지 알 수 없다.
 * - **직접 정리할 수 있게 한다.** 줄마다 '내역에서 지우기'가 있고, 지워지는 것은 예약
 *   기록 한 줄뿐이다 — 게시물도 원고도 그대로 남는다.
 */
export function PublishHistoryCard({
  jobs,
  posts,
  busy,
  onPreview,
  onRetry,
  onRemove,
}: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  // 같은 소재로 여러 편을 걸었으면 몇 편째인지 붙인다(2026-08-12 사용자 요청). 제목이
  // 아직 없는 줄(원고를 만들기 전에 실패한 작업)은 소재만 남아, 두 줄이 똑같아 보인다.
  const labels = draftLabels(jobs);
  // 어느 줄의 원문을 펼쳐 두었는가. 여러 줄을 동시에 펼 수 있다.
  const [opened, setOpened] = useState<Set<string>>(new Set());

  const counts = useMemo(() => {
    const tally = { all: jobs.length, COMPLETED: 0, FAILED: 0, CANCELED: 0 };
    for (const job of jobs) tally[bucketOf(job)] += 1;
    return tally;
  }, [jobs]);

  // 최근에 끝난 것부터. 그런 다음 날짜로 묶는다 — 묶음 안의 순서는 그대로 유지된다.
  const groups = useMemo(() => {
    const rows = jobs
      .filter((job) => filter === "all" || bucketOf(job) === filter)
      .slice()
      .sort(byFinishedOrder);
    const byDay: { day: string; rows: ScheduledJob[] }[] = [];
    for (const job of rows) {
      const day = dayLabel(finishedAt(job)) || "시각 미상";
      const last = byDay[byDay.length - 1];
      if (last && last.day === day) last.rows.push(job);
      else byDay.push({ day, rows: [job] });
    }
    return byDay;
  }, [jobs, filter]);

  const toggle = (jobId: string) =>
    setOpened((current) => {
      const next = new Set(current);
      if (next.has(jobId)) next.delete(jobId);
      else next.add(jobId);
      return next;
    });

  const FILTERS: { key: Filter; label: string }[] = [
    { key: "all", label: "전체" },
    { key: "COMPLETED", label: "발행 완료" },
    { key: "FAILED", label: "실패" },
    { key: "CANCELED", label: "취소" },
  ];

  return (
    <section className="panel scheduled-panel" aria-labelledby="publish-history-title">
      <div className="panel-header">
        <h2 className="panel-title" id="publish-history-title">
          <span className="scheduled-panel-icon" aria-hidden="true">
            <BarsIcon />
          </span>
          발행 내역
        </h2>
      </div>
      <div className="panel-body">
        {jobs.length === 0 ? (
          <p className="scheduled-queue-empty">
            아직 끝난 작업이 없습니다. 발행이 끝나거나 실패한 작업이 여기에 쌓입니다.
          </p>
        ) : (
          <>
            {/* 무엇을 보고 있는지 숫자로 먼저 말한다. 실패가 몇 건인지가 이 화면에
                들어온 사람이 가장 먼저 알고 싶은 것이다. */}
            <div className="history-filters" role="group" aria-label="발행 내역 거르기">
              {FILTERS.map(({ key, label }) => (
                <button
                  key={key}
                  type="button"
                  className={`history-filter${filter === key ? " is-active" : ""}`}
                  aria-pressed={filter === key}
                  onClick={() => setFilter(key)}
                >
                  {label} <span className="history-filter-count">{counts[key]}</span>
                </button>
              ))}
            </div>

            {groups.length === 0 ? (
              <p className="scheduled-queue-empty">이 조건에 해당하는 작업이 없습니다.</p>
            ) : (
              groups.map(({ day, rows }) => (
                <section className="history-day" key={day}>
                  <h3 className="history-day-title">{day}</h3>
                  <ul className="history-list">
                    {rows.map((job) => {
                      const bucket = bucketOf(job);
                      const reason = bucket === "FAILED" ? failureReason(job) : null;
                      const post = posts[job.jobId];
                      const title = post?.title || job.topic;
                      const note = articleNote(job, post);
                      // 작업이 실패로 끝났어도 글이 실제로 올라가 있으면 그 주소를 연다.
                      const url = job.postUrl || post?.publishedUrl;
                      const open = opened.has(job.jobId);
                      return (
                        <li className={`history-row is-${bucket.toLowerCase()}`} key={job.jobId}>
                          <div className="history-main">
                            <span className={`badge scheduled-state ${toneOf(bucket)}`}>
                              {labelOf(bucket)}
                            </span>
                            <div className="history-text">
                              <span className="history-title">
                                {title}
                                {labels[job.jobId] && (
                                  <span className="history-draft-no">{labels[job.jobId]}</span>
                                )}
                              </span>
                              {/* 제목이 소재와 다르면 어떤 소재로 쓴 글인지도 남긴다. */}
                              {title !== job.topic && (
                                <span className="history-topic">소재: {job.topic}</span>
                              )}
                              {reason && (
                                <span className="history-reason">
                                  {reason.summary}
                                  {reason.hint && (
                                    <span className="history-hint">{reason.hint}</span>
                                  )}
                                </span>
                              )}
                              {/* 작업은 실패로 끝났는데 **글은 그렇지 않은** 경우를
                                  여기서 말한다. 이걸 안 적으면 사용자가 발행 내역의
                                  '실패'와 '내 글 목록'의 완성된 글을 각각 보고 두
                                  화면이 다른 말을 한다고 느낀다(2026-08-06 신고). */}
                              {note && <span className="history-article-note">{note}</span>}
                            </div>
                          </div>

                          <div className="history-actions">
                            {/* 발행된 글은 **그 글로 바로 갈 수 있어야** 한다. 확인하러
                                들어온 화면에서 주소를 찾아 헤매게 두지 않는다.
                                작업이 실패로 끝났어도 글이 올라가 있으면 연다. */}
                            {url && (
                              <a
                                className="button small scheduled-preview"
                                href={url}
                                target="_blank"
                                rel="noreferrer noopener"
                              >
                                발행된 글 열기
                              </a>
                            )}
                            {/* 원고가 있으면 연다. 작업의 generatedAt만 보면 다른 경로로
                                완성된 원고를 못 여는데, 그게 바로 이번에 드러난 경우다. */}
                            {job.postId && (job.generatedAt || post?.title) && (
                              <button
                                className="button small scheduled-preview"
                                type="button"
                                onClick={() => job.postId && onPreview(job.postId)}
                              >
                                <EyeIcon />
                                원고 보기
                              </button>
                            )}
                            {bucket !== "COMPLETED" && (
                              <button
                                className="button small scheduled-preview"
                                type="button"
                                disabled={busy}
                                onClick={() => onRetry(job.jobId)}
                              >
                                재시도
                              </button>
                            )}
                            {reason?.detail && (
                              <button
                                className="button small history-detail-toggle"
                                type="button"
                                aria-expanded={open}
                                onClick={() => toggle(job.jobId)}
                              >
                                {open ? "자세히 닫기" : "자세히"}
                              </button>
                            )}
                            <button
                              className="button small scheduled-remove"
                              type="button"
                              disabled={busy}
                              aria-label={`'${job.topic}' 내역에서 지우기`}
                              title="이 줄을 발행 내역에서 지웁니다(게시물과 원고는 그대로)"
                              onClick={() => onRemove(job.jobId)}
                            >
                              <TrashIcon />
                            </button>
                          </div>

                          {/* 플랫폼·시각은 줄의 **오른쪽 칸**에 따로 선다. 예전에는
                              history-main 안에 있어 제목 첫 줄에 붙어 떴고, 아래
                              버튼줄까지 합친 상자에서는 위로 치우쳐 보였다(2026-08-07
                              사용자 지적). 이제 상자 높이의 가운데다. */}
                          <div className="history-meta">
                            <span className="history-platform">
                              {channelsOf(job).map((channel) => (
                                <span className="scheduled-queue-channel" key={channel}>
                                  {channel === "naver" ? <NaverMark /> : <ThreadsMark />}
                                  {channel === "naver" ? "Naver" : "Threads"}
                                </span>
                              ))}
                            </span>
                            {/* 시각을 **둘 다** 적는다. 실제 시각 하나만 적던 동안에는
                                예약 01:34짜리 글이 01:36으로 찍혀 "예약이 안 지켜졌다"로
                                읽혔다. 예약 시각은 **발행을 시작하는 시각**이고, 게시가
                                끝나기까지 채널마다 30초~2분이 더 걸린다(2026-08-07 실측:
                                네이버 26~35초, 스레드 1분 32초~2분 32초). 둘을 나란히
                                두면 그 차이가 그대로 보인다. */}
                            <span className="history-time">
                              {job.publishAt && (
                                <span className="history-time-planned">
                                  예약 {clockLabel(job.publishAt)}
                                </span>
                              )}
                              <span className="history-time-actual">
                                {bucket === "COMPLETED" ? "게시 " : "시도 "}
                                {clockLabel(finishedAt(job)) || "—"}
                              </span>
                            </span>
                          </div>

                          {/* 원문은 버리지 않는다. 접어 둘 뿐이다 — 무엇이 났는지 봐야
                              하는 사람이 있고, 우리가 요약을 잘못 짚었을 수도 있다. */}
                          {open && reason?.detail && (
                            <pre className="history-detail">{reason.detail}</pre>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))
            )}
          </>
        )}
      </div>
    </section>
  );
}

function labelOf(bucket: Exclude<Filter, "all">): string {
  if (bucket === "COMPLETED") return "발행 완료";
  if (bucket === "CANCELED") return "취소";
  return "실패";
}

function toneOf(bucket: Exclude<Filter, "all">): string {
  if (bucket === "COMPLETED") return "done";
  if (bucket === "CANCELED") return "canceled";
  return "failed";
}
