import { useEffect, useRef, useState } from "react";

import { useStore } from "../../store";
import { LiveSessionsPanel } from "../LiveSessionsPanel";
import { byQueueOrder } from "./jobOrder";
import { PublishHistoryCard } from "./PublishHistoryCard";
import { ReservationHeader } from "./ReservationHeader";
import { ReservationTabs, type ReservationTab } from "./ReservationTabs";
import { ScheduleControlCard } from "./ScheduleControlCard";
// 처음 오는 사람이 옆에 두고 읽는 안내. 다른 탭과 같은 카드를 쓴다(2026-08-12).
import { ScheduledHelperCard } from "./ScheduledHelperCard";
import { TaskQueueCard } from "./TaskQueueCard";
import { TaskStatusCard } from "./TaskStatusCard";
import { useScheduledPosting } from "./useScheduledPosting";

/**
 * 「작업 관리」 화면 — 걸어 둔 일을 **보는** 곳이다.
 *
 * 예약을 거는 화면은 둘이다: 새 글 작성(한 편, 사람이 고른다)과 자동 포스팅(여러 편,
 * 서버가 고른다). 어느 쪽으로 걸었든 진행과 결과는 여기서 본다.
 *
 * 글은 여기서 만들지 않는다 — 서버의 예약 워커가 **새 글 작성과 똑같은 파이프라인**을
 * 순서대로 부르고, 이 화면은 그 진행 상황을 보여 주고 시작·정지를 요청할 뿐이다.
 *
 * ## 걸음으로 나눈 이유
 *
 * 예전에는 소재·일정·큐·현황이 한 화면에 다 있었다. 처음 오는 사람은 어디부터 손대야
 * 하는지 알 수 없었고, 화면 절반이 아직 쓸 일 없는 카드로 차 있었다. 그래서 만드는
 * 일을 걸음으로 나눈다: 소재 입력 → 발행 일정 → (시작) 작업 큐.
 *
 * **입력하는 걸음은 둘뿐이고, 세 번째 걸음은 화면이 아니라 작업 큐 탭이다**
 * (2026-08-06 사용자 요청). 예전에는 그 사이에 '일정 확인' 화면이 하나 더 있었고,
 * 예약이 도는 동안에는 그 자리가 작업 큐 탭과 **같은 내용**(제어·큐·현황)이었다 —
 * 같은 것을 두 곳에서 보여 주던 자리다. 이제 2걸음의 마지막 버튼이 **예약을 걸고**
 * 작업 큐로 데려간다.
 *
 * **걸음은 화면일 뿐 데이터가 아니다.** 입력값·API 호출·서버로 보내는 몸통은 예전
 * 그대로이고(useScheduledPosting), 여기서는 그중 무엇을 지금 보여 줄지만 고른다.
 * 그래서 걸음을 오가도 적어 둔 것은 사라지지 않는다.
 */
export function ScheduledView() {
  const { openPost } = useStore();
  const scheduled = useScheduledPosting();
  const {
    batch,
    jobs,
    scheduledJobs,
    jobPosts,
    actionBusy,
  } = scheduled;

  /**
   * 화면 상태(어느 탭·어느 걸음)만 여기서 들고 있는다. 예약 데이터는 훅의 것이다.
   *
   * 주소가 `#/scheduled/queue`면 작업 큐로 연다. 내 글 목록에서 '예약 작업 보기'를
   * 누르면 그 주소로 오기 때문이다 — 예약이 만들고 있는 글의 진행은 새 글 작성이
   * 아니라 여기서 봐야 한다(2026-08-06 사용자 요청).
   */
  const [tab, setTab] = useState<ReservationTab>("queue");

  // 이미 걸어 둔 예약이 있으면 작업 큐를 연다. 그 사람이 보러 온 것은 새로 만들 예약이
  // 아니라 **지금 도는 예약**이다.
  //
  // 배치가 바뀔 때 한 번만 옮긴다. 매번 옮기면 배치가 도는 동안 소재를 고치러 1걸음으로
  // 돌아간 사용자를 2초 뒤 폴링이 도로 끌고 온다.
  const openedBatch = useRef<string | null>(null);
  useEffect(() => {
    const batchId = batch?.batchId ?? null;
    if (!batchId || openedBatch.current === batchId) return;
    openedBatch.current = batchId;
    setTab("queue");
  }, [batch?.batchId]);

  // 지금 글을 쓰거나 발행하는 중일 때만 입력을 잠근다.
  //
  // 배치가 RUNNING이어도 대개는 다음 발행까지 **기다리는 중**이다. 그때까지 잠가 두면
  // 간격을 바꾸려고 정지를 눌러야 하는데, 그 한 단계가 "1분으로 바꿨는데 5분마다 올라간다"의
  // 원인이었다. 기다리는 동안에는 고쳐서 바로 다시 시작할 수 있어야 한다.
  const executing = jobs.some(
    (job) => job.status === "RUNNING" || job.status === "PUBLISHING",
  );
  /**
   * 작업 큐와 발행 내역이 **겹치지 않게** 가른다(2026-08-06 사용자 요청).
   *
   * "작업큐에서 이미 발행이 완료가 된건 안보이게 하고 완료 내역들은 발행 내역에서
   * 확인할수 있게. 실패도 마찬가지."
   *
   * - 작업 큐 = **아직 남은 일.** 대기·진행 중·발행 대기·인증 필요(사람이 손대면 이어진다).
   * - 발행 내역 = **끝난 일.** 완료·실패·취소. 실패의 '재시도'도 여기서 누른다 —
   *   누르면 그 작업이 다시 대기가 되어 작업 큐로 돌아온다.
   */
  const FINISHED = new Set(["COMPLETED", "FAILED", "CANCELED"]);
  /**
   * 두 조회를 합쳐 본다: **내 예약 전부**(`/scheduled/naver/jobs`)와 **지금 도는 배치**.
   *
   * 목록 조회만 읽으면 예약을 시작한 **직후**의 몇 초가 비어 있다 — 서버가 방금 만든
   * 작업이 아직 그 목록에 담겨 오지 않으면, 화면에는 "예약 작업 2건이 생성되었습니다"라는
   * 로그와 **빈 작업 큐**가 나란히 놓인다(2026-08-06 사용자 신고). 시작 응답과 활성 배치
   * 조회에는 그 작업들이 들어 있으므로, 목록에 없는 것만 뒤에 이어 붙인다.
   */
  const allJobs = [
    ...scheduledJobs,
    ...jobs.filter((job) => !scheduledJobs.some((known) => known.jobId === job.jobId)),
  ];
  /**
   * 합친 뒤 **다시 세운다.** 서버도 같은 순서로 내려주지만(최근 예약 · 입력 순서), 여기서
   * 목록에 아직 없는 작업을 뒤에 이어 붙이므로 그 순간 순서가 흐트러진다.
   *
   * 사용자가 GS25 · 세븐일레븐 순으로 넣었는데 큐 맨 위에 세븐일레븐이 서 있던 것이
   * 이 순서 문제였다(2026-08-06 신고). 서버 쪽 원인은 한 배치의 작업이 createdAt·
   * publishAt이 전부 같아 정렬이 동점이었던 것이고, 화면 쪽 원인이 이 이어 붙이기다.
   */
  const orderedJobs = allJobs.slice().sort(byQueueOrder);
  const pendingJobs = orderedJobs.filter((job) => !FINISHED.has(job.status));
  const finishedJobs = orderedJobs.filter((job) => FINISHED.has(job.status));

  /**
   * 작업 표 하나를 만든다.
   *
   * 두 탭(작업 큐·발행 내역) 모두 **내 예약 전부**를 읽는다 — 활성 배치만 읽으면 배치가
   * 끝나는 순간 표가 통째로 빈다. 진행률(작업 현황)만 지금 돌고 있는 배치를 본다.
   */
  const taskQueue = (rows: typeof jobs, title?: string, emptyMessage?: string) => (
    <TaskQueueCard
      title={title}
      jobs={rows}
      emptyMessage={emptyMessage}
      busy={actionBusy}
      // 새 미리보기 렌더러를 만들지 않는다. 기존 '글 보기' 흐름을 그대로 쓴다.
      onPreview={(postId) => void openPost(postId)}
      onRetry={(jobId) => void scheduled.retry(jobId)}
      // 되돌릴 수 없는 동작이라 한 번 묻는다(내 글 목록의 삭제와 같은 방식).
      onRemove={(jobId) => {
        const target = rows.find((job) => job.jobId === jobId);
        const label = target ? `'${target.topic}'` : "이";
        if (!window.confirm(`${label} 작업을 예약에서 뺄까요?`)) return;
        void scheduled.removeJob(jobId);
      }}
    />
  );

  return (
    <div className="scheduled-page">
      {/* 머리와 탭은 한 장으로 묶는다 — 탭이 이 화면의 것임을 자리로 말한다. */}
      <div className="reservation-top">
        <ReservationHeader />
        {/* 배지는 **남은 일의 수**다. 끝난 것까지 세면 예약을 다 마쳐도 숫자가 남는다. */}
        <ReservationTabs current={tab} onSelect={setTab} queueCount={pendingJobs.length} />
      </div>

      {/* 두 탭 모두 **왼쪽 일 · 오른쪽 도우미**다(2026-08-12 사용자 요청). 자동 포스팅과
          같은 격자를 쓴다 — 탭을 옮겨도 도우미가 제자리에 있어야 눈이 그것을 찾는다.
          표가 화면 끝까지 늘어나던 것도 이 격자가 함께 줄인다. */}
      {tab === "queue" && (
        <div className="reservation-grid reservation-grid--queue">
          <div className="reservation-column">
            {/* 예약을 만드는 마지막 걸음이자 도는 예약을 손대는 자리다. 예전에는 이 카드가
                '새 예약 만들기'의 3걸음에도 똑같이 있었다(2026-08-06 사용자 요청으로 한 곳). */}
            <ScheduleControlCard
              batch={batch}
              executing={executing}
              busy={actionBusy}
              onPause={() => void scheduled.pause()}
              onResume={() => void scheduled.resume()}
              onDiscard={() => {
                // 미완료 작업이 DB에서 지워지는 되돌릴 수 없는 동작이라 한 번 묻는다.
                if (
                  !window.confirm(
                    "남은 작업을 모두 정지하고 삭제합니다. 입력한 정보도 초기화됩니다. 계속할까요?",
                  )
                )
                  return;
                void scheduled.discard();
              }}
            />
            {taskQueue(
              pendingJobs,
              undefined,
              finishedJobs.length > 0
                ? "남은 작업이 없습니다. 끝난 작업은 '발행 내역'에서 볼 수 있어요."
                : undefined,
            )}
            {/* 진행률은 지금 돌고 있는 배치의 것이다 — 끝난 배치까지 섞어 세면
                '0%'와 '100%'가 오간다. 배치가 없으면 카드가 빈 상태를 보여 준다. */}
            <TaskStatusCard batch={batch} jobs={jobs} jobPosts={jobPosts} />
            {/* 예약 발행이 서버에서 크롬을 여는 동안 그 화면을 중계한다 — 2단계 인증이
                뜨면 여기서 바로 처리할 수 있다. 발행 중이 아니면 아무것도 그려지지 않는다. */}
            <LiveSessionsPanel kinds={["publish"]} />
          </div>
          <div className="reservation-column">
            <ScheduledHelperCard />
          </div>
        </div>
      )}

      {tab === "history" && (
        <div className="reservation-grid reservation-grid--queue">
          <div className="reservation-column">
            {/* 끝난 일은 남은 일과 **읽는 목적이 다르다.** 큐의 표를 그대로 쓰던 동안에는
                이미 올라간 글의 '발행 시각' 칸에 '간격에 따라'라고 적혀 있었고, 실패
                사유는 셀레니움 스택 그대로 붉게 깔렸다(2026-08-06 사용자 요청). */}
            <PublishHistoryCard
              jobs={finishedJobs}
              posts={jobPosts}
              busy={actionBusy}
              onPreview={(postId) => void openPost(postId)}
              onRetry={(jobId) => void scheduled.retry(jobId)}
              onRemove={(jobId) => {
                const target = finishedJobs.find((job) => job.jobId === jobId);
                const label = target ? `'${target.topic}'` : "이";
                // 발행된 글까지 지울 수 있게 열어 둔 자리다 — 무엇이 지워지고 무엇이
                // 남는지 분명히 적는다. 게시물과 원고는 그대로다.
                if (
                  !window.confirm(
                    `${label} 기록을 발행 내역에서 지울까요?\n` +
                      "이미 올라간 게시물과 '내 글 목록'의 원고는 그대로 남습니다.",
                  )
                )
                  return;
                void scheduled.removeHistoryJob(jobId);
              }}
            />
          </div>
          <div className="reservation-column">
            <ScheduledHelperCard />
          </div>
        </div>
      )}

      {/* '새 예약 만들기'(소재 입력 → 발행 방식) 두 걸음은 없앴다(2026-08-11 사용자 요청).
          새 글 작성이 작업 시각을 받게 되면서 예약을 흡수했고, 세밀한 설정은 그쪽에만
          있다. 이 화면은 이제 **걸어 둔 일을 보는 곳**이다 — 작업 큐와 발행 내역. */}
    </div>
  );
}
