"""블로그 글 작성 태스크 모델."""

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .base import CamelModel
from .brand import BrandClosing
from .draft import DraftGenerationResult, FinalPost, SeoKeywordPlan, TitlePlan
from .intent import IntentValidationResult, SearchSource
from .posting import PostingLog
from .status import BlogTaskStatus
from .trend import TrendSelection


class ReferenceMaterialType(StrEnum):
    IMAGE = "IMAGE"
    PDF = "PDF"
    TEXT = "TEXT"
    URL = "URL"


#: 참고자료의 ``origin``에 붙는 표시 — 서버가 브랜드 자료에서 펼쳐 넣은 것.
#:
#: 모델 옆에 둔다. 검증(blog_task.validation)과 브랜드 서비스가 **같은 글자**를 봐야
#: 하는데, 브랜드 모듈에 두면 검증이 그것을 import할 수 없다(브랜드 쪽이 이미 검증
#: 모듈을 import하고 있어 순환이 된다).
BRAND_MATERIAL_ORIGIN = "brand"


#: 브랜드를 고른 글이 **어떤 글인가**(2026-08-19).
#:
#: 소재 칸과 브랜드 칸이 서로를 잠그던 동안에는 물을 필요가 없었다 — 브랜드가 있다는
#: 것은 곧 그 브랜드가 주인공이라는 뜻이었다. 잠금을 없애고 **소재와 브랜드를 함께**
#: 고를 수 있게 되면서(AIONA 유입용 콘텐츠), 같은 `brandId`가 두 가지 전혀 다른 글을
#: 뜻하게 됐다. 그래서 모드를 값으로 남긴다 — 프롬프트가 이 값 하나로 갈린다.
#:
#: ``FOCUS``   브랜드가 글의 주인공이다. 소재를 비우고 브랜드만 골랐을 때(또는 소재가
#:             브랜드 이름 그 자체일 때). 예: "AIONA란 무엇인가", "앱스튜디오 사용법".
#: ``UTILITY`` 트렌드·소재가 주인공이고, 브랜드는 그 상황에서 **활용한 도구**로만
#:             등장한다. 예: 소재 '빼빼로' + 브랜드 'AIONA'. 검색해서 들어온 독자가
#:             원하는 답을 먼저 주고, 그 과정에서 브랜드를 발견하게 하는 글이다.
BRAND_MODE_FOCUS = "FOCUS"
BRAND_MODE_UTILITY = "UTILITY"
BRAND_MODES: tuple[str, ...] = (BRAND_MODE_FOCUS, BRAND_MODE_UTILITY)


class ReferenceMaterial(CamelModel):
    type: ReferenceMaterialType
    value: str
    name: str | None = None
    # 이 자료를 **누가 넣었는가**(2026-08-11). "brand"는 서버가 브랜드 자료에서 펼쳐
    # 넣은 것이고, None은 사용자가 직접 올린 것이다.
    #
    # 왜 필요한가. 브랜드를 고른 글을 소재 단계에서 다시 저장하면 화면은 저장돼 있던
    # 자료 목록을 그대로 돌려보낸다 — 표시가 없으면 서버가 브랜드 자료를 한 번 더
    # 펼쳐 넣어 같은 자료가 두 벌이 되고, 브랜드를 바꿔도 옛 브랜드 자료가 남는다.
    # 이 표시가 있으면 저장할 때마다 브랜드 자료만 걷어내고 지금 브랜드로 다시 채운다.
    # 화면도 이 값을 보고 '내가 추가한 참고자료' 목록에서 브랜드 자료를 뺀다.
    origin: str | None = None


class StatusHistoryEntry(CamelModel):
    # `from`은 파이썬 예약어라, 명시적 별칭으로 통신 필드명을 그대로 유지한다.
    from_: BlogTaskStatus = Field(alias="from")
    to: BlogTaskStatus
    at: str
    by: str


# 소재가 **어느 분야의 것인가**(2026-08-11).
#
# 왜 필요한가. '오디세이'는 영화이고 게임이고 모니터다. 소재 글자만 받으면 모델이 어느
# 쪽인지 스스로 고르고, 사용자가 원한 분야와 다르면 글 전체가 엉뚱한 곳으로 간다 —
# 제목·자료 수집·이미지가 전부 그 판단 위에 얹히므로 뒤에서 되돌릴 수도 없다. 그래서
# 처음 한 번, 사용자에게 직접 묻는다.
#
# 2026-07-20에 없앤 '주제(네이버 카테고리 32개)'와는 다른 것이다. 그때 없앤 것은 **발행
# 분류**였고(발행 시점에 고르면 되는 값이라 입력에서 뺐다), 이것은 **소재 해석 조건**이다.
# 그래서 목록도 발행 분류가 아니라 동명이의어가 실제로 갈리는 축으로 짰고, 12개다 —
# 32개짜리 분류 화면으로 돌아가지 않는다.
#
# 저장된 옛 글에는 없다(None). 없으면 예전과 똑같이 모델이 알아서 판단한다.
SUBJECT_CATEGORIES: tuple[str, ...] = (
    "인물·연예인",
    "영화·드라마·방송",
    "게임",
    "IT·컴퓨터·AI",
    "브랜드·기업",
    "제품·쇼핑·리뷰",
    "음식·맛집",
    "여행·장소",
    "스포츠",
    "자동차·모빌리티",
    "건강·생활",
    "정책·시사",
)


class BlogTaskInput(CamelModel):
    topic: str
    subject: str | None = None
    # 사용자가 고른 소재 분야(SUBJECT_CATEGORIES 중 하나). 동명이의어를 가르는 값이라
    # 소재와 함께 프롬프트 맨 앞에 실린다(prompts.blog_input_summary).
    subject_category: str | None = None
    # 이 글에 엮은 브랜드(2026-08-11). 프롬프트는 이 값을 읽지 않는다 — 브랜드가 글에
    # 반영되는 통로는 참고자료다. 여기 두는 이유는 **소재 단계로 돌아왔을 때 어느
    # 브랜드를 골랐었는지 화면이 알아야** 하기 때문이고, 다시 저장할 때 그 브랜드의
    # 자료를 새로 펼쳐 넣는 기준이기도 하다. 브랜드를 안 쓴 글에는 없다.
    brand_id: str | None = None
    # 그 브랜드의 이름. **프롬프트가 이 값을 읽는다** — 브랜드 자료가 참고자료 더미에
    # 이름 없이 섞여 들어가면, 모델은 그것이 이 글의 대상인지 곁들일 회사인지 알 수 없다.
    # 소재가 '스파이더맨 4편 감상'인데 브랜드 소개 자료가 근거로 실려 있으면 글의 중심이
    # 브랜드로 끌려간다(2026-08-11 사용자 지적). 서버가 브랜드를 확인한 뒤 채운다.
    brand_name: str | None = None
    #: 그 브랜드가 이 글에서 맡는 역할 — ``FOCUS``(주인공) 또는 ``UTILITY``(활용 도구).
    #:
    #: **서버가 정한다.** 사용자가 소재를 적었는지로 갈린다(routes._with_brand_materials):
    #: 소재를 비우고 브랜드만 골랐으면 FOCUS, 소재를 적고 브랜드도 골랐으면 UTILITY다.
    #: 화면이 보낸 값을 믿지 않는 이유는 brand_name과 같다 — 화면이 잘못 보내면 글의
    #: 성격 자체가 뒤집힌다.
    #:
    #: 브랜드를 안 쓴 글에는 없다(None). 2026-08-19 이전의 옛 글에도 없는데, 그때는
    #: 브랜드가 있으면 언제나 주인공이었으므로 읽는 쪽이 None+브랜드를 FOCUS로 다룬다.
    brand_mode: str | None = None
    #: 이 소재와 브랜드가 얼마나 자연스럽게 닿는가 — ``A``·``B``·``C``.
    #: (``modules/brand/fit.evaluate_brand_fit``이 저장 시점에 잰 값이다.)
    #:
    #: 화면은 이 값으로 경고를 띄우고(C는 억지 연결이다), 프롬프트는 이 값으로 브랜드를
    #: **얼마나 앞에 낼지**를 정한다. B는 쓸 수 있지만 상황을 먼저 만들어야 하므로,
    #: 브랜드가 등장하는 자리가 A보다 뒤다.
    brand_fit_grade: str | None = None
    #: 이 소재에 닿은 기준표 줄들("상황 → 기능"). 프롬프트가 그대로 읽는다.
    #:
    #: 브랜드 자료 전체(참고자료)에도 같은 표가 들어 있지만, 거기서 모델이 고르게 두면
    #: 매번 같은 기능만 나오거나 소재와 무관한 기능이 붙는다. **닿은 것만** 여기 둔다.
    brand_use_cases: list[str] = Field(default_factory=list)
    #: 글 맨 마지막에 붙일 마무리(사실 한 줄 + 링크). 저장 시점의 브랜드 자료에서
    #: **그대로 베껴 둔다**(2026-08-19).
    #:
    #: 브랜드를 다시 조회하지 않으려는 것만이 아니다. 이 글자는 검수를 거치지 않고
    #: 그대로 발행되므로, **이 글을 만들 때 정해져 있던 문구**가 그 글의 것이어야 한다.
    #: 나중에 브랜드 자료를 고쳐도 이미 나간 글의 마무리는 바뀌지 않는다.
    brand_closing: BrandClosing | None = None
    #: 이 글에 고정으로 붙일 브랜드 해시태그. 저장 시점의 브랜드 자료에서 베껴 둔다 —
    #: 나중에 자료를 고쳐도 이미 나간 글의 해시태그는 바뀌지 않는다(마무리와 같은 이유).
    brand_hashtags: list[str] = Field(default_factory=list)
    purpose: list[str] | None = None
    keywords: list[str]
    tone: str | None = None
    target_reader: str | None = None
    reader_age_range: str | None = None
    reader_knowledge_level: str | None = None
    reference_materials: list[ReferenceMaterial] = Field(default_factory=list)

    # --- 원고 작업 예약(2026-08-11 사용자 지시) ---
    #
    # 사용자가 소재 단계에서 **원고를 언제 만들지** 고른 절대 시각(UTC ISO). 비워 두면
    # 예전 그대로다 — 방향을 고르는 즉시 원고를 만든다. 값이 있으면 방향까지 고른 뒤
    # 예약 작업으로 넘어가, 그 시각에 **자료를 새로 모아** 원고를 만든다.
    #
    # 왜 이 값이 필요한가. 며칠 뒤 발행할 글을 오늘 만들면 그 사이에 나온 이슈가 빠진다.
    # 방향(제목·독자·논지)은 사람이 고른 판단이라 며칠 뒤에도 유효하지만, 자료는 낡는다.
    # 그래서 방향만 오늘 확정하고 자료·원고는 이 시각에 만든다.
    #
    # 저장은 언제나 UTC 한 가지다. 사용자가 어느 시간대에서 골랐는지는 아래에 따로 적고,
    # 표시는 클라이언트가 자기 로컬 시간으로 한다 — 서버가 변환하지 않으므로 날짜가
    # 하루 밀리는 종류의 버그가 생기지 않는다(예약 포스팅의 publish_at과 같은 규칙이다).
    scheduled_run_at: str | None = None
    #: 시각을 고를 때 쓰던 시간대(IANA, 예: "Asia/Seoul"). 표시·감사용이다.
    scheduled_timezone: str | None = None
    #: 이 소재로 만들 원고 수(1~3, 2026-08-12). **기본값 1이라 옛 문서도 그대로 읽힌다.**
    #:
    #: 2 이상이면 작업이 줄지어 돈다 — 앞 편이 끝나야 다음 편이 시작한다. 편마다
    #: 시각을 따로 받지 않는 이유고, ``scheduled_run_at``은 **첫 편의 시각**이다.
    draft_count: int = 1
    #: 원고를 다 만들면 **어디에 올릴지**(2026-08-12). 처음에는 켜기/끄기 하나였는데,
    #: 플랫폼마다 따로 고르게 바꿨다(사용자 요청) — 같은 소재라도 네이버에만 올리고
    #: 쓰레드에는 안 올리는 경우가 있다.
    #:
    #: 네이버 기본 True — 예약 작업은 여태 원고를 만들면 네이버에 올렸고, 옛 문서에는
    #: 이 칸이 없다. 쓰레드 기본 False — 예전에 없던 동작을 옛 글에 소급하지 않는다.
    #: 둘 다 False면 원고까지만 만들고 멈춘다(작업 큐에서 사람이 확인한 뒤 올린다).
    auto_publish_naver: bool = True
    auto_publish_threads: bool = False


class SelectedIntent(CamelModel):
    intent_id: str
    title: str
    target_reader: str
    rationale: str
    # M3 후보가 뽑은 검색 키워드. 예전에는 후보에만 있고 선택 시점에 버려져서, 사용자가 고른
    # 의도의 키워드가 원고 단계까지 가지 못했다. 옛 문서에는 없으므로 기본값은 빈 목록이다.
    keywords: list[str] = Field(default_factory=list)
    sources: list[SearchSource] | None = None


class TaskPhase(StrEnum):
    SEARCH = "SEARCH"
    DRAFT = "DRAFT"


# 클라이언트가 보여주는 단계 라벨. 클라이언트가 아니라 여기 두는 이유는, 실제로 어떤
# 단계가 돌고 있는지 아는 쪽이 서버이기 때문이다 — 예전에는 클라이언트가 고정 목록을
# 타이머로 애니메이션했는데, 그건 사용자에게 아무 정보도 주지 못했다.
PHASE_STEPS: dict[TaskPhase, list[str]] = {
    TaskPhase.SEARCH: ["자료 검색", "검증 후보 정리"],
    # 라벨은 그 구간에서 실제로 도는 작업의 이름이어야 한다. 첫 구간의 옛 라벨
    # '입력값 정리'는 실제로는 콘텐츠 설계 LLM 호출이 도는 구간이라, 사용자와 개발자
    # 모두 "입력 정리가 왜 1분씩 걸리나"를 물었다 — 측정 라벨과 실제 작업이 다르면
    # 병목을 엉뚱한 곳에서 찾게 된다. (apps/web/src/constants.ts와 반드시 일치)
    # 마지막 구간의 옛 라벨은 '결과 정리'였고 실제로 DB 쓰기 한 번이었다. 2026-08-05부터
    # 그 자리에서 최종 검수가 돈다 — 완성된 원고와 이미지를 사용자 입력·조사 자료와 대조해
    # 어긋난 문장을 고치고, 맞지 않는 이미지를 뺀다(최대 3회). 같은 날 그 뒤에 문장 다듬기가
    # 붙었다(어색한 문장·AI 말투를 그 자리에서 교체). 저장은 그 뒤다.
    TaskPhase.DRAFT: [
        "원고 구조 설계",
        "본문 원고 작성",
        "카드 이미지 생성",
        "사실 검수·문장 다듬기",
    ],
}


class TaskProgress(CamelModel):
    """오래 걸리는 단계가 어디까지 진행됐는지.

    백그라운드 작업이 기록하고, 글을 폴링하는 쪽이 읽는다. 참고용일 뿐이다.
    업데이트를 놓쳐도 라벨이 낡을 뿐, 정확성에는 영향이 없다.
    """

    phase: TaskPhase
    step: int
    total_steps: int
    label: str
    steps: list[str]
    started_at: str
    updated_at: str
    # --- 단계 안의 진행(2026-08-11) ---
    #
    # 단계 하나가 여러 개를 처리할 때 **몇 개를 끝냈는지**. 이미지 생성이 대표적이다 —
    # 5~8분짜리 한 칸이라 시간으로 짐작하는 수밖에 없었는데, 만들 장수는 시작할 때
    # 이미 정해져 있고 한 장 끝날 때마다 사실을 안다. 추정으로 둘 이유가 없다.
    #
    # 옛 문서·이 값을 보고하지 않는 단계에는 없다(None) — 그때 화면은 예전처럼
    # 머문 시간으로 채운다.
    units_done: int | None = None
    units_total: int | None = None
    # 이 환경에서 **실제로 잰** 단계별 소요(초). 화면의 막대는 이 값으로 단계 몫을
    # 나눈다 — 없으면(첫 실행·재시작 직후) 클라이언트의 기본 상수를 쓴다.
    #
    # 실행 중에는 바뀌지 않는다(reporter가 시작할 때 한 번 읽는다). 도는 중에 바뀌면
    # 같은 시점의 퍼센트가 달라져 막대가 뒤로 갈 수 있다.
    step_seconds: list[float] | None = None


class ActivityEntry(CamelModel):
    """생성 중 '작업 현황' 로그 한 줄(2026-08-10 사용자 요청 — 기다리는 동안 지금 무슨
    일을 하는지 보이게). 단계 라벨·내레이션(detail) 같은 사용자 문구만 담는다 — 서버
    로그의 URL·식별자·예외 원문은 싣지 않는다. 프로세스 메모리에만 살고 DB에는 저장하지
    않는다(참고 표시일 뿐, 놓쳐도 정확성에 영향이 없다)."""

    at: str
    message: str


class PostSummary(CamelModel):
    """다른 화면이 글 하나에 대해 알아야 하는 것만 모은 가벼운 읽기 모델.

    예약 목록이 쓴다. 그 화면은 작업(ScheduledJob)의 상태만 알고 있었는데, 그것은
    **작업이 끝났을 때의 마지막 기억**일 뿐 글이 지금 어떤 상태인지가 아니다. 둘은
    실제로 갈라진다(2026-08-06 사용자 신고 — "발행내역에서는 실패라고 뜨는데 내 글
    목록에서는 글이 완성되어 있다"):

    - 작업이 원고 단계에서 실패해도, 같은 글을 쥐고 있던 **다른 실행이 원고를 끝내** 놓는
      경우가 있다(``M4 requires INTENT_SELECTED, received GENERATING``으로 실패한 작업들).
    - 발행이 실패로 기록돼도 사용자가 '내 글 목록'에서 **직접 발행**했을 수 있다.

    그래서 목록은 작업의 상태와 **글의 실제 상태**를 함께 받아 둘 다 보여 준다.
    원고 HTML·이미지는 담지 않는다(그것 때문에 목록이 무거워졌던 이력이 있다).
    """

    post_id: str
    status: BlogTaskStatus
    #: 완성된 원고의 제목. 아직 원고가 없으면 None이고 화면이 소재를 대신 쓴다.
    title: str | None = None
    #: **실제로 어딘가에 올라갔으면** 그 주소. 자동·수동 발행을 가리지 않는다 —
    #: "올라갔는가"를 묻는 값이지 "누가 올렸는가"를 묻는 값이 아니다.
    published_url: str | None = None
    #: 지금 어느 칸을 돌고 있는지(예: 4/4 사실 검수·문장 다듬기). 오래 걸리는 단계가
    #: 도는 동안에만 있다. 예약 화면이 '원고 생성 중'에 멈춰 보이던 것을 푼다.
    progress: TaskProgress | None = None
    #: 이 글의 '작업 현황' 줄들(단계 시작·내레이션). 새 글 작성 화면이 보여 주는 것과
    #: **같은 목록**이다(2026-08-10 사용자 요청 — "새 글 작성에서 보여주는 작업현황
    #: 로그처럼"). 예약 화면의 로그는 단계 경계에서만 한 줄씩 쌓여, 원고를 만드는 5~8분
    #: 동안 화면이 멈춘 것처럼 보였다.
    #:
    #: DB가 아니라 프로세스 메모리에서 온다(ActivityEntry 참고) — 생성이 도는 그
    #: 프로세스가 응답도 만들므로 폴링 화면에는 항상 최신 줄이 실린다. 서버가 다시
    #: 시작하면 비는데, 그때는 지금까지처럼 예약 자신의 로그만 보인다.
    activity_log: list[ActivityEntry] = Field(default_factory=list)


class BlogTaskListItem(CamelModel):
    """내 글 목록 카드에 필요한 가벼운 읽기 모델.

    ``BlogTask`` 전체에는 원고 HTML·마크다운과 base64 이미지가 들어 있어 목록에서 그대로
    읽으면 글 수에 비례해 응답이 수십~수백 MB까지 커진다. 상세 화면은 기존
    ``GET /posts/{postId}``를 사용하고, 목록은 이 모델만 받는다.
    """

    post_id: str
    user_id: str
    status: BlogTaskStatus
    version: int
    created_at: str
    updated_at: str
    title: str
    topic: str
    subject: str | None = None
    purposes: list[str] = Field(default_factory=list)
    post_url: str | None = None
    has_final_post: bool


class BlogTask(CamelModel):
    post_id: str
    user_id: str
    status: BlogTaskStatus
    version: int
    created_at: str
    updated_at: str
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    input: BlogTaskInput
    posting_logs: list[PostingLog] = Field(default_factory=list)
    trend_selection: TrendSelection | None = None
    intent_validation_result: IntentValidationResult | None = None
    selected_intent: SelectedIntent | None = None
    # 원고보다 먼저 확정한 제목 계획. 의도 선택 직후(설계 선행 생성과 같은 시점)에 만들어
    # 저장한다 — 저장하지 않으면 선행 생성과 실제 생성이 서로 다른 제목을 만들어 콘텐츠
    # 설계 캐시가 매번 어긋난다. 만들지 못한 글은 None이고 예전 동작으로 진행한다.
    title_plan: TitlePlan | None = None
    # 원고보다 먼저 확정한 SEO 키워드 계획(제목 계획과 같은 시점에 저장한다). 옛 문서에는
    # 없으므로 기본값 None이며, 없어도 조회·원고 생성이 예전과 똑같이 동작한다.
    seo_keyword_plan: SeoKeywordPlan | None = None
    draft_generation_result: DraftGenerationResult | None = None
    final_post: FinalPost | None = None
    progress: TaskProgress | None = None

    @model_validator(mode="before")
    @classmethod
    def _repair_draft_generation_result(cls, data: Any) -> Any:
        """생성 기록에 원고가 빠진 문서도 **읽을 수 있게** 한다.

        `draftGenerationResult.finalPost`는 필수 필드다. 그런데 저장된 문서 중에 그 자리가
        빠진 것이 있었고, 그 글을 열면 조회가 통째로 500이 났다 — 발행까지 끝난 멀쩡한
        글인데 화면에서 열리지 않았다(2026-08-06 실사용). 사용자에게는 글이 사라진 것으로
        보인다.

        원고 자체는 잃어버리지 않았다. 같은 문서의 **맨 위 `finalPost`**에 같은 원고가 한
        벌 더 저장돼 있기 때문이다(save_draft_generation_result가 두 자리에 함께 쓴다).
        그래서 빠진 자리를 그것으로 채운다.

        둘 다 없으면 생성 기록을 없는 것으로 본다. 그 글은 애초에 보여 줄 원고가 없으니
        `draftGenerationResult`도 아무것도 설명하지 못하고, 여기서 버리면 나머지(소재·
        제목·상태·발행 기록)는 그대로 읽힌다. **한 칸이 빠졌다고 글 전체를 못 열게 하지
        않는다**는 것이 이 검증기의 목적이다.
        """
        if not isinstance(data, dict):
            return data
        result = data.get("draftGenerationResult")
        if not isinstance(result, dict) or result.get("finalPost") is not None:
            return data

        repaired = dict(data)
        fallback = data.get("finalPost")
        if isinstance(fallback, dict):
            repaired["draftGenerationResult"] = {**result, "finalPost": fallback}
        else:
            repaired["draftGenerationResult"] = None
        return repaired
