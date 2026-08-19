"""예약 포스팅의 저장·통신 모델.

배치(ScheduledBatch) 하나가 사용자가 '예약 시작'을 누른 한 번이고, 그 안의 작업
(ScheduledJob) 하나가 소재 하나다. 두 문서는 BlogTask와 **분리**된다 — 예약은 글의
속성이 아니라 글들을 엮는 별도의 흐름이고, BlogTask 문서에 배치를 밀어 넣으면 예약을
쓰지 않는 글까지 그 필드를 이고 다니게 된다.

작업은 자기가 만든 글을 ``post_id``로만 가리킨다. 원고·이미지·발행 기록은 전부 기존
BlogTask에 있고 여기로 복사하지 않는다 — 복사하면 두 벌이 어긋난다.

자격 증명은 이 문서 어디에도 담지 않는다(네이버 아이디·비밀번호는 DB 밖 로컬 파일에
있다). 배치는 '네이버로 발행한다'는 사실만 안다.
"""

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from app.shared.base import CamelModel
from app.shared.blog_task import ActivityEntry, TaskProgress
from app.shared.status import BlogTaskStatus


class SchedulePlatform(StrEnum):
    """예약 배치의 이름표. 저장·조회 경로(``/scheduled/naver/...``)가 이 값을 쓴다.

    **어디에 올릴지는 이 값이 정하지 않는다.** 실제 게시 대상은 작업마다의
    ``publish_naver``·``publish_threads`` 두 스위치다 — 네이버만, 쓰레드만, 둘 다가
    모두 가능하다(2026-08-06). 두 벌로 두면 언젠가 서로 어긋나므로 여기에는 채널을
    더하지 않는다.
    """

    NAVER = "naver"


class ScheduleMode(StrEnum):
    """배치가 **언제 발행할지**를 정하는 방식. 두 개념을 가르는 자리다.

    - INTERVAL: 앞 글이 발행된 뒤 ``interval_seconds``만큼 지나면 다음 글을 만들어 올린다.
      절대 시각이 없으므로 "몇 시에 올라가는가"는 앞 글이 언제 끝나느냐에 달렸다.
      2026-08-05 이전의 유일한 방식이고, 옛 문서·옛 클라이언트가 이 값이다.
    - ABSOLUTE: 글마다 **절대 발행 시각**(ScheduledJob.publish_at)을 들고, 그 시각에 올린다.
      ``interval_seconds``는 그대로 남지만 뜻이 다르다 — 발행 간격이 아니라 **원고 생성
      작업을 띄우는 간격**이다(LLM·크롬을 동시에 여러 개 돌리지 않기 위한 것).
    """

    INTERVAL = "interval"
    ABSOLUTE = "absolute"


class ScheduleTopicMode(StrEnum):
    """소재를 글로 나누는 방식.

    - MULTI: 소재 하나에 글 한 편. 입력한 소재들이 그대로 작업이 된다(기본).
    - SINGLE: 소재 하나로 여러 편. 같은 소재를 서로 다른 각도로 쓴다.
    """

    MULTI = "multi"
    SINGLE = "single"


class ScheduledBatchStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ScheduledJobStatus(StrEnum):
    """작업 하나의 상태. **예약 상태(scheduleStatus)를 따로 두지 않는다** — 그 뜻은 전부
    이 값 하나에 이미 있고, 두 벌로 두면 언젠가 서로 어긋난다.

    2026-08-05 요구사항의 상태 이름과의 대응:

    ==================  ====================================================
    요구사항             여기서는
    ==================  ====================================================
    draft               WAITING이면서 publish_at이 없음(간격 방식·옛 배치)
    scheduled           WAITING이면서 publish_at이 있음
    preparing           RUNNING (글·트렌드·원고를 만드는 중)
    (준비 완료)          READY_TO_PUBLISH (원고를 다 만들고 발행 시각을 기다림)
    publishing          PUBLISHING
    published           COMPLETED
    failed              FAILED / NEEDS_HUMAN(사람 확인이 필요한 실패)
    cancelled           CANCELED (문서는 지우지 않고 상태만 남긴다)
    ==================  ====================================================
    """

    WAITING = "WAITING"
    RUNNING = "RUNNING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    CANCELED = "CANCELED"


#: 아직 발행되지 않았고 사용자가 시각·플랫폼을 고칠 수 있는 상태.
#: PUBLISHING·COMPLETED는 여기 없다 — 올라가는 중이거나 이미 올라간 글의 예약을 고치면
#: 화면과 실제 게시물이 다른 말을 하게 된다.
RESCHEDULABLE_JOB_STATUSES = frozenset(
    {
        ScheduledJobStatus.WAITING,
        ScheduledJobStatus.READY_TO_PUBLISH,
        ScheduledJobStatus.FAILED,
        ScheduledJobStatus.NEEDS_HUMAN,
    }
)


#: 더 진행할 것이 없는 상태. **순차 실행의 문지기**가 이것을 본다(2026-08-12).
#:
#: 실패(FAILED)와 사람 확인(NEEDS_HUMAN)도 여기 든다 — 앞 편이 실패했다고 뒤 편을
#: 멈추지 않는다는 사용자 결정이다. 한 편이 실패했을 때 나머지가 영원히 대기하면,
#: 사용자는 실패를 손으로 치우기 전까지 아무 글도 받지 못한다.
FINISHED_JOB_STATUSES = frozenset(
    {
        ScheduledJobStatus.COMPLETED,
        ScheduledJobStatus.FAILED,
        ScheduledJobStatus.NEEDS_HUMAN,
        ScheduledJobStatus.CANCELED,
    }
)


class ScheduledJobStage(StrEnum):
    """작업이 지금 어느 칸에 있는지. 화면의 상태 문구가 이 값을 읽는다."""

    CREATE_POST = "CREATE_POST"
    TREND_RECOMMENDATION = "TREND_RECOMMENDATION"
    TITLE_GENERATION = "TITLE_GENERATION"
    SEARCH_ANALYSIS = "SEARCH_ANALYSIS"
    INTENT_SELECTION = "INTENT_SELECTION"
    DRAFT_GENERATION = "DRAFT_GENERATION"
    NAVER_PUBLISH = "NAVER_PUBLISH"
    # 네이버 발행 성공 뒤 같은 원고를 스레드에도 올리는 단계. publish_threads가 켜진
    # 작업만 지난다. init_mongo.py의 stage enum에도 같은 값이 있어야 저장이 통과한다.
    THREADS_PUBLISH = "THREADS_PUBLISH"
    DONE = "DONE"


#: 워커가 계속 들여다봐야 하는 배치 상태. 서버 재시작 복구도 이 목록으로 찾는다.
ACTIVE_BATCH_STATUSES = frozenset(
    {
        ScheduledBatchStatus.READY,
        ScheduledBatchStatus.RUNNING,
        ScheduledBatchStatus.PAUSE_REQUESTED,
        ScheduledBatchStatus.STOP_REQUESTED,
        ScheduledBatchStatus.PAUSED,
        ScheduledBatchStatus.NEEDS_HUMAN,
    }
)

#: 사용자당 하나만 허용하는 '실행 중' 판정에 쓰는 상태. PAUSED·NEEDS_HUMAN도 포함한다 —
#: 멈춰 있어도 그 배치는 아직 사용자의 것이고, 재개하면 이어서 돈다.
OCCUPYING_BATCH_STATUSES = ACTIVE_BATCH_STATUSES


class ScheduledLogEntry(CamelModel):
    """사용자에게 보여 줄 한 줄.

    비밀번호·쿠키·토큰·셀레니움 스택은 여기 담지 않는다. 서버 로그에는 남기되 화면에는
    사람이 다음에 무엇을 할지 정할 수 있는 문장만 올린다.
    """

    at: str
    message: str
    #: success | info | muted — 화면의 점 색만 정한다.
    tone: str = "info"
    job_id: str | None = None


class ScheduledJob(CamelModel):
    job_id: str
    batch_id: str
    user_id: str
    platform: SchedulePlatform = SchedulePlatform.NAVER
    #: 배치 안의 순서(0부터). 화면 표시와 실행 순서가 모두 이 값을 따른다.
    sequence: int
    topic: str
    #: 같은 소재의 몇 번째 글인가(0부터). 소재 하나로 여러 편을 쓸 때 서로 다른 트렌드
    #: 키워드를 골라 각도를 벌리는 데 쓴다. 소재별 한 편 모드에서는 전부 0이다.
    variant_index: int = 0
    #: **한 번에 건 묶음**의 id(2026-08-13 사용자 지적). 화면의 '1편째·2편째'는 이 묶음
    #: 안에서 센다.
    #:
    #: 배치는 묶음이 아니다 — 새 글 작성에서 건 예약은 **돌고 있는 배치에 계속 붙는다.**
    #: 그래서 소재만 보고 세면 어제 건 글까지 한 줄에 세워져 "6편째"가 나온다(사용자가
    #: 본 것이 그것이다). 등록 한 번이 곧 한 묶음이다.
    #:
    #: 옛 작업 문서에는 없으므로 기본 None — 그때는 화면이 예전처럼 소재로 묶는다.
    series_id: str | None = None
    #: 이 소재가 어느 분야의 것인가(SUBJECT_CATEGORIES 중 하나, 2026-08-12). 글을 만들 때
    #: BlogTask의 subject_category로 그대로 넘어가 동명이의어를 가른다.
    #:
    #: 옛 작업 문서에는 없으므로 기본 None — 그때는 예전 그대로 소재 글자만 보고 판단한다.
    subject_category: str | None = None
    #: 이 글에 **활용할 브랜드**(2026-08-19). 새 글 작성의 브랜드 선택과 같은 값이다.
    #:
    #: 배치 전체에 하나로 걸리지만(줄마다 다른 브랜드를 쓰는 일은 없다) 값은 작업마다
    #: 들고 있는다 — 작업이 스스로 글을 만들기 때문이다(`_ensure_post`). 배치에서
    #: 찾아 쓰면 재시도·재예약 때마다 배치를 다시 읽어야 하고, 배치가 지워진 뒤의
    #: 작업은 브랜드를 잃는다.
    #:
    #: 역할은 언제나 활용(UTILITY)이다 — 소재가 반드시 있는 흐름이라 브랜드가 주인공일
    #: 수 없다. 판정은 글을 만들 때 `with_brand_materials`가 한다.
    #:
    #: 옛 작업 문서에는 없으므로 기본 None — 그때는 예전 그대로 브랜드 없이 만든다.
    brand_id: str | None = None
    #: 이 작업이 만든 글. 재시도해도 새로 만들지 않고 이 값을 다시 쓴다.
    post_id: str | None = None
    #: 사용자가 새 글 작성에서 **방향까지 골라 둔 글**을 넘겨받은 작업인가(2026-08-11).
    #:
    #: True면 준비 단계가 소재→트렌드→제목→검증→의도 선택을 전부 건너뛴다. 그 다섯은
    #: 사용자가 이미 손으로 정했고, 자동으로 다시 고르면 사용자가 고른 글이 아니게 된다.
    #: 대신 **자료만 새로 모아** 원고를 만든다 — 방향은 사람의 판단이라 며칠 뒤에도
    #: 유효하지만 자료는 낡기 때문이고, 이 예약의 존재 이유가 바로 그것이다.
    #:
    #: 옛 작업 문서에는 없으므로 기본 False — 그때는 예전 그대로 소재부터 만든다.
    starts_from_prepared_post: bool = False
    #: 이 작업보다 **먼저 끝나야 하는** 작업의 id(2026-08-12). 한 소재로 여러 편을 만들 때
    #: 2편·3편이 1편을 가리킨다.
    #:
    #: 편마다 시각을 받지 않는 이유가 이것이다 — 한 편이 5~8분 걸리므로 시각을 따로 받으면
    #: 사용자가 그 시간을 계산해 간격을 띄워야 한다. 대신 앞 편이 **끝나면**(성공이든
    #: 실패든, FINISHED_JOB_STATUSES) 다음 편이 시작한다.
    #:
    #: 옛 작업 문서에는 없으므로 기본 None — 그때는 예전처럼 자기 시각만 보고 시작한다.
    after_job_id: str | None = None
    #: 이 원고를 네이버에 올릴지. **옛 문서에는 없으므로 기본 True** — 그때는 네이버가
    #: 언제나 발행 대상이었고, False로 읽으면 돌고 있던 예약이 네이버를 건너뛴다.
    #: 이 값이 False면 쓰레드 단독 예약이다(2026-08-06).
    publish_naver: bool = True
    #: 같은 원고를 스레드에도 올릴지. 옛 문서에는 없으므로 기본 False — 그때는 예전과
    #: 같이 네이버만 발행한다. 둘 다 켜면 네이버가 먼저, 스레드가 뒤다.
    publish_threads: bool = False
    status: ScheduledJobStatus = ScheduledJobStatus.WAITING
    stage: ScheduledJobStage = ScheduledJobStage.CREATE_POST
    #: 이 글을 **실제로 게시할 절대 시각**(UTC ISO). 예약 방식이 ABSOLUTE인 배치의 작업만
    #: 값을 갖는다. 없으면 간격 방식이라 앞 글이 끝나는 대로 이어서 올라간다.
    #:
    #: 저장은 언제나 UTC 한 가지 기준이다. 사용자가 어느 시간대에서 골랐는지는 아래
    #: ``timezone``에 따로 적고, 화면 표시는 클라이언트가 자기 로컬 시간으로 한다 —
    #: 서버가 시간대 변환을 하지 않으므로 날짜가 하루 밀리는 종류의 버그가 생기지 않는다.
    publish_at: str | None = None
    #: 사용자가 **작업 시각을 고르지 않아** 지금 바로 시작한 작업인가(2026-08-13).
    #:
    #: 새 글 작성의 '지금 바로'로 걸린 작업이 여기다. 그런 작업도 `publish_at`은 채워져
    #: 있다 — 걸린 시각이 들어간다(표시·정렬에 쓴다). 그래서 `publish_at`만으로는
    #: "사용자가 정한 약속"과 "그냥 지금"을 가를 수 없어 이 칸을 따로 둔다.
    #:
    #: 발행 순서가 이 값을 본다: **시각을 고르지 않은 작업이 먼저 올라간다**(사용자 지시).
    #: 예약을 건 글의 원고가 먼저 끝나도, 즉시 작업이 올라갈 때까지 기다린다.
    #:
    #: 옛 작업 문서에는 없으므로 기본 False — 그때는 예전 그대로 시각 순으로 올라간다.
    starts_immediately: bool = False
    #: 사용자가 시각을 고를 때 쓰던 시간대(IANA, 예: "Asia/Seoul"). 표시·감사용이며
    #: 계산에는 쓰지 않는다. 옛 작업에는 없다.
    timezone: str | None = None
    #: 마지막으로 발행을 **시도한** 시각. 성공 시각(published_at)과 다르다 — 실패한
    #: 시도도 여기 남아야 "몇 시에 시도해서 실패했는가"를 볼 수 있다.
    last_attempt_at: str | None = None
    #: 자동 발행 재시도 횟수. 사용자가 누른 재시도(retry_count)와 섞지 않는다 —
    #: 자동 재시도 한도는 이 값으로만 판단한다.
    publish_attempts: int = 0
    #: 작업이 시작 가능해진 시각(표시용). **발행 시각이 아니다** — 그것은 publish_at이다.
    scheduled_at: str | None = None
    started_at: str | None = None
    generated_at: str | None = None
    published_at: str | None = None
    post_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    created_at: str
    updated_at: str


class ScheduledBatch(CamelModel):
    batch_id: str
    user_id: str
    platform: SchedulePlatform = SchedulePlatform.NAVER
    #: 소재를 글로 나누는 방식. 옛 문서에는 없으므로 기본은 예전 동작(소재별 한 편)이다.
    topic_mode: ScheduleTopicMode = ScheduleTopicMode.MULTI
    #: 발행 시점을 정하는 방식. 옛 문서에는 없으므로 기본은 예전 동작(간격)이다.
    schedule_mode: ScheduleMode = ScheduleMode.INTERVAL
    #: 사용자가 시각을 고를 때 쓰던 시간대. 작업마다도 남기지만(ScheduledJob.timezone),
    #: 배치 단위로도 들고 있어야 '다음 예약'의 기본값을 같은 시간대로 채울 수 있다.
    timezone: str | None = None
    #: 배치의 기본 게시 대상. 작업이 따로 정하지 않았을 때 물려받는 값이다.
    #: 옛 문서에는 없으므로 publish_naver의 기본은 True(예전에는 네이버가 늘 대상이었다).
    publish_naver: bool = True
    publish_threads: bool = False
    #: 이 배치의 글에 활용할 브랜드(2026-08-19). 실제로 글을 만들 때 읽는 값은 작업의
    #: `brand_id`이고, 여기 두는 것은 **화면이 배치를 다시 그릴 때** 무엇으로 걸었는지
    #: 보여 주기 위해서다(플랫폼 기본값과 같은 자리다).
    brand_id: str | None = None
    status: ScheduledBatchStatus = ScheduledBatchStatus.READY
    target_count: int
    #: **생성 작업 간격(초).** 분 단위만 있던 시절의 문서는 아래 검증기가 환산한다.
    #:
    #: 간격 방식(INTERVAL)에서는 두 발행 사이의 최소 간격이고, 절대 시각 방식(ABSOLUTE)
    #: 에서는 두 **원고 생성 작업**을 띄우는 사이의 간격이다. 어느 쪽이든 "여러 개를
    #: 한꺼번에 돌리지 않는다"가 이 값의 뜻이며, 게시 시각과는 별개의 설정이다.
    interval_seconds: int
    total_count: int
    completed_count: int = 0
    failed_count: int = 0
    canceled_count: int = 0
    current_job_id: str | None = None
    #: 다음 **작업을 시작해도 되는** 시각. 간격 방식에서는 직전 발행 성공 시각 + 간격이고,
    #: 절대 시각 방식에서는 직전 원고 생성이 끝난 시각 + 간격이다. 게시 시각이 아니다.
    next_run_at: str | None = None
    pause_requested: bool = False
    stop_requested: bool = False
    #: 같은 클릭이 두 번 도착했을 때 배치를 두 개 만들지 않기 위한 열쇠.
    client_request_id: str | None = None
    created_at: str
    started_at: str | None = None
    paused_at: str | None = None
    completed_at: str | None = None
    updated_at: str
    logs: list[ScheduledLogEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_interval(cls, data: Any) -> Any:
        """분 단위만 저장돼 있던 옛 배치를 초로 읽는다.

        간격은 원래 ``intervalMinutes``였다. 배포 시점에 돌고 있던 배치가 그 형식이라,
        없는 필드로 읽으면 검증에서 터져 진행 중인 예약이 통째로 죽는다.
        """
        if not isinstance(data, dict):
            return data
        if data.get("intervalSeconds") is None and data.get("interval_seconds") is None:
            minutes = data.get("intervalMinutes", data.get("interval_minutes"))
            if isinstance(minutes, int) and not isinstance(minutes, bool):
                data = {**data, "intervalSeconds": minutes * 60}
        return data


class ScheduledBatchView(CamelModel):
    """배치와 그 작업들을 한 번에 내려주는 조회 응답."""

    batch: ScheduledBatch
    jobs: list[ScheduledJob] = Field(default_factory=list)


class ScheduledJobListItem(CamelModel):
    """예약 목록의 한 줄 — 배치를 넘나들며 '내 예약'을 한 번에 보기 위한 것.

    제목·글 상태는 여기서 붙인다. 작업 문서에는 소재만 있고 나머지는 글(BlogTask)에
    있는데, 목록을 그리자고 화면이 글마다 조회를 한 번씩 더 하게 만들 수는 없다.

    **작업의 상태와 글의 상태를 함께 싣는 것이 핵심이다.** 작업의 status는 그 실행이
    끝났을 때의 마지막 기억일 뿐이고, 같은 글이 그 뒤에 다른 경로로 완성되거나 발행될
    수 있다(2026-08-06 사용자 신고 — "발행내역에서는 실패라고 뜨는데 내 글 목록에서는
    글이 완성되어 있다"). 화면이 둘 다 알아야 사실대로 말할 수 있다.
    """

    job: ScheduledJob
    title: str | None = None
    #: 배치의 상태. 목록에서 '이 예약이 아직 살아 있는 배치의 것인가'를 가른다.
    batch_status: ScheduledBatchStatus | None = None
    #: 이 작업이 만든 글의 **지금 상태**. 글이 지워졌으면 None이다.
    post_status: BlogTaskStatus | None = None
    #: 그 글이 **실제로 올라가 있으면** 그 주소. 작업이 실패로 남아 있어도 값이 있을 수
    #: 있다 — 사용자가 직접 발행했거나, 발행 뒤 기록만 실패로 남은 경우다.
    published_url: str | None = None
    #: 오래 걸리는 단계가 지금 어느 칸인지(예: 4/4 사실 검수·문장 다듬기). 화면이
    #: '원고 생성 중'에 7분씩 멈춰 보이던 것을 푼다(2026-08-06 사용자 요청).
    progress: TaskProgress | None = None
    #: 그 글의 **작업 현황 줄**(2026-08-12 사용자 신고: "작업현황 로그도 상세하게 안뜨네").
    #:
    #: 예약의 로그(배치 logs)는 단계 경계에서만 한 줄씩 쌓여, 원고를 만드는 5~8분 동안
    #: 새 줄이 하나도 오지 않는다. 새 글 작성 화면이 보여 주는 그 촘촘한 줄들을 목록이
    #: 함께 실어 그 사이를 채운다 — 글 요약(PostSummary)이 이미 들고 있는데 목록이
    #: 옮겨 담지 않아 화면까지 오지 못했다.
    activity_log: list[ActivityEntry] = []


def has_appointment(job: ScheduledJob) -> bool:
    """사용자가 이 작업의 시각을 **직접 정했는가**(2026-08-13).

    두 가지가 여기서 '아니오'다:

    - 시각 칸이 아예 비어 있다(``publish_at is None``) — 「자동 포스팅」 탭에서 줄의
      시각을 비우면 그렇게 온다. 차례가 곧 시각이다.
    - 새 글 작성에서 '지금 바로'로 걸었다(``starts_immediately``) — 그쪽은 publish_at에
      걸린 시각이 채워지지만, 그것은 약속이 아니라 그냥 지금이다.

    세 군데가 이 구분을 본다. 셋이 같은 답을 봐야 화면과 실제가 어긋나지 않는다.

    - 발행 순서: 약속이 없는 쪽이 **먼저** 올라간다(worker._due_to_publish).
    - 원고 생성: 약속이 없으면 여러 편을 **함께** 만든다(worker._due_to_prepare).
    - 자료 수집: 약속이 없으면 검증 단계에서 모은 자료를 **그대로 쓴다**(service).
    """
    return job.publish_at is not None and not job.starts_immediately


def publishes_anywhere(job: ScheduledJob) -> bool:
    """이 작업이 **어딘가에 올리기는 하는가**(2026-08-12).

    소재 단계에서 발행 플랫폼을 하나도 고르지 않으면 두 스위치가 모두 꺼진 채로 온다.
    그런 작업은 **원고를 만들면 끝이다**(2026-08-13 사용자 지시: "플랫폼 선택안했는데
    왜 발행을 하려고 그래. 원고생성 완료하면 작업 끝이지"). 발행 줄에 세우지 않는다.
    """
    return job.publish_naver or job.publish_threads
