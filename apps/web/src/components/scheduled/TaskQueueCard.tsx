import type { PostingChannel, ScheduledJob } from "../../api/types";
import { draftLabels } from "./draftLabels";
import { BarsIcon, EyeIcon, NaverMark, ThreadsMark, TrashIcon } from "./icons";
import { formatWorkStartAt } from "./schedule";
import { jobStatusLabel, jobStatusTone } from "./topics";

/**
 * 사용자가 **시각을 고르지 않은** 작업인가(2026-08-12 사용자 신고: "예약시간
 * 설정안했는데?").
 *
 * 시각을 비우면 서버가 '지금'을 넣는다(schedule_prepared_post의 `run_at`) — 걸자마자
 * 시작하라는 뜻이다. 그런데 화면이 그 값을 그대로 그리면 **이미 지난 시각이 예정 시각으로**
 * 찍혀, 사용자는 자기가 걸지도 않은 예약을 보게 된다.
 *
 * 작업이 만들어진 시각과 견줘 판단한다 — 따로 표시를 저장하지 않아도 되고, 이미 걸려
 * 있는 옛 작업에도 그대로 통한다. 사용자가 고른 시각은 언제나 **만든 시각보다 뒤**다
 * (지난 시각은 입력 단계에서 막는다).
 */
function startsRightAway(job: ScheduledJob): boolean {
  if (!job.publishAt || !job.createdAt) return false;
  return new Date(job.publishAt).getTime() <= new Date(job.createdAt).getTime();
}

type Props = {
  /**
   * 카드 제목. 지금은 작업 큐 한 곳만 쓴다 — 발행 내역은 2026-08-06에 이 표를 떠나
   * 자기 화면(PublishHistoryCard)을 갖게 됐다. 끝난 일은 예정 시각이 아니라 **실제
   * 발행 시각과 글 주소**가 필요해서, 같은 표로는 둘 다 제대로 말할 수 없었다.
   */
  title?: string;
  jobs: ScheduledJob[];
  /** 표가 비었을 때의 문구. 자리마다 '왜 비었는지'가 다르다. */
  emptyMessage?: string;
  busy: boolean;
  /** 만들어진 글을 기존 글 보기 흐름으로 연다. postId가 없으면 열 수 없다. */
  onPreview: (postId: string) => void;
  onRetry: (jobId: string) => void;
  onRemove: (jobId: string) => void;
};

/**
 * 이 작업이 올라갈 곳. 순서는 실제 발행 순서와 같다(네이버가 먼저).
 *
 * `publishNaver`가 없는 **옛 작업은 네이버**다 — 그때는 네이버가 언제나 발행
 * 대상이었고, 그 배치는 지금도 네이버에 올린다. 없는 것을 없다고 적으면 화면이
 * 실제 동작과 다른 말을 하게 된다.
 */
function channelsOf(job: ScheduledJob): PostingChannel[] {
  const channels: PostingChannel[] = [];
  if (job.publishNaver ?? true) channels.push("naver");
  if (job.publishThreads) channels.push("threads");
  return channels;
}

const RETRYABLE = new Set(["FAILED", "NEEDS_HUMAN", "CANCELED"]);
/** 지금 돌고 있는 작업은 뺄 수 없고, 이미 발행된 글은 목록에서 지운다고 사라지지 않는다. */
const REMOVABLE = new Set(["WAITING", "FAILED", "NEEDS_HUMAN", "CANCELED", "READY_TO_PUBLISH"]);

export function TaskQueueCard({
  title = "작업 큐",
  jobs,
  emptyMessage = "소재를 입력하고 예약을 시작하면 작업이 여기에 표시됩니다.",
  busy,
  onPreview,
  onRetry,
  onRemove,
}: Props) {
  // 같은 소재가 여러 줄이면 몇 편째인지 붙인다(2026-08-12 사용자 요청). 한 편뿐인
  // 소재에는 붙이지 않는다 — 모든 줄에 '1편째'가 붙으면 구분이 되지 않는다.
  const labels = draftLabels(jobs);
  return (
    <section className="panel scheduled-panel" aria-labelledby="scheduled-queue-title">
      <div className="panel-header">
        <h2 className="panel-title" id="scheduled-queue-title">
          <span className="scheduled-panel-icon" aria-hidden="true">
            <BarsIcon />
          </span>
          {title}
        </h2>
      </div>
      <div className="panel-body">
        {jobs.length === 0 ? (
          <p className="scheduled-queue-empty">{emptyMessage}</p>
        ) : (
          <table className="scheduled-queue">
            <thead>
              <tr>
                <th scope="col">소재</th>
                <th scope="col">플랫폼</th>
                <th scope="col">작업 예정 시각</th>
                <th scope="col">상태</th>
                <th scope="col">비고</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.jobId}>
                  <td className="scheduled-queue-topic">
                    {job.topic}
                    {labels[job.jobId] && (
                      <span className="scheduled-queue-draft-no">{labels[job.jobId]}</span>
                    )}
                    {job.errorMessage && (
                      <span className="scheduled-queue-error">{job.errorMessage}</span>
                    )}
                  </td>
                  <td>
                    {/* 이 작업이 **실제로 올라갈 곳**을 적는다. publishNaver가 없는 옛
                        작업은 네이버다(그때는 그것뿐이었다). 쓰레드 단독 예약이 생긴
                        뒤로는 'Naver'를 늘 적으면 거짓말이 된다(2026-08-06).

                        플랫폼 하나가 **한 덩어리**여야 한다. 표식·이름·구분자를 따로
                        늘어놓았더니 flex 항목이 다섯 개가 되어 칸(고정 폭)을 넘쳤고,
                        넘친 글자가 옆의 '발행 시각' 위에 겹쳐 찍혔다. 이제 덩어리
                        사이에서만 줄이 바뀐다. */}
                    <span className="scheduled-queue-platform">
                      {channelsOf(job).map((channel) => (
                        <span className="scheduled-queue-channel" key={channel}>
                          {channel === "naver" ? <NaverMark /> : <ThreadsMark />}
                          {channel === "naver" ? "Naver" : "Threads"}
                        </span>
                      ))}
                    </span>
                  </td>
                  <td className="scheduled-queue-time">
                    {/* 절대 시각 예약에만 값이 있다. 간격 방식이면 앞 글이 끝나는 대로
                        이어서 올라가므로 정해진 시각이 없다 — 없는 것을 지어내지 않는다.

                        한 소재로 여러 편일 때 2편·3편은 **시각을 적지 않는다**
                        (2026-08-12 사용자 지시). 원고는 함께 만들지만 발행은 앞 편이
                        끝나야 차례가 오므로, 언제 올라갈지는 앞 편에 달렸다 — 세 편에
                        같은 시각을 적으면 셋이 동시에 올라간다는 거짓말이 된다. */}
                    {job.afterJobId ? (
                      // **무엇이 기다리는지 정확히 적는다**(2026-08-12 사용자 지적).
                      // 발행하는 작업은 원고를 앞 편과 **함께** 만들고 발행만 차례를
                      // 기다린다(worker._due_to_prepare) — 그런데 "이전 작업이 완료되면
                      // 진행됩니다"라고 적혀 있어, 이미 원고를 만들고 있는 줄이 아직
                      // 시작도 안 한 것처럼 보였다. 발행하지 않는 작업(원고만 만드는
                      // 예약)은 실제로 생성이 줄을 서므로 예전 문구가 맞다.
                      <span className="scheduled-queue-after">
                        {channelsOf(job).length > 0
                          ? "원고는 함께 만들고, 앞 편이 끝나면 발행됩니다."
                          : "이전 작업이 완료되면 진행됩니다."}
                      </span>
                    ) : startsRightAway(job) ? (
                      // 시각을 고르지 않고 건 작업이다 — 걸자마자 시작한다. 지난 시각을
                      // '예정 시각'으로 적으면 걸지도 않은 예약처럼 보인다(2026-08-12 신고).
                      <span className="scheduled-queue-now">바로 시작</span>
                    ) : job.publishAt ? (
                      formatWorkStartAt(job.publishAt)
                    ) : (
                      "간격에 따라"
                    )}
                  </td>
                  <td>
                    {/* 배지 하나로 끝낸다. 예전에는 그 아래 '4/4 사실 검수·문장 다듬기'
                        처럼 원고 생성 안쪽의 칸까지 적었는데, 이 표는 **무엇이 언제
                        올라가는가**를 보는 자리라 그 줄이 필요 없다(2026-08-07 사용자
                        요청). 진행 칸은 글 화면에 그대로 있다. */}
                    <span className={`badge scheduled-state ${jobStatusTone(job.status)}`}>
                      {jobStatusLabel(job)}
                    </span>
                  </td>
                  <td className="scheduled-queue-actions">
                    {RETRYABLE.has(job.status) ? (
                      <button
                        className="button small scheduled-preview"
                        type="button"
                        disabled={busy}
                        onClick={() => onRetry(job.jobId)}
                      >
                        재시도
                      </button>
                    ) : (
                      <button
                        className="button small scheduled-preview"
                        type="button"
                        // 원고가 아직 없으면 볼 것이 없다. generatedAt은 곧 '원고가
                        // 생겼다'는 표시다. 완료된 글도 발행된 주소가 아니라 기존
                        // 글 보기로 연다 — 원고·이미지를 그대로 확인할 수 있다.
                        disabled={!job.postId || !job.generatedAt}
                        onClick={() => job.postId && onPreview(job.postId)}
                      >
                        <EyeIcon />
                        미리보기
                      </button>
                    )}
                    {REMOVABLE.has(job.status) && (
                      <button
                        className="button small scheduled-remove"
                        type="button"
                        disabled={busy}
                        aria-label={`'${job.topic}' 작업 삭제`}
                        title="이 작업을 목록에서 뺍니다"
                        onClick={() => onRemove(job.jobId)}
                      >
                        <TrashIcon />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
