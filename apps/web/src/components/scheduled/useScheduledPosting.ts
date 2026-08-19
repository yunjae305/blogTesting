import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { request } from "../../api/client";
import type {
  ActivityEntry,
  BlogTaskStatus,
  PostingChannel,
  ScheduleMode,
  ScheduleTopicMode,
  ScheduledBatchView,
  ScheduledJob,
  ScheduledJobList,
  TaskProgress,
} from "../../api/types";
import { useStore } from "../../store";
import type { NaverStatus } from "../NaverConnect";
import type { ThreadsStatus } from "../ThreadsConnect";
import {
  browserTimeZone,
  publishIsoToWorkStartInput,
  workStartInputToPublishIso,
} from "./schedule";
import { normalizeTopics } from "./topics";

/** 활성 배치가 도는 동안의 폴링 주기. */
const POLL_ACTIVE_MS = 2000;
/** 멈춰 있는 배치(일시정지·인증 필요)는 덜 자주 본다 — 서버가 스스로 바꾸지 않는다. */
const POLL_IDLE_MS = 8000;

const RUNNING_STATUSES = new Set(["READY", "RUNNING", "PAUSE_REQUESTED", "STOP_REQUESTED"]);
const HALTED_STATUSES = new Set(["PAUSED", "NEEDS_HUMAN"]);

export const MAX_TOPICS = 20;
/** 생성 작업 간격(초). 서버의 MIN/MAX_INTERVAL_SECONDS와 같아야 한다. */
const MIN_INTERVAL_SECONDS = 15;
/** 서버로 보내는 간격 값. 화면에서 간격 입력을 뺐으므로(2026-08-07 사용자 결정 —
    순차 발행은 앞 글이 끝나면 다음으로) 항상 허용 최소값을 보낸다. 서버 검증이
    intervalSeconds를 요구하기 때문에 안 보낼 수는 없다. */
const DEFAULT_INTERVAL_SECONDS = MIN_INTERVAL_SECONDS;

// 기본 발행 시각(DEFAULT_FIRST_PUBLISH_MINUTES)은 없앴다(2026-08-12 사용자 결정).
// 시각 칸은 **비어 있는 것이 기본**이고, 빈 칸은 '앞 글이 발행되면 이어서'라는 뜻이다.

/**
 * 한 작업이 만든 **글**에 대해 화면이 아는 것. 서버의 ScheduledJobListItem에서 온다.
 *
 * 작업의 상태(`job.status`)와 **따로 둔다.** 둘은 갈라질 수 있고, 갈라졌을 때
 * 사실대로 말하려면 둘 다 필요하다.
 */
export type JobPostState = {
  title?: string;
  status?: BlogTaskStatus;
  publishedUrl?: string;
  progress?: TaskProgress;
  /**
   * 이 글의 '작업 현황' 줄들. 새 글 작성 화면이 보여 주는 것과 **같은 목록**이다
   * (2026-08-10 사용자 요청). 예약 자신의 로그는 단계 경계에서만 한 줄씩 쌓여, 원고를
   * 만드는 5~8분 동안 화면이 멈춘 것처럼 보였다 — 그 사이를 이 줄들이 채운다.
   */
  activityLog?: ActivityEntry[];
};

/**
 * 예약 포스팅 화면의 상태 한 벌.
 *
 * 폴링은 활성 배치가 있을 때만 돈다. 계정을 전환하면 이전 사용자의 늦은 응답이 새 계정의
 * 화면을 덮어쓰지 않도록, 응답을 쓸 때마다 그 요청을 띄운 시점의 사용자와 지금 사용자가
 * 같은지 확인한다.
 */
export function useScheduledPosting() {
  const { session, reportError, showToast } = useStore();
  const userId = session?.user.userId ?? null;

  const [topicsText, setTopicsText] = useState("");
  // 소재를 글로 나누는 방식. 기본은 예전과 같은 "소재별 한 편"이다.
  const [topicMode, setTopicMode] = useState<ScheduleTopicMode>("multi");
  /**
   * 이 큐의 글에 **활용할 브랜드**(2026-08-19). 빈 문자열이 '브랜드 없이'다.
   *
   * 배치 전체에 하나다 — 줄마다 다른 브랜드를 고르는 일이 없다. 이 큐를 거는 이유가
   * "이 소재들을 우리 서비스와 엮어 쓴다"이기 때문이고, 줄마다 칸을 하나 더 두면
   * 소재·플랫폼·분야·시각에 이어 다섯 번째가 된다.
   */
  const [brandId, setBrandId] = useState("");
  // 발행 시점을 정하는 방식. 기본은 **글마다 정한 절대 시각**이다(2026-08-05).
  // 예전의 간격 방식도 그대로 고를 수 있다 — 없애지 않고 별도 설정으로 남겼다.
  const [scheduleMode, setScheduleMode] = useState<ScheduleMode>("absolute");
  // 소재 순서와 짝을 이루는 발행 시각(datetime-local 값).
  const [publishTimes, setPublishTimes] = useState<string[]>([]);
  /**
   * 글마다 **어디에 올릴지**. 소재 순서와 짝을 이룬다.
   *
   * 식별자는 이미 있는 것을 쓴다(PostingChannel = "naver" | "threads") — 화면에 보이는
   * 한글('네이버'·'쓰레드')과 저장되는 값을 섞지 않기 위해서다. 복수 선택이므로 배열이고,
   * 서버로는 두 불리언(publishNaver·publishThreads)으로 옮겨 실린다.
   */
  const [platformsList, setPlatformsList] = useState<PostingChannel[][]>([]);
  /**
   * 글마다 **어느 분야의 소재인가**(2026-08-12). 소재 순서와 짝을 이룬다.
   *
   * 빈 문자열은 '고르지 않음'이다 — 그 줄은 서버로 값을 보내지 않고, 예전처럼 모델이
   * 소재 글자만 보고 판단한다. 플랫폼과 **같은 규칙으로 소재를 따라간다**(아래 effect).
   */
  const [categories, setCategories] = useState<string[]>([]);
  const [targetCount, setTargetCount] = useState(3);
  const [naverStatus, setNaverStatus] = useState<NaverStatus | null>(null);
  const [threadsStatus, setThreadsStatus] = useState<ThreadsStatus | null>(null);
  // 게시 대상을 정하는 곳은 **1걸음의 소재 줄 하나뿐**이다(platformsList).
  //
  // 예전에는 간격 방식만 2걸음의 '게시 플랫폼' 카드에서 배치 하나의 값으로 따로 받았다.
  // 같은 것을 두 곳에서 받으니 서로 다른 말을 했고 — 줄에는 '쓰레드', 요약에는
  // '네이버 2건' — 실제로는 줄의 선택이 버려졌다. 그 카드를 없앴다(2026-08-06 사용자 요청).
  const [view, setView] = useState<ScheduledBatchView | null>(null);
  /**
   * **내가 걸어 둔 예약 전부**(배치를 넘나든다). 작업 큐·발행 내역 탭이 이것을 읽는다.
   *
   * 활성 배치(`view`)만 보면 배치가 끝나는 순간 화면에서 **모든 기록이 사라진다** —
   * `/scheduled/naver/batches/active`는 끝난 배치를 돌려주지 않기 때문이다. 예약을 걸어
   * 두고 기다리던 사람에게는 그것이 "만든 글이 통째로 없어졌다"로 보인다(2026-08-06).
   * 서버에는 이 목록을 위한 조회가 이미 있었고, 화면만 그것을 쓰지 않고 있었다.
   */
  const [scheduledJobs, setScheduledJobs] = useState<ScheduledJob[]>([]);
  /**
   * jobId → 그 작업이 만든 **글의 실제 상태**. 서버가 목록과 함께 준다.
   *
   * 예전에는 응답에서 `item.job`만 꺼내 쓰고 나머지를 버렸다. 그래서 화면은 작업의
   * 상태밖에 몰랐는데, 그것은 그 실행이 끝났을 때의 **마지막 기억**일 뿐이다:
   *
   * - 제목이 없으면 어느 글인지 소재로만 짐작해야 한다.
   * - 작업이 원고 단계에서 실패해도 그 글은 완성돼 있을 수 있다.
   * - 발행이 실패로 기록돼도 사용자가 직접 발행했을 수 있다.
   * - 원고를 만드는 7분 동안 어느 칸인지 알 수 없다.
   *
   * 넷 다 2026-08-06 사용자 신고다.
   */
  const [jobPosts, setJobPosts] = useState<Record<string, JobPostState>>({});
  const [loading, setLoading] = useState(true);
  const [actionBusy, setActionBusy] = useState(false);

  // 어느 사용자의 응답인지 가르는 기준. 계정이 바뀌면 값이 바뀌고, 그 전에 띄운 요청의
  // 응답은 버려진다.
  const userRef = useRef(userId);
  userRef.current = userId;
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const topics = useMemo(() => normalizeTopics(topicsText), [topicsText]);

  /**
   * 실제로 만들 **글**의 소재 목록. 발행 시각·플랫폼은 소재가 아니라 이 목록과 짝을 이룬다.
   *
   * 소재 하나 모드에서는 같은 소재를 글 수만큼 되풀이한다. 소재 수만 보고 시각을 받으면
   * '소재 1개로 5편'을 예약해도 시각이 하나뿐이라 한 편만 올라간다.
   */
  const plannedTopics = useMemo(() => {
    if (topicMode !== "single") return topics;
    const first = topics[0];
    if (!first) return [];
    return Array.from({ length: Math.max(1, targetCount) }, () => first);
  }, [topicMode, topics, targetCount]);

  // 만들 글이 늘면 그 줄의 발행 시각 칸이 따라 생긴다. 이미 고른 시각은 그대로 두고,
  // 새로 생긴 줄만 기본값(첫 글 30분 뒤, 이후 1시간 간격)으로 채운다 — 사용자가 정한
  // 시각을 소재 한 줄 추가했다고 되돌리면 안 된다.
  const plannedCount = plannedTopics.length;
  // 지난 렌더의 소재 목록. 플랫폼 선택을 **자리가 아니라 소재로** 따라가게 하는 기준이다.
  const previousTopics = useRef<string[]>([]);
  useEffect(() => {
    // **기본은 비어 있다**(2026-08-12 사용자 결정). 빈 칸은 '앞 글이 발행되면 이어서'라는
    // 뜻이고, 그것이 이 화면의 기본 동작이다 — 시각을 미리 채워 두면 사용자가 정하지
    // 않은 약속이 걸린다. 이미 고른 시각은 줄이 늘어도 그대로 둔다.
    setPublishTimes((current) => {
      if (current.length === plannedCount) return current;
      return Array.from({ length: plannedCount }, (_, index) => current[index] ?? "");
    });
    /**
     * 플랫폼 선택은 **그 소재를 따라간다.**
     *
     * 예전에는 자리(index)만 보고 옮겼다. 그래서 가운데 소재의 글자를 지우면 남은 소재가
     * 한 칸씩 당겨지면서 **지운 소재의 선택을 물려받았다** — 3번 소재에 걸어 둔 쓰레드가
     * 2번 소재로 옮겨 가는 식이다(2026-08-05).
     *
     * 그래서 같은 소재가 있던 자리를 먼저 찾고, 없을 때만 자리로 물려받는다. 자리로
     * 물려받는 쪽도 필요하다 — 줄의 글자를 고쳐 쓰는 중에는 매 글자마다 '다른 소재'가
     * 되는데, 그때 선택이 기본값으로 되돌아가면 안 된다.
     */
    const before = previousTopics.current;
    previousTopics.current = plannedTopics;
    setPlatformsList((current) => {
      const next = plannedTopics.map((topic, index) => {
        const wasAt = before.indexOf(topic);
        return (
          (wasAt >= 0 ? current[wasAt] : undefined) ??
          current[index] ??
          (["naver"] as PostingChannel[])
        );
      });
      // 달라진 것이 없으면 같은 배열을 돌려준다 — 새 배열을 만들면 아래로 이어지는
      // 렌더가 매번 다시 돈다.
      return next.length === current.length && next.every((item, i) => item === current[i])
        ? current
        : next;
    });
    // 소재 분야도 **그 소재를 따라간다.** 플랫폼과 같은 이유다(위 주석) — 가운데 줄을
    // 지웠을 때 아래 줄이 지운 소재의 분야를 물려받으면 안 된다.
    setCategories((current) => {
      const next = plannedTopics.map((topic, index) => {
        const wasAt = before.indexOf(topic);
        return (wasAt >= 0 ? current[wasAt] : undefined) ?? current[index] ?? "";
      });
      return next.length === current.length && next.every((item, i) => item === current[i])
        ? current
        : next;
    });
  }, [plannedTopics, plannedCount]);

  /**
   * 소재별 한 편에서는 **글 수가 곧 소재 수다.**
   *
   * 예전에는 간격 방식에서 글의 개수를 따로 받았다. 그래서 소재 2개에 글 3편처럼 서로
   * 어긋난 값을 넣을 수 있었고, 그때마다 "글의 개수가 입력한 소재 수보다 많습니다"로
   * 막아 세워야 했다 — 사용자가 정할 것이 아닌 값을 받아 놓고 틀렸다고 말한 셈이다.
   * 이제 여기서 맞춘다(2026-08-05 사용자 요청). 화면은 결과만 보여 준다.
   *
   * 소재 하나 모드는 그대로다. 그쪽은 한 소재로 **몇 편을 만들지**를 사용자가 1걸음의
   * −·+로 정하는 것이라 소재 수에서 나올 수 없다.
   */
  useEffect(() => {
    if (topicMode === "single") return;
    // 아직 아무것도 적지 않았으면 손대지 않는다 — 0으로 떨어뜨리면 '새 예약 시작'으로
    // 비운 직후에 기본값까지 잃는다.
    if (topics.length === 0) return;
    setTargetCount((current) => (current === topics.length ? current : topics.length));
  }, [topicMode, topics.length]);

  /**
   * 예약 줄 하나를 통째로 뺀다(발행 시각 카드의 ×).
   *
   * 소재별 한 편에서는 소재까지 함께 뺀다 — 시각만 지우면 다음 렌더에서 그 소재의 줄이
   * 다시 생기고, 사용자는 지웠다고 생각한 글이 발행되는 것을 보게 된다.
   * 소재 하나 모드에서는 소재가 하나뿐이므로 '한 편 줄이기'가 곧 그 뜻이다.
   */
  const removeRow = useCallback(
    (index: number) => {
      if (topicMode === "single") {
        setTargetCount((current) => Math.max(1, current - 1));
      } else {
        setTopicsText(topics.filter((_, position) => position !== index).join("\n"));
      }
      setPublishTimes((current) => current.filter((_, position) => position !== index));
      // 소재를 빼면 그 소재의 플랫폼 선택도 함께 빠진다.
      setPlatformsList((current) => current.filter((_, position) => position !== index));
      // 소재 분야도 마찬가지다.
      setCategories((current) => current.filter((_, position) => position !== index));
    },
    [topics, topicMode],
  );

  const applyView = useCallback((owner: string | null, next: ScheduledBatchView | null) => {
    if (!mounted.current || userRef.current !== owner) return;
    setView(next);
  }, []);

  /** 예약 목록 응답을 작업 배열과 글 상태 표로 가른다. */
  const applyJobList = useCallback((owner: string | null, list: ScheduledJobList | null) => {
    if (!mounted.current || userRef.current !== owner) return;
    const items = list?.items ?? [];
    setScheduledJobs(items.map((item) => item.job));
    setJobPosts(() => {
      const next: Record<string, JobPostState> = {};
      for (const item of items) {
        // jobId로 담는다 — 같은 글을 가리키는 작업이 둘일 일은 없고, 화면은 언제나
        // 작업 줄에서 찾으므로 postId를 한 번 더 거칠 이유가 없다.
        next[item.job.jobId] = {
          title: item.title,
          status: item.postStatus,
          publishedUrl: item.publishedUrl,
          progress: item.progress,
          activityLog: item.activityLog,
        };
      }
      return next;
    });
  }, []);

  /** 예약 목록만 다시 읽는다. 실패해도 시끄럽게 알리지 않는다(표가 한 박자 늦을 뿐이다). */
  const refreshJobList = useCallback(async () => {
    const owner = userRef.current;
    if (!owner) return;
    try {
      applyJobList(owner, await request<ScheduledJobList | null>("/scheduled/naver/jobs"));
    } catch (error) {
      console.error(error);
    }
  }, [applyJobList]);

  /**
   * 네이버·스레드 계정 상태만 다시 읽는다.
   *
   * 이 두 값은 예약을 **시작할 수 있는지**를 정한다. 그런데 첫 로드에서 한 번만 읽고
   * 있어서, 설정 화면(또는 다른 창)에서 계정을 저장하고 돌아와도 화면은 여전히
   * "설정에서 Naver 계정을 먼저 저장해 주세요."라고 말했다 — 새로고침 말고는 푸는 길이
   * 없었다(2026-08-06 사용자 신고). 서버 쪽은 파일 몇 개를 보는 가벼운 조회다.
   */
  const refreshAccounts = useCallback(async () => {
    const owner = userRef.current;
    if (!owner) return;
    try {
      const [naver, threads] = await Promise.all([
        request<NaverStatus>("/naver/status"),
        request<ThreadsStatus>("/threads/status"),
      ]);
      if (!mounted.current || userRef.current !== owner) return;
      setNaverStatus(naver);
      setThreadsStatus(threads);
    } catch (error) {
      // 계정 상태를 한 번 못 읽는 것으로 화면을 시끄럽게 하지 않는다.
      console.error(error);
    }
  }, []);

  const refresh = useCallback(async () => {
    const owner = userRef.current;
    if (!owner) return;
    // 활성 배치와 **내 예약 전부**를 함께 읽는다. 배치가 끝나는 순간에도 목록 쪽에는
    // 마지막 상태가 남아야 작업 큐가 통째로 비지 않는다.
    //
    // **둘을 따로 적용한다.** 예전에는 Promise.all로 묶어 한 번에 넣었는데, 한쪽이
    // 실패하면(또는 느리면) 성공한 쪽까지 버려져 화면이 통째로 멈춰 있었다 — 진행 중인
    // 작업이 '대기'로 남아 보이던 원인 하나다(2026-08-06).
    const results = await Promise.allSettled([
      request<ScheduledBatchView | null>("/scheduled/naver/batches/active"),
      request<ScheduledJobList | null>("/scheduled/naver/jobs"),
    ]);
    const [batchResult, listResult] = results;
    if (batchResult.status === "fulfilled") applyView(owner, batchResult.value ?? null);
    if (listResult.status === "fulfilled") applyJobList(owner, listResult.value ?? null);
    // 폴링 실패는 시끄럽게 알리지 않는다 — 다음 주기에 다시 시도한다.
    for (const result of results) {
      if (result.status === "rejected") console.error(result.reason);
    }
  }, [applyView, applyJobList]);

  // 첫 로드: 네이버 상태와 활성 배치를 함께 읽는다.
  useEffect(() => {
    const owner = userId;
    if (!owner) {
      setView(null);
      setScheduledJobs([]);
      setJobPosts({});
      setNaverStatus(null);
      setThreadsStatus(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    void (async () => {
      try {
        const [status, threads, active, list] = await Promise.all([
          request<NaverStatus>("/naver/status"),
          request<ThreadsStatus>("/threads/status"),
          request<ScheduledBatchView | null>("/scheduled/naver/batches/active"),
          request<ScheduledJobList | null>("/scheduled/naver/jobs"),
        ]);
        if (!mounted.current || userRef.current !== owner) return;
        setNaverStatus(status);
        setThreadsStatus(threads);
        setView(active ?? null);
        applyJobList(owner, list ?? null);
      } catch (error) {
        if (mounted.current && userRef.current === owner) reportError(error);
      } finally {
        if (mounted.current && userRef.current === owner) setLoading(false);
      }
    })();
  }, [userId, reportError, applyJobList]);

  // 이 창으로 돌아오면 계정 상태를 다시 본다. 계정을 저장하는 곳은 설정 화면이고,
  // 사람은 대개 그쪽을 보고 온다 — 돌아오는 순간이 다시 읽기 가장 자연스러운 때다.
  useEffect(() => {
    if (!userId) return;
    const recheck = () => {
      if (document.visibilityState === "hidden") return;
      void refreshAccounts();
    };
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [userId, refreshAccounts]);

  // 돌고 있는 배치가 있으면 글의 개수·간격 칸을 **그 배치의 실제 값**으로 맞춘다.
  //
  // 배치는 시작할 때의 값을 그대로 들고 끝까지 간다. 그런데 이 칸들은 화면의 지역
  // 상태여서, 기본값을 바꾸거나 새로고침하면 칸에는 새 기본값이 뜨고 실제 예약은 옛
  // 값으로 도는 상태가 된다 — 화면이 1분이라 말하는데 5분마다 글이 올라가는 식이다.
  //
  // batchId가 바뀔 때만 맞춘다. 매번 맞추면 사용자가 다음 예약을 위해 고쳐 둔 값을
  // 폴링이 2초마다 되돌려 버린다.
  const activeBatchId = view?.batch.batchId ?? null;
  const syncedBatch = useRef<string | null>(null);
  useEffect(() => {
    if (!activeBatchId || !view) {
      if (!activeBatchId) syncedBatch.current = null;
      return;
    }
    if (syncedBatch.current === activeBatchId) return;
    syncedBatch.current = activeBatchId;
    setTargetCount(view.batch.targetCount);
    setTopicMode(view.batch.topicMode);
    // 옛 배치에는 brandId가 없다 — 그때는 브랜드를 쓰지 않았다.
    setBrandId(view.batch.brandId ?? "");
    // 옛 배치에는 scheduleMode가 없다 — 그때는 간격 방식이다.
    setScheduleMode(view.batch.scheduleMode ?? "interval");
    // 소재도 되살린다. 새로고침하면 입력칸이 비는데, 그 상태로는 값을 고쳐 다시
    // 시작하려 해도 "소재를 입력해 주세요"에 막혀 전부 다시 타이핑해야 한다.
    if (view.jobs.length > 0) {
      setTopicsText(view.jobs.map((job) => job.topic).join("\n"));
      // 예약된 시각도 입력칸으로 되돌린다(로컬 시간). 새로고침했다고 사용자가 정한
      // 시각이 화면에서 사라지면, 고쳐 다시 시작할 때 전부 다시 골라야 한다.
      // 입력칸은 작업 시각을 다룬다(2026-08-11) — 저장은 발행 시각이므로 되돌려 넣는다.
      setPublishTimes(view.jobs.map((job) => publishIsoToWorkStartInput(job.publishAt)));
      // 작업이 어디에 올라가기로 돼 있었는지 그대로 되살린다. publishNaver가 없는
      // 옛 작업은 네이버다(그때는 그것뿐이었다).
      setPlatformsList(
        view.jobs.map((job) => {
          const platforms: PostingChannel[] = [];
          if (job.publishNaver ?? true) platforms.push("naver");
          if (job.publishThreads) platforms.push("threads");
          return platforms;
        }),
      );
      // 소재 분야도 되살린다. 없는 작업(옛 작업·고르지 않은 줄)은 빈 값이다.
      setCategories(view.jobs.map((job) => job.subjectCategory ?? ""));
    }
  }, [activeBatchId, view]);

  // 폴링. 배치 상태가 끝났으면 멈춘다.
  const batchStatus = view?.batch.status ?? null;
  useEffect(() => {
    if (!userId || !batchStatus) return;
    const period = RUNNING_STATUSES.has(batchStatus)
      ? POLL_ACTIVE_MS
      : HALTED_STATUSES.has(batchStatus)
        ? POLL_IDLE_MS
        : 0;
    // COMPLETED·STOPPED·FAILED면 더 볼 것이 없다.
    if (period === 0) return;
    const timer = window.setInterval(() => void refresh(), period);
    return () => window.clearInterval(timer);
  }, [userId, batchStatus, refresh]);

  const runAction = useCallback(
    async (call: () => Promise<ScheduledBatchView | null>) => {
      const owner = userRef.current;
      setActionBusy(true);
      try {
        const next = await call();
        applyView(owner, next ?? null);
        // 시작·재시도·정지는 예약 목록도 바꾼다. 폴링을 기다리면 방금 만든 작업이
        // 몇 초 동안 작업 큐에 나타나지 않는다(배치가 곧바로 끝나면 영영 나타나지 않는다).
        void refreshJobList();
        return true;
      } catch (error) {
        reportError(error);
        return false;
      } finally {
        if (mounted.current) setActionBusy(false);
      }
    },
    [applyView, refreshJobList, reportError],
  );

  /**
   * 예약을 건다.
   *
   * 방식(시각을 정할지·소재를 어떻게 나눌지)은 기본이 **화면 상태**지만, 인자로
   * 덮어쓸 수 있다(2026-08-12). 「자동 포스팅」 탭이 그 자리다 — 그 화면은 언제나
   * '소재별 한 편'이고, 줄마다 시각을 적었는가로 방식을 정해 넘긴다. 상태를 미리
   * 바꿔 두는 방법은 쓰지 않는다: 도는 배치를 읽어 오는 동기화 effect가 그 값을
   * 되돌릴 수 있어, 사용자가 고르지 않은 방식으로 예약이 걸린다.
   */
  const start = useCallback(async (options?: {
    scheduleMode?: ScheduleMode;
    topicMode?: ScheduleTopicMode;
  }) => {
    // 같은 클릭이 두 번 도착해도 배치가 하나만 생기게 하는 열쇠. 요청마다 새로 만든다.
    const clientRequestId = crypto.randomUUID();
    /**
     * 글마다의 설정. **두 방식 모두 이것을 보낸다**(2026-08-06 사용자 요청).
     *
     * 플랫폼은 어느 방식이든 **소재 줄마다** 고른다. 예전에는 간격 방식만 이 배열을
     * 보내지 않아, 줄에 '쓰레드'라고 적어 두고도 배치 하나의 값(기본 네이버)으로
     * 발행됐다 — 화면의 요약이 '네이버 2건'이라고 말하던 그 어긋남이다.
     *
     * 발행 시각은 날짜·시각 방식에만 싣는다. 간격 방식은 앞 글이 끝나는 대로 이어서
     * 올라가므로 정해진 시각이 없고, 서버도 시각이 있고 없고로 두 방식을 가른다.
     */
    const mode = options?.scheduleMode ?? scheduleMode;
    const grouping = options?.topicMode ?? topicMode;
    const absolute = mode === "absolute";
    // 소재 하나 모드에서만 같은 소재를 되풀이한다. 인자로 방식을 덮어쓴 경우에는
    // plannedTopics(상태로 계산된 값)가 아니라 그 방식에 맞는 목록을 써야 한다.
    const rows = grouping === "single" ? plannedTopics : topics;
    const schedules = rows.map((topic, index) => {
      /**
       * 이 줄의 발행 시각. **줄마다 있을 수도 없을 수도 있다**(2026-08-12).
       *
       * 비어 있거나 읽을 수 없는 칸은 **키 자체를 보내지 않는다.** 임의로 채워 보내면
       * 사용자가 고르지 않은 시각에 글이 올라가고, 빈 칸의 뜻('앞 글이 끝나면')도
       * 사라진다. 서버는 시각이 없는 줄을 앞 줄에 매단다.
       */
      const publishAt = absolute
        ? workStartInputToPublishIso(publishTimes[index] ?? "")
        : null;
      return {
        topic,
        ...(publishAt ? { publishAt } : {}),
        // 고르지 않은 분야는 **보내지 않는다.** 빈 문자열을 보내면 서버가 목록 밖의
        // 값이라고 거절한다.
        ...(categories[index] ? { subjectCategory: categories[index] } : {}),
        // 고른 그대로 두 스위치로 옮긴다 — 네이버만·쓰레드만·둘 다가 모두 가능하다.
        publishNaver: (platformsList[index] ?? []).includes("naver"),
        publishThreads: (platformsList[index] ?? []).includes("threads"),
      };
    });
    const ok = await runAction(() =>
      request<ScheduledBatchView>("/scheduled/naver/batches", {
        method: "POST",
        body: {
          topics,
          // 소재별 한 편에서는 **줄 수가 곧 글 수다.** 방식을 인자로 덮어쓴 경우에도
          // 배치에 적히는 수가 실제로 만들 글 수와 같아야 한다.
          targetCount: grouping === "single" ? targetCount : rows.length,
          intervalSeconds: DEFAULT_INTERVAL_SECONDS,
          topicMode: grouping,
          // 배치의 이름표다. 어디에 올릴지는 글마다의 두 스위치가 정한다.
          platform: "naver",
          // 배치 기본값은 '한 줄이라도 그 플랫폼을 쓰는가'다 — 서버가 시작 전에
          // 연결을 확인하는 데 쓴다.
          publishNaver: schedules.some((item) => item.publishNaver),
          publishThreads: schedules.some((item) => item.publishThreads),
          clientRequestId,
          // 고르지 않은 브랜드는 **보내지 않는다.** 빈 문자열을 보내도 서버가 같은
          // 뜻으로 읽지만, 안 보내는 쪽이 "브랜드를 쓰지 않는 예약은 예전 그대로"라는
          // 사실을 요청 본문에서도 그대로 보여 준다.
          ...(brandId ? { brandId } : {}),
          schedules,
          // 시간대는 시각을 고른 방식에서만 뜻이 있다(표시·감사용).
          ...(absolute ? { timezone: browserTimeZone() } : {}),
        },
      }),
    );
    if (ok) {
      // 한 배치에 시각을 적은 줄과 안 적은 줄이 섞일 수 있다(2026-08-12). 전부 적었을
      // 때만 "정한 시각에 발행됩니다"라고 말한다 — 아니면 사실이 아니다.
      const allTimed = schedules.every((item) => "publishAt" in item);
      showToast(
        absolute && allTimed
          ? "예약을 등록했습니다. 정한 시각에 발행됩니다."
          : "예약을 시작했습니다.",
      );
    }
    return ok;
  }, [
    runAction,
    topics,
    plannedTopics,
    targetCount,
    topicMode,
    scheduleMode,
    publishTimes,
    platformsList,
    categories,
    brandId,
    showToast,
  ]);

  const batchId = view?.batch.batchId ?? null;

  const pause = useCallback(async () => {
    if (!batchId) return false;
    return runAction(() =>
      request<ScheduledBatchView>(`/scheduled/naver/batches/${batchId}/pause`, {
        method: "POST",
      }),
    );
  }, [batchId, runAction]);

  const resume = useCallback(async () => {
    if (!batchId) return false;
    return runAction(() =>
      request<ScheduledBatchView>(`/scheduled/naver/batches/${batchId}/resume`, {
        method: "POST",
      }),
    );
  }, [batchId, runAction]);

  /**
   * 배치를 버리고 처음 상태로 — '새 예약 시작' 버튼(2026-08-04 사용자 결정).
   *
   * 서버가 미완료 작업을 DB에서 지우고, 화면은 입력칸(소재·개수·간격)까지 전부
   * 기본값으로 되돌린다. 정말 백지에서 새 예약을 시작하는 동작이다.
   */
  const discard = useCallback(async () => {
    if (!batchId) return false;
    const owner = userRef.current;
    setActionBusy(true);
    try {
      await request(`/scheduled/naver/batches/${batchId}/discard`, { method: "POST" });
      if (mounted.current && userRef.current === owner) {
        setView(null);
        setTopicsText("");
        setTargetCount(3);
      }
      // 미완료 작업이 DB에서 지워졌다 — 목록도 다시 읽어야 지운 것이 표에 남지 않는다.
      void refreshJobList();
      showToast("예약을 정리했습니다. 새로 시작할 수 있습니다.");
      return true;
    } catch (error) {
      reportError(error);
      return false;
    } finally {
      if (mounted.current) setActionBusy(false);
    }
  }, [batchId, refreshJobList, reportError, showToast]);

  const retry = useCallback(
    async (jobId: string) =>
      runAction(() =>
        request<ScheduledBatchView>(`/scheduled/naver/jobs/${jobId}/retry`, {
          method: "POST",
        }),
      ),
    [runAction],
  );

  /**
   * 작업 하나를 큐에서 뺀다. 남은 작업은 순서대로 이어서 진행된다.
   *
   * 소재 입력칸도 함께 줄인다. batchId가 그대로라 위의 동기화 effect가 건너뛰는데,
   * 그대로 두면 지운 소재가 입력칸에 남아 있다가 다음 '예약 시작'에 되살아난다 —
   * 사용자가 방금 빼겠다고 한 소재의 글이 결국 발행된다.
   */
  const removeJob = useCallback(
    async (jobId: string) => {
      const owner = userRef.current;
      setActionBusy(true);
      try {
        const next = await request<ScheduledBatchView>(
          `/scheduled/naver/jobs/${jobId}`,
          { method: "DELETE" },
        );
        applyView(owner, next ?? null);
        void refreshJobList();
        if (next && mounted.current && userRef.current === owner) {
          setTopicsText(next.jobs.map((job) => job.topic).join("\n"));
          setTargetCount(next.batch.targetCount);
        }
        return true;
      } catch (error) {
        reportError(error);
        return false;
      } finally {
        if (mounted.current) setActionBusy(false);
      }
    },
    [applyView, refreshJobList, reportError],
  );

  /**
   * 발행 내역에서 한 줄을 지운다.
   *
   * 큐에서 빼는 것(`removeJob`)과 **응답을 다루는 방식이 다르다.** 서버는 지운 작업이
   * 속한 배치를 돌려주는데, 내역의 줄은 대개 **이미 끝난 옛 배치**의 것이다. 그것을
   * 활성 배치 자리에 넣으면 화면이 끝난 배치를 지금 도는 예약으로 착각하고, 소재
   * 입력칸까지 그 배치의 소재로 덮어쓴다. 그래서 여기서는 목록만 다시 읽는다.
   */
  const removeHistoryJob = useCallback(
    async (jobId: string) => {
      setActionBusy(true);
      try {
        await request(`/scheduled/naver/jobs/${jobId}`, { method: "DELETE" });
        await refreshJobList();
        return true;
      } catch (error) {
        reportError(error);
        return false;
      } finally {
        if (mounted.current) setActionBusy(false);
      }
    },
    [refreshJobList, reportError],
  );

  return {
    topicsText,
    setTopicsText,
    topics,
    plannedTopics,
    topicMode,
    setTopicMode,
    scheduleMode,
    setScheduleMode,
    publishTimes,
    setPublishTimes,
    platformsList,
    setPlatformsList,
    /** 소재 순서와 짝을 이루는 소재 분야. 빈 문자열은 '고르지 않음'이다. */
    categories,
    setCategories,
    /** 이 큐의 글에 활용할 브랜드. 빈 문자열이 '브랜드 없이'다(배치 전체에 하나). */
    brandId,
    setBrandId,
    removeRow,
    targetCount,
    setTargetCount,
    naverStatus,
    threadsStatus,
    batch: view?.batch ?? null,
    /** 지금 돌고 있는 배치의 작업들. 3걸음의 제어 화면이 쓴다. */
    jobs: view?.jobs ?? [],
    /** 배치를 넘나드는 **내 예약 전부**. 작업 큐·발행 내역 탭이 쓴다. */
    scheduledJobs,
    /** jobId → 그 작업이 만든 글의 실제 상태(제목·상태·발행 주소·진행 칸). */
    jobPosts,
    loading,
    actionBusy,
    start,
    pause,
    resume,
    discard,
    retry,
    removeJob,
    removeHistoryJob,
    refresh,
  };
}
