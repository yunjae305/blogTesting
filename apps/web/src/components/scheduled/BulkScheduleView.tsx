import type { PostingChannel } from "../../api/types";
import { useStore } from "../../store";
// 소재 화면과 **같은 고르기 칸**을 쓴다 — 브랜드 추가·관리도 그 자리에서 열린다.
import { BrandPicker } from "../write/BrandPicker";
import { LiveSessionsPanel } from "../LiveSessionsPanel";
import { BulkHelperCard } from "./BulkHelperCard";
import { ClipboardIcon, PlayIcon } from "./icons";
import { MaterialInputList } from "./MaterialInputList";
import { PlatformCountBadges } from "./PlatformToggle";
import { MIN_PUBLISH_GAP_MINUTES, isPast, localInputToIso, tooCloseIndex } from "./schedule";
import { countPublishJobs } from "./scheduleSummary";
import { MAX_TOPICS, useScheduledPosting } from "./useScheduledPosting";

/**
 * 「자동 포스팅」 — 여러 소재를 한 번에 걸고 **손대지 않는** 자리(2026-08-12).
 *
 * 이름이 '예약'이 아닌 이유는 예약이 이 화면의 특징이 아니기 때문이다 — 시각은 줄마다
 * 비울 수 있고, 새 글 작성에도 작업 시각 칸이 있다. 여기만의 것은 **제목·방향까지
 * 서버가 정한다**는 것이고, 그것이 '자동'이다.
 *
 * ## 왜 새 글 작성과 따로 있는가
 *
 * 새 글 작성은 한 편을 사용자가 끌고 간다 — 목적·연령대·제목·방향을 직접 고른다. 그
 * 세밀함이 필요할 때는 그쪽이 맞지만, 소재 열 개를 걸어 두려면 같은 걸음을 열 번 밟아야
 * 한다. 이 화면은 그 반대쪽이다: **소재와 플랫폼만 정하면 나머지는 서버가 한다** —
 * 트렌드 키워드 선택 → 제목 생성 → 자료 수집 → 방향 결정 → 원고 → 발행.
 *
 * 그래서 **새 글 작성은 건드리지 않는다**(2026-08-12 사용자 요청). 두 화면은 같은
 * 파이프라인을 부르지만 사람이 어디까지 정하느냐가 다르고, 그 차이가 존재 이유다.
 *
 * ## 여기서 받지 않는 것
 *
 * **발행 시각을 받지 않는다**(2026-08-12 사용자 결정). 시작을 누르면 첫 글부터 돌고,
 * 앞 글이 발행되면 다음 글이 이어서 돈다(간격 방식). 편마다 시각을 받으면 한 편에
 * 5~8분이 걸리는 것을 사용자가 계산해 띄워야 하는데, 그것이 '따로 설정하지 않는다'는
 * 이 화면의 뜻과 어긋난다. 시각을 정해 걸고 싶으면 새 글 작성에 그 자리가 있다.
 *
 * 걸어 둔 뒤의 진행·발행 내역은 **「작업 관리」 탭**이 보여 준다. 시작하면 그리로 옮긴다 —
 * 아무 일도 없는 빈 화면 앞에 남겨 두지 않는다.
 */
export function BulkScheduleView() {
  const { session } = useStore();
  const scheduled = useScheduledPosting();
  const {
    topicsText,
    setTopicsText,
    topics,
    platformsList,
    setPlatformsList,
    categories,
    setCategories,
    brandId,
    setBrandId,
    publishTimes,
    setPublishTimes,
    naverStatus,
    threadsStatus,
    jobs,
    actionBusy,
  } = scheduled;

  const setPlatformsAt = (index: number, platforms: PostingChannel[]) =>
    setPlatformsList((current) =>
      current.map((item, position) => (position === index ? platforms : item)),
    );
  const setCategoryAt = (index: number, category: string) =>
    setCategories((current) =>
      current.map((item, position) => (position === index ? category : item)),
    );
  const setWorkStartAt = (index: number, value: string) =>
    setPublishTimes((current) =>
      current.map((item, position) => (position === index ? value : item)),
    );

  const plannedCount = topics.length;
  /**
   * 줄마다의 **작업 시각**. 빈 칸은 '앞 글이 발행되면 이어서'다(2026-08-12 사용자 결정).
   *
   * 그래서 이 화면은 두 방식을 한 배치에 섞어 보낸다 — 적은 줄은 절대 시각, 안 적은
   * 줄은 앞 줄에 매달린다. 서버가 그 둘을 함께 받는다.
   */
  const times = publishTimes.slice(0, plannedCount);
  const anyTimed = times.some((value) => localInputToIso(value) !== null);
  // 적었는데 읽을 수 없는 칸(예: 날짜만 고르고 시간을 비운 상태).
  const brokenTime = times.findIndex(
    (value) => value.trim() !== "" && localInputToIso(value) === null,
  );
  const pastTime = times.findIndex((value) => isPast(value));
  // 적은 칸끼리 너무 붙어 있는가. 비운 칸은 건너뛴다 — 약속한 시각이 없어 겹칠 것도 없다.
  const tooClose = tooCloseIndex(times);
  const invalidTimeIndex = brokenTime >= 0 ? brokenTime : pastTime >= 0 ? pastTime : tooClose;
  // 발행 작업 수 = 소재마다 고른 플랫폼 수의 합. 한 소재를 두 곳에 올리면 2건이다.
  const jobCounts = countPublishJobs(platformsList, plannedCount);
  // 적어 둔 소재인데 올릴 곳이 없는 줄. 있으면 시작할 수 없다.
  const missingPlatform = platformsList
    .slice(0, plannedCount)
    .findIndex((platforms) => platforms.length === 0);

  const wantsNaver = platformsList
    .slice(0, plannedCount)
    .some((platforms) => platforms.includes("naver"));
  const wantsThreads = platformsList
    .slice(0, plannedCount)
    .some((platforms) => platforms.includes("threads"));

  // 지금 글을 쓰거나 발행하는 중이면 새 배치를 시작할 수 없다 — 서버도 같은 것을 막는다
  // (도는 LLM·셀레니움을 버리면 네이버에 올라갔는지 알 수 없는 글이 생긴다).
  const executing = jobs.some(
    (job) => job.status === "RUNNING" || job.status === "PUBLISHING",
  );

  /**
   * 발행 계정은 **Blog-it 계정마다 따로** 저장된다. 그래서 막힌 이유에 지금 로그인한
   * 계정을 함께 적는다 — 다른 계정으로 들어와 있으면 "저장했는데 왜 또 저장하라고 하나"가
   * 된다(2026-08-06에 실제로 그랬다).
   */
  const account = (session?.user.email || session?.user.nickname || "").trim();
  const forThisAccount = account ? `'${account}' 계정에 ` : "";

  /**
   * 시작을 막는 첫 번째 이유 **하나만** 보여 준다. 여럿을 늘어놓으면 무엇부터 고쳐야
   * 하는지 오히려 흐려진다.
   *
   * **계정이 없다고 플랫폼 버튼을 잠그지 않는다**(2026-08-05 사용자 요청). 고르는 것은
   * 자유롭게 두고 시작할 때 말한다 — 서버도 시작 시점에 '쓰는 곳'만 확인한다.
   */
  const blockedReason = ((): string | null => {
    if (executing) return "글을 쓰거나 발행하는 중입니다. 끝난 뒤에 다시 시작할 수 있습니다.";
    if (topics.length === 0) return "글로 만들 소재를 한 개 이상 입력해 주세요.";
    if (plannedCount > MAX_TOPICS)
      return `한 번에 걸 수 있는 글은 최대 ${MAX_TOPICS}편입니다.`;
    if (missingPlatform >= 0)
      return `${missingPlatform + 1}번째 소재의 발행 플랫폼을 하나 이상 선택해 주세요.`;
    // 시각은 **비워 두는 것이 정상**이다. 여기서 막는 것은 '적었는데 쓸 수 없는' 값뿐이다.
    if (brokenTime >= 0)
      return `${brokenTime + 1}번째 소재의 작업 시각을 끝까지 정하거나 비워 주세요.`;
    if (pastTime >= 0)
      return `${pastTime + 1}번째 소재의 작업 시각이 이미 지났습니다. 지금보다 뒤의 시각을 골라 주세요.`;
    // 발행은 한 번에 하나씩 돈다. 촘촘한 약속은 받아 봐야 지킬 수 없다(서버도 거부한다).
    if (tooClose >= 0)
      return `${tooClose + 1}번째 소재의 작업 시각을 다른 글과 ${MIN_PUBLISH_GAP_MINUTES}분 이상 떨어뜨려 주세요.`;
    if (wantsNaver && !naverStatus?.saved)
      return `${forThisAccount}저장된 Naver 계정이 없습니다. 설정에서 저장해 주세요.`;
    if (wantsThreads && !threadsStatus?.saved)
      return `${forThisAccount}저장된 Threads 계정이 없습니다. 설정에서 저장해 주세요.`;
    return null;
  })();

  /**
   * 예약을 걸고 「작업 관리」의 작업 큐로 옮긴다.
   *
   * 방식을 **인자로 못 박는다.** 한 줄이라도 시각을 적었으면 절대 시각 방식이다 —
   * 그래야 워커가 그 약속을 보고, 시각을 비운 줄은 그 안에서 앞 줄을 기다린다. 훅의
   * 상태를 미리 바꾸는 방법은 쓰지 않는다(도는 배치를 읽어 오는 동기화가 되돌린다).
   *
   * 실패하면 이 자리에 남는다. 큐로 먼저 옮기면 아무 일도 일어나지 않은 빈 큐 앞에 선다.
   */
  const startAndOpenQueue = async () => {
    const ok = await scheduled.start({
      scheduleMode: anyTimed ? "absolute" : "interval",
      topicMode: "multi",
    });
    if (ok) location.hash = "#/scheduled/queue";
  };

  return (
    <div className="scheduled-page">
      <div className="reservation-top">
        <header className="reservation-header">
          <div className="reservation-header-copy">
            <span className="reservation-mark" aria-hidden="true" />
            <div>
              <h1 className="reservation-title">자동 포스팅</h1>
              <p className="reservation-subtitle">
                소재와 플랫폼만 정하면 제목·자료·원고·발행까지 알아서 진행합니다.
              </p>
            </div>
          </div>
        </header>
      </div>

      {/* 자동 포스팅이 서버에서 크롬을 열어 발행 중이면 그 화면을 여기서도 중계한다 —
          발행이 없는 동안에는 아무것도 그려지지 않는다(설정·발행 화면과 같은 패널). */}
      <LiveSessionsPanel kinds={["publish"]} />

      {/* 두 칸으로 나눈다(2026-08-12 사용자 요청). 설명을 입력칸 **위**에 문단으로 두면
          정작 눈이 가야 할 자리를 글자가 덮는다 — 옆으로 옮겨 두면 필요할 때만 본다.
          새 글 작성의 「이 글 요약」이 서 있는 자리와 같은 구조다. */}
      <div className="reservation-grid reservation-grid--bulk">
        <div className="reservation-column">
          <section className="panel scheduled-panel" aria-labelledby="bulk-topic-title">
            <div className="panel-header">
              <h2 className="panel-title" id="bulk-topic-title">
                <span className="scheduled-panel-icon" aria-hidden="true">
                  <ClipboardIcon />
                </span>
                포스팅 소재·플랫폼 선택
              </h2>
            </div>
            <div className="panel-body">
              {/* 여기에 있던 안내 문단은 옆의 「도우미」 카드로 옮겼다. 남은 것은 입력칸
                  머리의 한 줄(MaterialInputList)뿐이고, 그것은 '지금 할 일'만 말한다. */}
              <MaterialInputList
                value={topicsText}
                onChange={setTopicsText}
                maxRows={MAX_TOPICS}
                platformsList={platformsList}
                onPlatformsChangeAt={setPlatformsAt}
                categories={categories}
                onCategoryChangeAt={setCategoryAt}
                workStartTimes={publishTimes}
                onWorkStartAtChangeAt={setWorkStartAt}
                invalidTimeIndex={invalidTimeIndex}
                disabled={executing}
              />

              {/* 이 큐 전체에 활용할 브랜드(2026-08-19). **줄마다가 아니라 배치 하나에
                  하나**다 — 이 화면으로 큐를 거는 이유가 "이 소재들을 우리 서비스와
                  엮어 쓴다"이고, 줄마다 칸을 하나 더 두면 소재·플랫폼·분야·시각에 이어
                  다섯 번째가 된다.

                  소재는 줄마다 있으므로 브랜드는 언제나 **활용한 도구**로 들어간다
                  (새 글 작성의 역할 고르기가 여기 없는 이유다). */}
              <div className="bulk-brand-field">
                <BrandPicker
                  brandId={brandId}
                  onChange={(nextId) => setBrandId(nextId)}
                >
                  <p className="field-desc">
                    {brandId
                      ? "각 글은 소재가 주인공이고, 이 브랜드는 그 과정에서 쓴 도구로 등장합니다."
                      : "고르면 모든 글에 이 브랜드를 자연스럽게 엮습니다. 비워 두면 브랜드 없이 씁니다."}
                  </p>
                </BrandPicker>
              </div>
            </div>
          </section>

          <section className="panel scheduled-panel" aria-labelledby="bulk-start-title">
            <div className="panel-header">
              <h2 className="panel-title" id="bulk-start-title">
                <span className="scheduled-panel-icon" aria-hidden="true">
                  <PlayIcon />
                </span>
                시작
              </h2>
            </div>
            <div className="panel-body">
              {/* 두 숫자를 갈라 적는다: 만들어질 **원고** 수와, 그 원고가 **올라가는**
                  횟수. 뭉치면 한 소재가 두 곳에 간다는 사실이 화면에서 사라진다
                  (2026-08-05 사용자 결정과 같은 셈). */}
              <div className="bulk-summary">
                <span className="bulk-summary-count">
                  생성 예정 글 <strong>{plannedCount}</strong>편
                </span>
                <span className="bulk-summary-count">
                  발행 작업 <strong>{jobCounts.total}</strong>건
                </span>
                <PlatformCountBadges counts={jobCounts} />
              </div>
              {/* 이 한 줄은 **지금 적어 둔 값**이 어떻게 도는지를 말한다(도우미 카드는
                  규칙을 말하고, 여기는 그 규칙이 이번 입력에 어떻게 적용되는지를 말한다).
                  '진행은 작업 관리 탭에서'는 도우미로 옮겨 여기서 뺐다. */}
              <p className="scheduled-card-lead">
                {anyTimed
                  ? "시각을 적은 글은 그 시각에, 비워 둔 글은 앞 글이 발행된 뒤 이어서 진행합니다."
                  : "시작하면 첫 글부터 바로 만들기 시작하고, 앞 글이 발행되면 다음 글이 이어서 진행됩니다."}
              </p>
              {/* 막힌 이유는 **버튼 옆이 아니라 버튼 위**에 둔다. 잠긴 버튼을 눌러 보고
                  이유를 찾게 하지 않는다. */}
              {blockedReason && <p className="schedule-start-blocked">{blockedReason}</p>}
              <div className="bulk-actions">
                <button
                  type="button"
                  className="button primary"
                  disabled={Boolean(blockedReason) || actionBusy}
                  onClick={() => void startAndOpenQueue()}
                >
                  자동 포스팅 시작
                </button>
              </div>
            </div>
          </section>
        </div>

        {/* 오른쪽 칸. 지금은 도우미 하나뿐이라 컬럼으로 감싸 두어, 나중에 카드가
            늘어도 자리가 흔들리지 않는다. */}
        <div className="reservation-column">
          <BulkHelperCard />
        </div>
      </div>
    </div>
  );
}
