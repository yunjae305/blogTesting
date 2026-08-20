"""M4 원고 생성과 사용자 수정 저장. 이미지 생성(M5)도 여기서 함께 돈다."""

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from html import escape
from app.shared.format import now_iso as _now
from typing import Any

from app.shared import perf

from app.errors import BlogTaskError
from app.llm import (
    DraftGenerator,
    PhotoSearch,
    PostImageGenerationInput,
    PostImageGenerator,
)
from app.llm.imaging import LEGACY_BODY_IMAGE_LIMIT, thumbnail_lines
from app.llm.parsing import (
    ProviderTruncatedError,
    align_seo_plan_with_title,
    apply_visual_theme,
)
from app.config import final_review_max_rounds
from app.llm.prompts import (
    article_length_pass_max,
    article_length_targets,
    length_total_image_cap,
    visual_style_for,
)
from app.modules.blog_task.jobs import BackgroundJobs, ProgressReporter
from app.modules.blog_task.locks import JobLease, NoOpJobLease, hold, lease_key
from app.modules.blog_task.repository import BlogTaskRepository
from app.modules.persona.service import PersonaService
from app.modules.user_settings.service import UserSettingsService
from app.shared.ids import short
from app.shared.image_bytes import normalize_data_url
from app.shared.image_privacy import mask_data_url
from app.shared.reference_url import is_public_reference_url
from app.shared import (
    DRAFT_CHECKPOINT_STAGE_DRAFT_READY,
    NAMED_SUBJECT_KINDS,
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
    BlogTask,
    BlogTaskStatus,
    DraftCheckpoint,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationResult,
    DraftGenerationSettings,
    FinalPost,
    FinalReviewIssue,
    FinalReviewReport,
    FinalReviewResult,
    FinalReviewTarget,
    GeneratedPostImage,
    IntentAnchor,
    PolishResult,
    PrivateRegion,
    ReferenceMaterialType,
    SelectedIntentForDraft,
    TaskPhase,
    VisualBudget,
    WebPhoto,
)

logger = logging.getLogger(__name__)

from .card_selection import (
    MAX_TOTAL_IMAGES,
    NamedSubject,
    SelectedCards,
    named_subject_of,
    plan_named_subject,
    section_number,
    select_cards,
)
from .brand_art import insert_brand_art
from .closing import append_closing
from .editor import parse_edited_html
# 회차 상한은 더 이상 이 모듈의 상수가 아니라 설정값이다(config.final_review_max_rounds).
from .final_review import (
    apply_review,
    as_review_report as _as_review_report,
    merge_review_reports as _merge_review_reports,
)
from .polish import apply_polish
from .editorial_style import (
    colour_direction_for,
    normalize_style_plan,
    thumbnail_layout_plan_for,
)
from .reference_evidence import build_profile, enrich, reference_image_id
from .visual_policy import (
    gate_visuals,
    has_numeric_user_material,
    purpose_policy,
)
from .quality import body_char_count, check_draft, check_title_plan
from .critique import markdown_with_placeholders, rebuild_post
from .content_validation import (
    CONTENT_VALIDATION_VERSION,
    run_content_validations,
    validate_official_thumbnail_used,
    validate_real_entity_image_used,
    write_validation_log,
)
from .images import (
    MAX_POST_IMAGES,
    MAX_REFERENCE_IMAGES,
    dedupe_images,
    extract_image_tags,
    image_html,
    image_markdown,
    insert_after_heading_html,
    insert_after_heading_markdown,
    insert_html_images,
    insert_markdown_images,
    is_image_data_url,
    mime_type_from_data_url,
    replace_image_tags,
    strip_image_tags,
)
from .visuals import (
    render_planned_visual,
    renderable_visuals,
    replace_visual_markers,
    visual_html,
    visual_markdown,
)
from .validation import (
    validate_generate_draft_request,
    validate_update_draft_request,
)

DRAFT_GENERATION_ACTOR = "system:m4-draft-generation"
DRAFT_EDIT_ACTOR = "user:m4-draft-edit"

# 완성된 원고를 저장하다 DB가 흔들리면 몇 번 더 해 본다. 다시 만드는 데 5분과 실제
# 비용이 드는 것을 한 번의 네트워크 실패로 버리지 않는다(_store_generation_result 참고).
SAVE_ATTEMPTS = 3
SAVE_RETRY_SECONDS = 2.0


def _generation_payload_bytes(result: DraftGenerationResult) -> int:
    """저장하려는 원고가 몇 바이트인지. **실패 로그에 실으려고만 잰다.**

    저장이 시간 초과로 죽었을 때, 그것이 '문서가 커서'인지 '연결이 흔들려서'인지 가릴
    방법이 지금까지 없었다. 크기를 함께 남기면 다음 실패에서 그 자리에서 갈린다.

    재는 데 실패해도 0을 돌려준다 — 크기를 못 쟀다고 원고 저장이 막히면 본말이 뒤집힌다.
    """
    try:
        import bson

        return len(bson.encode({"d": result.to_wire()}))
    except Exception:  # noqa: BLE001
        return 0

# 품질 검사에 걸리면 다시 생성한다. 같은 프롬프트에도 모델은 다른 답을 낸다.
DRAFT_ATTEMPTS = 2
# 제목 계획이 규격에 걸리면 **제목만** 다시 만든다. 본문은 아직 쓰기 전이라 버릴 것이 없다.
TITLE_PLAN_ATTEMPTS = 2
DRAFT_RETRY_ACTOR = "user:m4-draft-retry"
# v2.0: 참고자료 근거 프로필 + 편집 스타일 계획 + 목적별 시각자료 하드 게이트 +
# 아키타입별 글 구조. 프롬프트·스키마·검증이 함께 바뀌므로 메이저를 올린다.
# v2.1: 소재 정체(contentEntity)를 근거 프로필에 얹어 제목·SEO·설계·원고까지 관통시켰다.
# 원본 검색 키워드와 '글에 쓸 표현'이 분리되고, 실제 영상 콘텐츠는 핵심 포맷이 도입부에
# 오며 시청 경험을 지어내지 않는다. 프롬프트·스키마·검증이 함께 바뀌므로 마이너를 올린다.
# v2.2: 같은 판정에서 **카테고리**까지 정하고, 카테고리별 작성 지침(독자 궁금증·필수 조사
# 항목·권장 구조·금지·자체 점검)이 제목·설계·원고 프롬프트에 실린다. 실존 대상 규칙도
# 영상 콘텐츠에서 상품·인물·장소·고위험 주제로 넓어졌다.
# v2.3: 참고 이미지 개인정보 검사 완료 여부와 REUSED의 정확한 referenceId 계약을 반영한다.
# 버전을 올려 privacy_scanned가 없던 옛 저장점이 새 이미지 단계로 재개되지 않게 한다.
# v2.4: 외부 URL 본문·검색 스니펫을 신뢰할 수 없는 데이터로 다루는 시스템 규칙을 모든
# 원고 단계에 적용한다. 옛 저장점이 새 prompt-injection 방어를 우회해 재개되지 않게 한다.
M4_PROMPT_VERSION = "m4-draft@v2.4"
# v3.2: 핵심 시각 대상(subjectKind·mustShowSubject)을 계획→파싱→이미지 프롬프트까지
# 관통시켰다. 고유 캐릭터·실제 인물이 소재면 그 대상 본인이 화면에 보여야 하고, 캐릭터
# 식별에 필요한 비문자형 복장 문양은 로고 금지에서 예외가 된다.
# v3.3: 실존 인물 경로 강화 — 키워드에만 있는 인물명이 계획까지 도달하고,
# PRIMARY IDENTITY REQUIREMENT 블록·인물 참고 이미지 전달·같은 인물 단순 구도 재시도·
# 얼굴을 피하는 썸네일 문구 배치가 더해졌다.
# v3.4: 실존 인물·캐릭터는 그리지 않고 웹에서 실제 사진을 찾아 쓴다. 이름을 관통시키는
# v3.2·v3.3으로도 남은 문제(모델이 그 얼굴을 만들지 못한다)는 프롬프트로 풀 수 없다.
# 사진에는 출처 캡션이 붙고, 세로로 긴 보도 사진은 얼굴을 남기는 크롭을 쓴다.
# v3.5: 소재가 실제 영상 콘텐츠면 카드 계획의 imageSource 판단에 기대지 않고 코드가
# 유튜브 썸네일을 먼저 찾게 하고, 질의도 소재 문자열이 아니라 확인된 정식 명칭으로 만든다.
# 공식 썸네일에는 이미 인물·로고·영상 제목이 구워져 있어 제목 박스를 덧씌우지 않고,
# 규격(1:1 720×720)을 맞출 때도 자르지 않고 비율을 보존한다.
# v3.6: 실물 사진 우선이 영상 콘텐츠 밖으로 넓어졌다. 상품·인물·장소·책·게임도 확인된
# 정식 명칭(+브랜드)으로 먼저 검색하고, 사진 계획 프롬프트에 카테고리별 이미지 우선순위와
# 실물 대체 금지가 실린다. 실존 인물 글의 대표 사진에는 얼굴을 피하는 문구 배치를 쓴다.
# v3.7: REUSED는 검증된 정확한 사용자 사진을 로컬 배치만 하고 생성 폴백하지 않으며,
# 대표 720×720·본문 900×506 규격과 정규화된 참고 이미지 provenance를 기록한다.
M5_PROMPT_VERSION = "m5-image@v3.7"

# provider 어댑터가 네트워크 재시도를 맡는다. 여기서 같은 고비용 이미지 요청을 다시
# 감싸면 한 장이 최대 3배 호출될 수 있어, 장면 단위 호출은 한 번만 한다.
CARD_GENERATION_ATTEMPTS = 1


def _content_entity_key(evidence) -> str:
    """설계 캐시 키에 넣는 소재 정체 한 줄. 판정이 없으면 빈 문자열(예전과 같은 키).

    카테고리도 키에 들어간다. 같은 소재라도 카테고리가 다르면 설계 프롬프트에 실리는
    구조 지침이 통째로 달라지므로, 카테고리가 바뀐 뒤 옛 설계를 재사용하면 지침과 결과가
    어긋난다.
    """
    entity = getattr(evidence, "content_entity", None) if evidence is not None else None
    if entity is None:
        return ""
    return (
        f"{entity.entity_type}:{entity.canonical_name}"
        f":{entity.primary_category}/{entity.secondary_category}"
    )


async def _progress_detail(
    reporter: Any,
    message: str,
    *,
    units_done: int | None = None,
    units_total: int | None = None,
) -> None:
    """진행 화면의 현재 단계 **안에서** '지금 무엇을 하는 중'인지만 바꾼다.

    단계 수(4개)와 막대는 그대로 두고 라벨만 갈아 끼운다 — 검수는 한 단계 안에서 여러 일을
    하는데(검수 → 확인 → 수정), 그걸 단계로 쪼개면 진행률 막대가 통째로 다시 그려지고
    저장된 옛 글의 진행 표시와도 어긋난다.

    진행 표시는 참고용이라 실패해도 원고 생성을 막지 않는다.
    """
    if reporter is None:
        return
    detail = getattr(reporter, "detail", None)
    if detail is None:
        return
    try:
        # 단위를 모르는 구형 reporter(테스트 스텁 포함)도 그대로 돈다 —
        # 진행 표시 하나 때문에 원고 생성이 죽어서는 안 된다.
        if units_total is None:
            await detail(message)
        else:
            try:
                await detail(message, units_done=units_done, units_total=units_total)
            except TypeError:
                await detail(message)
    except Exception as error:  # pragma: no cover - 표시 실패가 생성을 막지 않는다
        logger.debug("진행 상세 표시 실패 - %s", error)


def _draft_input_fingerprint(draft_input: DraftGenerationInput) -> str:
    """저장점 재개 가능 여부를 가르는 입력 지문.

    사용자가 바꿀 수 있는 입력(소재·의도·스타일·형식·설정·트렌드 제목)과 프롬프트 버전만
    담는다. 제목·SEO·설계·근거처럼 파이프라인이 뒤에서 채우는 파생 필드는 뺀다 — 첫
    실행이 만들어 DB에 저장한 제목 계획이 재실행 때는 입력에 실려 오므로, 파생 필드를
    지문에 넣으면 지문이 매번 달라져 멀쩡한 저장점을 버리게 된다.
    """
    payload = draft_input.model_dump(
        mode="json",
        exclude={
            "title_plan",
            "seo_keyword_plan",
            "content_plan",
            "reference_evidence",
            "editorial_style",
            "revision_notes",
            "previous_draft",
        },
    )
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _is_image_safety_block(error: Exception) -> bool:
    """이미지 provider가 프롬프트를 안전 시스템에서 거절했는가.

    이 부류(예: OpenAI moderation_blocked — 실존 인물 묘사 요청)는 같은 프롬프트로
    재시도해도 반드시 같은 이유로 죽는다. 일시적 혼잡·타임아웃과 구분해야 폴백이
    프롬프트를 바꿀지(고유 대상 제거) 그냥 실패로 둘지 정할 수 있다.
    """
    text = str(error).lower()
    return "moderation" in text or "safety system" in text


# 안전 차단된 고유 대상의 프로세스 내 기억(identity → 기록 시각). 차단은 구도가 아니라
# 이름 때문이라, 같은 이름은 카드가 달라도·같은 글을 다시 생성해도 반드시 다시 차단된다 —
# 실측에서 스파이더맨 글 하나가 차단 호출 4번(각 40~150초)으로 이미지 단계에만 8분을 썼다
# (2026-08-10). 한 번 차단된 이름은 TTL 동안 생성 호출 없이 바로 이름 없는 경로로 보낸다.
# provider 정책은 바뀔 수 있으므로 영구 기억은 하지 않는다.
_SAFETY_BLOCK_TTL_SECONDS = 24 * 60 * 60
_safety_blocked_identities: dict[str, float] = {}


def _remember_blocked_identity(identity: str | None) -> None:
    if identity:
        _safety_blocked_identities[identity] = time.monotonic()


def _identity_safety_blocked(identity: str | None) -> bool:
    if not identity:
        return False
    recorded = _safety_blocked_identities.get(identity)
    if recorded is None:
        return False
    if time.monotonic() - recorded > _SAFETY_BLOCK_TTL_SECONDS:
        del _safety_blocked_identities[identity]
        return False
    return True


def _anchor_names(entity) -> list[str]:
    """유튜브 후보 판정(score_candidate)에 넘길 이름들: 확인된 인물 + 짧은 이름
    (원본 검색어·브랜드). 정식 명칭이 길어 제목에 통째로 담기지 않는 콘텐츠(영화
    시리즈·게임)에서 제목 앵커 구실을 한다(2026-08-10)."""
    names = list(entity.person_names)
    for extra in (entity.raw_keyword, entity.brand):
        cleaned = (extra or "").strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return names


def _anchor_unless_blocked(evidence) -> str | None:
    """근거의 고유 대상(anchor). 안전 차단 이력이 있으면 쓰지 않는다 — '이름 없이 다시
    시도'하는 폴백에서 anchor가 같은 이름을 도로 실어 보내면 그 시도도 반드시 죽는다."""
    anchor = (evidence.anchor or None) if evidence else None
    return None if _identity_safety_blocked(anchor) else anchor

# 콘텐츠 설계 캐시 보관 시간·상한. 같은 입력(소재·의도·출처·설정·프롬프트 버전)이면 같은
# 설계를 재사용한다 — 의도 선택 직후의 선행 생성(prefetch)이 여기 들어오고, 사용자가
# '원고 생성'을 누르는 시점에는 설계가 이미 있어 그 시간만큼 체감이 줄어든다. '다시
# 생성하기'도 같은 키라 재사용한다.
#
# 캐시는 같은 입력의 결과를 **안정적으로 재사용하기 위한 장치**다. 예전 주석은 temperature 0.3
# 이라서 같은 입력이면 같은 설계가 나온다고 적었지만, 그것은 사실이 아니었다 — 낮은 temperature는
# 변동을 줄일 뿐 동일 출력을 보장하지 않고, Opus 5는 temperature를 아예 받지 않는다. 정확히
# 말하면: 캐시가 적중하면 저장된 같은 결과를 돌려주고, 캐시가 없거나 만료돼 모델을 다시 부르면
# 세부 표현이 조금 달라질 수 있다. 키에 모델·effort·thinking·프롬프트 버전이 들어가므로, 그중
# 하나가 바뀌면 옛 결과를 재사용하지 않는다.
CONTENT_PLAN_CACHE_TTL_SECONDS = 30 * 60.0
CONTENT_PLAN_CACHE_MAX_ENTRIES = 50

# 서로 다른 카드 그룹의 사진 검색은 네트워크 대기라 겹쳐도 되지만, 한 원고의 그룹을 전부
# 한꺼번에 열면 네이버·유튜브 검색 한도와 공유 HTTP 연결 풀을 순간적으로 몰아 쓴다.
# 네 그룹이면 일반적인 원고(썸네일 + 본문 3~5장)는 대부분 한 물결로 끝내면서도 상한이 있다.
PHOTO_SEARCH_GROUP_CONCURRENCY = 4

_CHART_TYPES = {"BAR_CHART", "LINE_CHART", "PIE_CHART"}

_EXPERIENCE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:제가|나는|저는|내가)\s*.{0,18}(?:사용|이용|구매|방문|체험|써\s*봤)",
        r"직접\s*.{0,12}(?:사용|이용|구매|방문|체험|써\s*봤)",
        r"(?:사용|이용|구매|방문|체험)해\s*보니",
        r"(?:써|가|먹어|입어)\s*본\s*(?:결과|후기|느낌)",
    )
)


def _has_explicit_experience_material(draft_input: DraftGenerationInput) -> bool:
    """첨부 여부가 아니라 사용자가 실제 경험을 명시했는지를 확인한다.

    예전에는 URL 아닌 자료가 하나라도 있으면 체험 서술을 허용했다 — 이미지 한 장에
    지어낸 1인칭 후기가 통과했다. 이제 TEXT 자료에 실제 경험 서술이 있을 때만 허용한다."""
    texts = (
        material.value
        for material in draft_input.input.reference_materials
        if material.type == ReferenceMaterialType.TEXT
    )
    return any(pattern.search(text) for text in texts for pattern in _EXPERIENCE_PATTERNS)


def _generation_revision(task: BlogTask) -> int:
    """이 글이 이미 몇 번 완성됐었나(0부터). 편집 스타일의 variation_seed에 들어간다.

    '다시 생성하기'가 같은 디자인을 내지 않으려면 회차를 알아야 한다. 그런데 이 값은
    **선행 생성과 실제 생성에서 같아야 한다** — 다르면 콘텐츠 설계 캐시 키가 매번 어긋나
    선행 생성이 통째로 헛돈다.

    그래서 세는 것은 GENERATING 전이가 아니라 READY_TO_PUBLISH 전이다. 생성 시작(GENERATING)은
    선행 생성 시점에는 아직 일어나지 않았고 생성 시점에는 이미 일어나 값이 갈리지만, '완성'은
    두 시점 사이에 바뀌지 않는다. 실패 후 재시도는 회차를 올리지 않는다 — 그건 재생성이
    아니라 같은 글의 두 번째 시도이므로 같은 디자인이 맞다.
    """
    return sum(
        1
        for entry in task.status_history
        if entry.to == BlogTaskStatus.READY_TO_PUBLISH
    )


def _visual_with_resolved_source(visual, draft_input: DraftGenerationInput):
    """`source-2` 같은 참조 라벨을 실제 출처명으로 바꾼다.

    라벨은 모델이 수집 목록을 가리키라고 우리가 준 형식이다 — 독자에게 보여 줄 문자열이
    아니다. 가리키는 출처를 못 찾으면 라벨을 지운다: 출처가 비면 렌더러가 하단 표기를
    통째로 생략하므로, 뜻 모를 'source-2'가 이미지에 남는 것보다 낫다.
    """
    match = re.fullmatch(r"source-(\d+)", visual.source or "", flags=re.IGNORECASE)
    if match is None:
        return visual
    intent = draft_input.selected_intent
    sources = (intent.sources if intent else None) or []
    index = int(match.group(1)) - 1
    if 0 <= index < len(sources):
        return visual.model_copy(update={"source": sources[index].title})
    return visual.model_copy(update={"source": None})


def _verified_visual(visual, draft_input: DraftGenerationInput):
    """그래프가 선택된 검색 출처(source-N)의 실측값을 그대로 썼는지 확인한다.

    통과하면 source-N 참조를 실제 출처명·URL 캡션으로 해석해 돌려주고, 참조가 틀리거나
    수치가 하나라도 다르면 None — 근거를 확인할 수 없는 그래프는 그리지 않는다."""
    if visual.type not in _CHART_TYPES:
        # 표·과정도·인포그래픽은 실측 수치 대조 대상이 아니다. 다만 출처 라벨은 똑같이
        # 풀어 줘야 한다 — 안 그러면 렌더된 이미지 하단에 '출처: source-2'가 글자
        # 그대로 찍힌다(2026-08-03 실측: 박태준 세계관 글의 표).
        return _visual_with_resolved_source(visual, draft_input)
    match = re.fullmatch(r"source-(\d+)", visual.source or "", flags=re.IGNORECASE)
    sources = draft_input.selected_intent.sources or []
    if match is None:
        return None
    index = int(match.group(1)) - 1
    if index < 0 or index >= len(sources):
        return None
    source = sources[index]
    expected = source.data_points or []
    actual = visual.data or []
    if len(expected) < 2 or len(actual) != len(expected):
        return None
    if any(
        actual_point.label != expected_point.label
        or actual_point.value != expected_point.value
        for actual_point, expected_point in zip(actual, expected, strict=True)
    ):
        return None
    units = {point.unit for point in expected if point.unit}
    unit = units.pop() if len(units) == 1 else visual.unit
    return visual.model_copy(
        update={
            "source": source.title,
            "caption": f"출처: {source.title} ({source.url})",
            "published_at": None,
            "unit": unit,
        }
    )


def _images_from_post(post: FinalPost) -> list[GeneratedPostImage]:
    if post.images:
        return post.images
    return [post.featured_image] if post.featured_image else []


def _reference_image_urls(task: BlogTask) -> dict[str, str]:
    """reference-image-N → data URL. 순서는 첨부 순서이며, 근거 프로필의 id와 같다.

    예전에는 첫 이미지 하나만 모든 생성의 기준으로 썼다 — 제품 사진과 사용 장면 사진을
    함께 올려도 둘 다 첫 장을 닮게 나왔다. 이제 카드가 자기 referenceId로 필요한 장을
    가리킨다.
    """
    urls: dict[str, str] = {}
    index = 0
    for material in task.input.reference_materials:
        if material.type != ReferenceMaterialType.IMAGE or not is_image_data_url(
            material.value
        ):
            continue
        urls[reference_image_id(index)] = material.value
        index += 1
    return urls


def _first_reference_image_url(task: BlogTask, evidence=None) -> str | None:
    """참고 이미지가 하나뿐이거나 카드가 어느 장인지 말하지 않을 때의 기본값."""
    urls = _safe_reference_image_urls(task, evidence)
    return next(iter(urls.values()), None)


def _preserves_brand_marks(evidence, reference_image: str | None) -> bool:
    """생성 이미지에서 브랜드 표식을 보존해야 하는가.

    전면 금지를 그대로 두면 나이키 글에서 스우시가 지워진 운동화가 나온다. 그렇다고 아무
    글에서나 로고를 허용하면 가짜 로고가 생긴다. 조건은 셋이 모두 참일 때뿐이다:
    참고 이미지를 실제로 편집 입력으로 쓰고 있고, 자료가 브랜드를 확인해 줬고, 그 제품이
    글의 대상이다. 그때도 지시는 '새로 그리지 말고 원본의 것을 그대로 두라'다.
    """
    return bool(reference_image and evidence and evidence.brand and evidence.primary_entity)


def _reuses_reference(card) -> bool:
    """사용자 참고 이미지를 생성 없이 그대로 쓰기로 한 카드인가."""
    return (getattr(card, "generated_or_reused", "") or "").upper() == "REUSED"


def _reference_url_for(card, urls: dict[str, str]) -> str | None:
    """이 카드가 기준으로 삼을 참고 이미지.

    GENERATED 카드는 옛 계획 호환을 위해 지정이 없으면 첫 장을 쓸 수 있다. REUSED는
    provenance 계약이 더 엄격하다. 계획이 지목한, 개인정보 검사를 통과한 정확한 한 장만
    허용하며 없으면 None이다.
    """
    if not card.uses_reference or not urls:
        return None
    if _reuses_reference(card):
        return urls.get(card.reference_id) if card.reference_id else None
    if card.reference_id and card.reference_id in urls:
        return urls[card.reference_id]
    return next(iter(urls.values()))


# 인물 확인용 참고 이미지는 최대 3장까지만 쓴다. 더 늘려도 정체성 근거가 더 좋아지지
# 않고 요청만 무거워진다.
MAX_PERSON_REFERENCE_IMAGES = 3


def _person_reference_urls(
    named_subject, urls: dict[str, str], card=None
) -> list[str]:
    """실존 인물·캐릭터의 정체성 확인에 쓸 참고 이미지(data URL).

    이름만으로는 그 사람의 얼굴이 재현되지 않는다. 사용자가 올린 사진이 있으면 그것이
    '누구인가'의 유일한 근거이므로, 카드가 usesReferenceImage를 켰는지와 **무관하게**
    고유 인물 카드에는 실어 보낸다(참고 이미지의 원래 용도는 제품 충실도였고, 그 판단은
    인물 정체성과 다른 질문이다).

    카드가 특정 장을 지목했으면 그 장이 첫 번째다 — 첫 장이 편집 입력이 된다.
    """
    if named_subject is None or not urls:
        return []
    ordered: list[str] = []
    preferred = getattr(card, "reference_id", None) if card is not None else None
    if preferred and preferred in urls:
        ordered.append(urls[preferred])
    for reference_id in getattr(named_subject, "reference_ids", ()) or ():
        if reference_id in urls and urls[reference_id] not in ordered:
            ordered.append(urls[reference_id])
    for url in urls.values():
        if url not in ordered:
            ordered.append(url)
    return ordered[:MAX_PERSON_REFERENCE_IMAGES]


# 한 글에 붙일 수 있는 웹 사진의 최대 장수. 썸네일 1장 + 고유 대상 본문 사진 몇 장이면
# 충분하다. 더 늘리면 한 글에서 남의 사진을 그만큼 더 퍼 오는 일이 된다.
MAX_WEB_PHOTOS = 4

# 그룹마다 자리 수보다 몇 장을 더 받아 둘 것인가. 픽셀 게이트가 배정된 사진을 떨어뜨렸을
# 때 같은 그룹의 예비가 그 자리를 잇는다 — 예전에는 질의당 확보가 자리 수만큼뿐이라
# 게이트 오판 한 번(2026-08-10 실사례: 진짜 개봉 포스터를 '합성 문구'로 오판)에 소재
# 사진이 전멸하고 생성 폴백만 남았다. 예비도 같은 게이트를 통과해야 실린다.
WEB_PHOTO_GATE_SPARES = 2


_HANGUL_RUN = re.compile(r"[가-힣][가-힣0-9]*")


def _korean_name(identity: str) -> str:
    """정체성 문자열에서 한글 이름만. 없으면 빈 문자열.

    계획이 만드는 정체성은 '백지헌 Baek Jiheon'처럼 한글+로마자다 — 장면 묘사가 영어라,
    그 이름이 mainSubject에 살아 있는지 보는 검증(named_subject_problem)을 통과하려면
    로마자가 함께 있어야 하기 때문이다. 그 문자열을 그대로 한국어 검색 API에 넣으면
    질의가 나빠지므로, 검색에는 한글 이름만 쓴다.
    """
    return " ".join(_HANGUL_RUN.findall(identity or "")).strip()


def _accepts_grounding(searcher) -> bool:
    """이 검색기가 '무엇을 찾는지'(정식 명칭·출연자)를 받아 쓸 수 있는가.

    네이버 이미지 검색과 유튜브 검색은 같은 ``find_photos(query, limit)`` 계약을 쓰지만,
    공식 회차 채점은 유튜브 쪽에만 있다. 스텁·구형 검색기까지 안전하게 다루려고 시그니처를
    직접 본다 — 검색기 종류를 이름으로 판단하면 테스트 스텁이 조용히 어긋난다.
    """
    try:
        return "program_name" in inspect.signature(searcher.find_photos).parameters
    except (TypeError, ValueError):  # 내장·C 구현 등 시그니처를 못 읽는 경우
        return False


def _web_photo_queries(
    task: BlogTask, named_subject, entity=None, visual_subject: str = ""
) -> list[str]:
    """웹 사진 검색 질의를 정밀한 것부터. 앞에서부터 시도해 처음 성공한 것을 쓴다.

    그룹명과 멤버명이 함께 있을 때 '프로미스나인 백지헌'이 '백지헌'보다 정확하다 —
    동명이인이 걸리는 것을 줄인다. 인물명으로 못 찾으면 소재로 넓힌다: 그 사람이 아니더라도
    **소재와 무관한 그림**만은 내보내지 않는다는 것이 이 폴백의 목적이다.

    소재 정체(entity)가 확인된 글에서는 그 정식 명칭과 관계가 맨 앞에 온다. 소재 문자열이
    일반 명사와 같은 콘텐츠가 있기 때문이다 — 사용자가 입력한 소재를 그대로 검색하면 그
    단어의 사전적 의미에 해당하는 사진이 걸린다. 확인된 정식 명칭에 브랜드나 인물 이름을
    붙여 물으면 그 대상의 사진일 확률이 크게 오른다.

    영상 콘텐츠만의 규칙이 아니다. 브랜드+정식 상품명은 '같은 종류의 다른 제품'을 걸러
    내는 유일한 장치이고, 그룹명+멤버명은 동명이인을 줄인다.

    ``visual_subject``는 카드 계획이 정한 '이 사진이 실제로 보여 줄 대상'이다(2026-08-05).
    소재만으로 물으면 브랜드 전체의 사진이 걸린다 — 소재 '디올'로 검색하면 향수도 매장도
    광고도 나오지만, 그 문단이 말하는 것은 '레이디 디올 핸드백'이다. 있으면 맨 앞에 두고,
    실패하면 예전 질의 사다리로 그대로 내려간다.
    """
    identity = ((named_subject.identity if named_subject else None) or "").strip()
    topic = (task.input.topic or "").strip()
    subject = (visual_subject or "").strip()
    # 한국어 검색이므로 한글 이름이 먼저다. 한글이 없는 정체성(예: 영어권 캐릭터명)은
    # 원래 문자열을 그대로 쓴다.
    name = _korean_name(identity) or identity

    queries: list[str] = []
    if entity is not None and entity.wants_real_image:
        queries.extend(entity.search_queries())
    if subject:
        # 소재가 시각 대상에 빠져 있으면 붙여서 먼저 묻는다 — '레이디 디올'만으로는
        # 다른 브랜드의 같은 이름이 걸릴 수 있다.
        if topic and topic not in subject:
            queries.append(f"{topic} {subject}")
        queries.append(subject)
    if name and topic and name not in topic and topic not in name:
        queries.append(f"{topic} {name}")
    if name:
        queries.append(name)
    # 한글 이름으로 못 찾으면 로마자까지 붙은 원래 정체성으로 한 번 더 — 한글 표기가
    # 흔하지 않은 인물은 이쪽이 걸린다.
    if identity and identity != name:
        queries.append(identity)
    if topic:
        queries.append(topic)
    seen: set[str] = set()
    return [q for q in queries if not (q in seen or seen.add(q))]


def _first_person_reference(card, urls: dict[str, str]) -> str | None:
    """고유 인물 카드의 image-to-image 편집 기준. 카드가 참고 이미지를 쓰겠다고 하지
    않았어도, 인물 사진이 있으면 그것이 얼굴의 유일한 근거라 기준으로 삼는다."""
    references = _person_reference_urls(named_subject_of(card), urls, card)
    return references[0] if references else None


def _without_private_information(
    data_url: str, regions: list[PrivateRegion], reference_id: str
) -> str | None:
    """개인정보가 보이는 자리를 검게 덮은 사본. 덮을 것이 없으면 받은 것을 그대로.

    덮었는지 **로그로 남긴다.** 사용자가 "왜 사진 일부가 검은가"를 물었을 때 답할 근거가
    있어야 하고, 반대로 번호판이 그대로 나갔을 때 '모델이 못 찾은 것'인지 '덮다 실패한
    것'인지 여기서 갈린다.
    """
    if not regions:
        return data_url
    masked = mask_data_url(data_url, regions)
    if masked is None:
        logger.warning(
            "개인정보를 찾았지만 안전하게 덮지 못해 이미지를 제외합니다 | %s - %d곳",
            reference_id,
            len(regions),
        )
        return None
    logger.info(
        "사진의 개인정보를 덮었습니다 | %s - %s",
        reference_id,
        ", ".join(region.kind or "미상" for region in regions),
    )
    return masked


def _leftover_spare_photos(
    spare_photos: dict[int, list["WebPhoto"]],
    thumbnail_photo: "WebPhoto | None",
    body_photos: dict[int, "WebPhoto"],
) -> list["WebPhoto"]:
    """배정·승격에 쓰이고 남은 예비 사진들(중복·사용분 제외, 발견 순서 유지).

    같은 그룹의 자리들이 같은 목록 객체를 공유하므로 여기서 URL로 걸러 한 번만 센다."""
    used = {
        photo.source_url
        for photo in (thumbnail_photo, *body_photos.values())
        if photo is not None
    }
    pool: list[WebPhoto] = []
    seen = set(used)
    for photos in spare_photos.values():
        for photo in photos:
            if photo.source_url in seen:
                continue
            seen.add(photo.source_url)
            pool.append(photo)
    return pool


def _safe_reference_image_urls(task: BlogTask, evidence=None) -> dict[str, str]:
    """개인정보 검사를 통과한 게시·편집용 사본만 reference id로 돌려준다."""
    if evidence is None:
        if _reference_image_urls(task):
            logger.warning("개인정보 검사가 없어 사용자 참고 이미지를 게시·편집에서 제외합니다.")
        return {}
    roles = {role.reference_id: role for role in evidence.reference_image_roles}
    safe: dict[str, str] = {}
    for reference_id, data_url in _reference_image_urls(task).items():
        role = roles.get(reference_id)
        if role is None or not role.privacy_scanned:
            logger.warning("개인정보 검사가 완료되지 않아 참고 이미지를 제외합니다 | %s", reference_id)
            continue
        normalized = normalize_data_url(data_url)
        if normalized is None:
            logger.warning("참고 이미지가 안전한 형식이 아니어서 제외합니다 | %s", reference_id)
            continue
        sanitized = _without_private_information(
            normalized, list(role.private_regions), reference_id
        )
        if sanitized is not None:
            safe[reference_id] = sanitized
    return safe


def _reference_images(
    task: BlogTask, title: str, evidence=None
) -> list[GeneratedPostImage]:
    """사용자가 올린 이미지는 새로 생성하기 전에 먼저 재사용한다.

    근거 프로필이 그 이미지를 화면 캡처·영수증으로 판정했으면 media_kind를 screenshot으로
    둔다 — 화면 캡처는 사진과 달리 얇은 회색 테두리가 있어야 잘린 것처럼 보이지 않는다.

    **개인정보는 여기서 덮는다.** 사용자가 올린 사진이 글로 들어가는 길목이 여기 하나라,
    번호판·전화번호가 찍힌 채 발행되는 것을 막을 마지막 자리다(2026-08-07 신고: 주차장
    사진의 차량 번호판이 그대로 읽혔다). 저장된 원본은 손대지 않는다 — 덮은 것은 글에
    싣는 사본이다.
    """
    roles = {
        role.reference_id: role
        for role in (evidence.reference_image_roles if evidence else [])
    }
    safe_urls = _safe_reference_image_urls(task, evidence)
    images: list[GeneratedPostImage] = []
    for reference_id, data_url in list(safe_urls.items())[:MAX_REFERENCE_IMAGES]:
        index = int(reference_id.rsplit("-", 1)[-1]) - 1
        role = roles[reference_id]
        images.append(
            GeneratedPostImage(
                data_url=data_url,
                alt_text=f"{title} 참고 이미지 {index + 1}",
                prompt="Uploaded reference image reused in the article.",
                provider="reference",
                model="uploaded-image",
                generated_at=_now(),
                mime_type=mime_type_from_data_url(data_url),
                source="reference",
                media_kind=(
                    "screenshot"
                    if role.role in ("SCREENSHOT_EVIDENCE", "RECEIPT_EVIDENCE")
                    else "reference"
                ),
                # 실제 화면·자료(스크린샷)는 AI 생성 이미지와 구분되도록 캡션으로 출처를 밝힌다.
                caption="사용자 제공 자료",
            )
        )
    return images


def _with_inserted_images(post: FinalPost, images: list[GeneratedPostImage]) -> FinalPost:
    """폴백 배치. 채워 넣을 [[IMAGE:]] 태그를 남기지 않은 원고를 위한 것.

    images[0]은 표지다 — 나머지와 함께 흩뿌려지지 않고 글을 이끈다. 그래서 이것이
    네이버가 대표 이미지로 집어 가는 이미지가 된다.
    """
    unique = dedupe_images(images)[:MAX_POST_IMAGES]
    if not unique:
        return post

    lead, rest = unique[0], unique[1:]
    markdown = post.markdown_content or f"# {post.title}\n\n{post.body}"

    # 태그를 찾지 못해 여기까지 왔다는 것이지, 원고에 태그가 없다는 뜻은 아니다. M4가
    # markdownContent에만 태그를 빠뜨리면 body와 htmlContent에는 남아 있고, 그대로
    # 두면 발행된 글에 `[[IMAGE: ...]]`가 글자 그대로 찍힌다.
    return post.model_copy(
        update={
            "body": strip_image_tags(post.body),
            "images": unique,
            "featured_image": lead,
            "html_content": (
                f"{image_html(lead)}\n"
                f"{insert_html_images(strip_image_tags(post.html_content), rest)}"
            ),
            "markdown_content": (
                f"{image_markdown(lead)}\n\n"
                f"{insert_markdown_images(strip_image_tags(markdown), rest)}"
            ),
        }
    )


def _rebuilt_from_html(post: FinalPost, title: str, raw_html: str) -> FinalPost:
    """사용자가 에디터에 남긴 그대로의 글.

    이 수정에서는 에디터의 HTML이 진실의 근원이다: 모든 문단, 소제목, 이미지가 어디로
    갔는지 말해 주므로 짐작할 게 없다. 글 안에서 이미지를 다른 곳으로 끌면 옮겨지고,
    지우면 빠진다.

    body와 markdownContent는 나란히 관리하는 대신 같은 HTML에서 유도한다 — 둘은
    htmlContent가 하는 말을 해야 하고, 유도하는 것만이 둘이 어긋나지 않는 유일한 방법이다.
    htmlContent가 네이버에 복사되고 발행되는 것이라, 거기에 닿지 못한 수정은 새 원고를
    보여주면서 옛 원고를 발행하게 된다.

    이미지는 새로 생성하지 않고 이미 글에 있던 것에 다시 맞춘다: 이 글을 위해 그려진
    것이고, 하나를 옮긴다고 틀려지지 않으며, 저장할 때마다 이미지 모델을 다시 부르면 실제
    비용이 든다.
    """
    parsed = parse_edited_html(raw_html)

    by_src = {image.data_url: image for image in _images_from_post(post)}
    kept = dedupe_images([by_src[src] for src in parsed.image_srcs if src in by_src])

    return post.model_copy(
        update={
            "title": title,
            "body": parsed.text,
            "images": kept or None,
            "featured_image": kept[0] if kept else None,
            "html_content": f"<article><h1>{escape(title)}</h1>{parsed.html[len('<article>'):]}",
            "markdown_content": f"# {title}\n\n{parsed.markdown}",
        }
    )


def _with_tagged_images(
    post: FinalPost,
    tagged: list[GeneratedPostImage],
    lead: list[GeneratedPostImage],
    tag_count: int,
) -> FinalPost:
    """태그마다 이미지를 채우고, 남는 것은 따로 배치한다.

    M4는 정확히 태그 두 개를 요청받지만 항상 두 개를 주지는 않는다. 이미지 개수는 제안이
    아니라 규격이라 모자란 만큼은 어차피 생성한다 — 채울 태그가 없는 이미지도 본문 어딘가에
    자리 잡아야 한다. 안 그러면 `images`에만 들어앉고 글에는 어디에도 안 나온다.
    """
    markdown_source = post.markdown_content or f"# {post.title}\n\n{post.body}"
    placed, spare = tagged[:tag_count], tagged[tag_count:]
    all_images = dedupe_images([*lead, *tagged])[:MAX_POST_IMAGES]

    lead_markdown = "\n\n".join(image_markdown(i) for i in lead)
    lead_html = "\n".join(image_html(i) for i in lead)

    markdown = insert_markdown_images(
        replace_image_tags(markdown_source, placed, image_markdown), spare
    )
    html = insert_html_images(replace_image_tags(post.html_content, placed, image_html), spare)

    return post.model_copy(
        update={
            "body": strip_image_tags(post.body),
            "images": all_images,
            "featured_image": all_images[0] if all_images else None,
            "html_content": f"{lead_html}\n{html}" if lead else html,
            "markdown_content": f"{lead_markdown}\n\n{markdown}" if lead else markdown,
        }
    )


class DraftService:
    def __init__(
        self,
        repository: BlogTaskRepository,
        draft_generator: DraftGenerator,
        post_image_generator: PostImageGenerator | None = None,
        user_settings_service: UserSettingsService | None = None,
        persona_service: PersonaService | None = None,
        job_lease: JobLease | None = None,
        photo_search: PhotoSearch | None = None,
        youtube_photo_search: PhotoSearch | None = None,
        final_reviewer: Any = None,
    ):
        self._repository = repository
        self._draft_generator = draft_generator
        self._post_image_generator = post_image_generator
        # 실제 사진을 찾는 검색기(네이버 이미지 / 유튜브 썸네일). 자격 증명이 없으면
        # None이고, 그때는 예전처럼 생성만 한다 — 검색이 없다고 원고 생성이 멈추지는
        # 않는다. 어떤 카드가 어떤 소스를 쓸지는 카드 계획(imageSource)이 정한다.
        self._photo_search = photo_search
        self._youtube_search = youtube_photo_search
        # 2차 품질 검수기(2026-08-07). 원고를 쓴 모델과 **다른 모델**이 같은 원고를 한 번
        # 더 보고, 그림은 실제로 본다. None이면 예전처럼 1차 검수만 돈다 — 없다고 원고
        # 생성이 멈추지는 않는다.
        self._final_reviewer = final_reviewer
        self._user_settings = user_settings_service
        self._personas = persona_service
        self._jobs = BackgroundJobs()
        # 같은 글의 원고 생성을 두 프로세스가 동시에 돌리지 않게 하는 임차. M4는 이미지
        # 모델까지 부르므로 두 벌 도는 것이 그대로 비용이다.
        self._lease = job_lease or NoOpJobLease()
        # 이 프로세스가 지금 원고를 쓰고 있는 글. 임차(위)는 **프로세스 사이의** 경합만
        # 막고, Redis가 없는 배포에서는 NoOpJobLease라 아무것도 막지 않는다. 한 프로세스
        # 안에서 같은 글이 두 번 도는 길은 따로 열려 있다: 첫 실행이 실패로 기록되면
        # (_mark_generation_failed) 사용자는 '다시 생성하기'를 누를 수 있는데, 그 실행이
        # 아직 살아 있어도 상태만 보고 판단하면 그대로 통과한다. 실제로 한 글에 세 벌이
        # 동시에 돌아 LLM 비용이 세 배로 나가고, 세 실행이 같은 레코드의 진행 상황을
        # 번갈아 덮어써서 화면이 '1단계에서 멈춤'을 보이는 동안 다른 실행은 3단계를
        # 지나고 있었다(2026-08-03).
        self._running_drafts: set[str] = set()
        #: 지금 이 프로세스가 쓰고 있는 글 → 그 주인(2026-08-12). 사용자 한 사람의
        #: **대화형 원고 생성을 하나로 묶기 위해** 들고 있다.
        #:
        #: 예약 작업은 여기 들어오지 않는다 — 그쪽은 ``generate_draft``로 들어와
        #: ``start_draft_generation``을 지나지 않는다. 예약이 도는 동안 새 소재로
        #: 글을 쓰는 것은 막지 않는다는 사용자 결정이 그렇게 지켜진다.
        self._running_draft_owners: dict[str, str] = {}
        # 글별 원고 잡 핸들. 기다리는 쪽이 **자기 글만** 기다리게 하려는 것이다.
        self._draft_jobs: dict[str, Any] = {}
        # 콘텐츠 설계 캐시: key -> (만료 시각(monotonic), 설계). inflight는 같은 키의
        # 설계 생성이 겹칠 때(선행 생성 중에 원고 생성이 시작) 두 번째 호출이 새 요청을
        # 보내는 대신 진행 중인 것을 기다리게 한다 — 같은 입력의 중복 API 요청 차단.
        self._plan_cache: dict[str, tuple[float, Any]] = {}
        self._plan_inflight: dict[str, asyncio.Task] = {}
        # 제목 계획은 글마다 하나뿐이라 postId로 잠근다. 선행 생성과 실제 생성이 겹쳐도
        # 제목 LLM은 한 번만 돈다(설계 캐시와 같은 이유·같은 방식).
        self._title_plan_inflight: dict[str, asyncio.Task] = {}
        # 의도 선택 직후 선행 생성과 실제 생성이 겹쳐도 SEO 계획 호출은 글마다 한 번이다.
        self._seo_plan_inflight: dict[str, asyncio.Task] = {}
        # 참고자료 근거·편집 스타일의 모델 출력 캐시(설계 캐시와 같은 방식). 이 둘은
        # 지금까지 캐시가 없어 선행 생성이 만들어 둔 결과를 실제 생성이 버리고 같은
        # LLM을 다시 불렀다 — 1단계(원고 구조 설계)가 매번 통째로 다시 돌던 이유이고
        # (2026-08-10 사용자: "구조 설계가 156초"), 근거 출력이 호출마다 조금씩 달라
        # 설계 캐시 키(anchor 포함)까지 어긋나게 했다. 실패는 캐시하지 않는다.
        self._evidence_cache: dict[str, tuple[float, Any]] = {}
        self._evidence_inflight: dict[str, asyncio.Task] = {}
        self._style_cache: dict[str, tuple[float, Any]] = {}
        self._style_inflight: dict[str, asyncio.Task] = {}

    async def shutdown(self) -> None:
        await self._jobs.cancel()

    async def _require_intent_selected_task(self, post_id: str) -> BlogTask:
        task = await self._repository.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")

        # A draft that failed left the post in FAILED, which is terminal — so asking
        # again was refused, and 다시 생성하기 could not work. The post itself is
        # fine: the input and the chosen intent are still there, and the model timing
        # out is not a reason to throw the post away. Un-fail it and let M4 run.
        if task.status == BlogTaskStatus.FAILED and task.selected_intent and not task.final_post:
            task = await self._repository.rewind_status(
                post_id, BlogTaskStatus.INTENT_SELECTED, DRAFT_RETRY_ACTOR
            )

        # **아무도 돌리고 있지 않은 GENERATING**도 되살린다.
        #
        # 프로세스가 죽으면(배포·재시작·크래시) 잡은 사라지는데 글은 GENERATING에 남는다.
        # 복구 스위퍼가 그걸 되돌리지만, 시작한 지 얼마 안 된 작업은 유예한다
        # (recovery.FRESH_SECONDS = 15분 — 멀쩡히 돌던 원고를 회수하지 않으려는 장치다).
        # 그 유예 창 안에서는 **사용자가 할 수 있는 일이 없었다**: 화면은 '아직 돌고
        # 있어요'라 말하고, 재시도를 누르면 여기서 GENERATING이라고 거절했다.
        #
        # 실제로 그렇게 막혔다(2026-08-06 신고). 서버가 19:50에 재시작했고, 19:43에
        # 시작한 원고가 유예에 걸려 회수되지 않은 채 남았다 — 진행 기록은 19:45에 멈춰
        # 있는데 화면은 13분째 '진행 중'이었고 재시도는 계속 거절됐다.
        #
        # **명시적인 요청은 유예보다 앞선다.** 사용자가 다시 만들라고 한 것이고, 그때
        # 확인할 것은 하나뿐이다: 지금 정말 누가 돌리고 있는가. 이 프로세스가 돌고 있는
        # 경우는 여기 오기 전에 막힌다(start_draft_generation의 _running_drafts), 그러니
        # 남은 것은 다른 프로세스의 임차뿐이다. 임차를 쥔 쪽이 있으면 되살리지 않는다 —
        # 그건 같은 글을 두 벌 쓰는 길이다.
        if (
            task.status == BlogTaskStatus.GENERATING
            and task.selected_intent
            and not task.final_post
        ):
            if await self._lease.is_held(lease_key(post_id, "m4")):
                raise BlogTaskError(
                    "DRAFT_IN_PROGRESS",
                    "다른 작업이 이 글의 원고를 쓰고 있습니다. 끝난 뒤에 다시 시도해 주세요.",
                )
            logger.info(
                "원고 생성이 중단된 채 남아 있어 되살립니다 | %s", short(post_id)
            )
            task = await self._repository.rewind_status(
                post_id, BlogTaskStatus.INTENT_SELECTED, DRAFT_RETRY_ACTOR
            )
            # 멈춘 진행 표시를 지운다. 남겨 두면 새 실행의 1단계 위에 옛 4단계가 겹친다.
            await self._repository.update_progress(post_id, None)

        if task.status != BlogTaskStatus.INTENT_SELECTED or task.selected_intent is None:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"M4 requires {BlogTaskStatus.INTENT_SELECTED.value}, received {task.status.value}",
            )
        return task

    async def _require_ready_draft_task(self, post_id: str) -> BlogTask:
        task = await self._repository.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        if task.status != BlogTaskStatus.READY_TO_PUBLISH or task.final_post is None:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"원고 수정은 {BlogTaskStatus.READY_TO_PUBLISH.value} 상태에서만 됩니다. 지금은 {task.status.value}.",
            )
        return task

    async def _load_generation_settings(self, user_id: str) -> DraftGenerationSettings | None:
        if self._user_settings is None:
            return None
        settings = await self._user_settings.get_by_user_id(user_id)
        if settings is None:
            return None
        persona_prompt = (
            await self._personas.resolve_prompt(
                settings.default_persona,
                settings.custom_persona,
            )
            if self._personas is not None
            else settings.default_persona
        )
        return DraftGenerationSettings(
            hashtag_count=settings.hashtag_count,
            article_length=settings.article_length,
            # 소재·트렌드 결합 방향. 예전엔 제목(M2)에만 전달됐는데, 원고(M4)도 이 방향으로
            # 본문의 소재·트렌드 연결을 잡아야 해서 함께 넘긴다.
            blend_mode=settings.blend_mode,
            # 저장된 id는 페르소나 도메인에서 실제 생성 프롬프트로 해석한다.
            default_persona=persona_prompt,
            # 해석 전의 id도 함께 넘긴다 — 표현 강도 표를 이름 대신 id로 조회한다.
            default_persona_id=settings.default_persona,
            custom_persona_name=settings.custom_persona_name,
            custom_persona_description=settings.custom_persona_description,
            custom_persona=settings.custom_persona,
        )

    async def start_draft_generation(
        self, post_id: str, raw_body: Any, *, owner_id: str | None = None
    ) -> BlogTask:
        """글을 GENERATING으로 옮기고 작업을 백그라운드 잡에 넘긴다.

        상태를 쓰는 즉시 반환하므로, M4와 이미지가 실제로 걸리는 1분 남짓 동안 클라이언트가
        요청을 붙잡고 있지 않는다. GENERATING 전이는 잠금 역할도 한다: 두 번째 요청은
        글이 더 이상 INTENT_SELECTED가 아님을 발견하고 거부된다.

        **상태만으로는 부족하다.** 실행이 실패로 기록되면 상태는 FAILED가 되고
        ``_require_intent_selected_task``가 그것을 INTENT_SELECTED로 되돌려 재시도를
        열어 준다 — 그런데 실패를 기록한 그 실행이 아직 살아 있을 수 있다(이미지 단계는
        예외를 삼키고 계속 간다). 그래서 '지금 이 프로세스가 이 글을 쓰고 있는가'를
        따로 본다.
        """
        # 검사와 등록 사이에 await가 없어야 한다. 있으면 거의 동시에 들어온 두 요청이
        # 둘 다 검사를 통과한 뒤 등록한다(asyncio는 await에서만 다른 요청으로 넘어간다).
        if post_id in self._running_drafts:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                "이 글은 이미 원고를 생성하고 있습니다. 끝날 때까지 기다려 주세요.",
            )
        # 한 사람이 **대화형 원고 생성을 두 개** 동시에 돌리지 못하게 한다(2026-08-12
        # 사용자 지시). 한 편이 5~8분 걸리고 크롬 프로필도 하나라, 둘이 겹치면 둘 다
        # 느려지고 어느 쪽이 무엇을 하는지 화면에서 읽을 수 없다.
        #
        # **막지 않는 것 둘**(사용자가 명시했다):
        # - 새로고침해서 돌던 작업을 이어 보는 것 — 그건 새 일이 아니라 같은 일이다.
        #   이 경로를 지나지 않는다(화면이 followTask로 따라붙는다).
        # - 예약 작업이 도는 동안 새 소재로 글을 쓰는 것 — 예약은 이 경로가 아니다.
        if owner_id is not None:
            busy = next(
                (
                    other
                    for other, who in self._running_draft_owners.items()
                    if who == owner_id and other != post_id
                ),
                None,
            )
            if busy is not None:
                raise BlogTaskError(
                    "INVALID_STATUS_TRANSITION",
                    "다른 글의 원고를 만들고 있습니다. 그 글이 끝난 뒤에 시작해 주세요.",
                )
        self._running_drafts.add(post_id)
        if owner_id is not None:
            self._running_draft_owners[post_id] = owner_id
        try:
            await self._require_intent_selected_task(post_id)
            request = validate_generate_draft_request(raw_body)

            generating = await self._repository.transition_status(
                post_id, BlogTaskStatus.GENERATING, DRAFT_GENERATION_ACTOR
            )

            reporter = ProgressReporter(self._repository, post_id, TaskPhase.DRAFT)
            await reporter.step(0)
        except BaseException:
            # 잡을 띄우기 전에 실패했으면 등록을 되돌린다 — 안 그러면 그 글은 이 프로세스가
            # 살아 있는 동안 영영 '생성 중'으로 남아 다시 시도할 수 없다.
            self._running_drafts.discard(post_id)
            self._running_draft_owners.pop(post_id, None)
            raise

        # 핸들을 글 단위로 붙잡아 둔다. 기다리는 쪽(generate_draft)이 **자기 글의 잡만**
        # 기다리게 하려는 것이다 — 아래 `_jobs.drain()`은 이 서비스의 잡을 전부 기다린다.
        # 여기서 start와 등록 사이에 await가 없어야 한다(그 사이에 잡이 끝나면 핸들을
        # 지운 뒤에 다시 넣게 된다).
        job = self._jobs.start(self._tracked_draft_generation(generating, request, reporter))
        self._draft_jobs[post_id] = job

        return generating.model_copy(update={"progress": None})

    async def _tracked_draft_generation(
        self, generating: BlogTask, request: Any, reporter: ProgressReporter
    ) -> None:
        """실행이 끝나면(성공·실패·취소 무엇이든) 등록을 지운다."""
        try:
            await self._run_draft_generation(generating, request, reporter)
        finally:
            self._running_drafts.discard(generating.post_id)
            self._running_draft_owners.pop(generating.post_id, None)
            self._draft_jobs.pop(generating.post_id, None)

    async def generate_draft(self, post_id: str, raw_body: Any) -> BlogTask:
        """시작하고 기다린다. 테스트와, 작업 핸들이 아니라 완성된 글을 원하는 것들이 쓴다.

        **자기 글의 잡만 기다린다.** 예전에는 `_jobs.drain()`으로 이 서비스의 잡을 전부
        기다렸는데, 그러면 브랜드 자동 생성처럼 백그라운드로 돌려 둔 글이 있을 때 서로의
        완료를 기다리게 된다 — 원고 두 편이 실제로는 나란히 돌면서도 먼저 끝난 쪽이
        늦게 끝난 쪽을 기다리고, 새 요청이 계속 들어오면 그만큼 더 기다린다(drain은
        도는 잡이 하나도 없을 때까지 반복한다).

        핸들이 없으면 기다리지 않는다 — 잡이 이미 끝났다는 뜻이고, 결과는 DB에 있다.

        **이미 쓰고 있는 원고가 있으면 다시 시작하지 않고 그것을 기다린다.**
        `start_draft_generation`은 그때 거절하는 것이 맞다 — 그쪽은 HTTP 요청이고,
        같은 글에 두 번째 생성을 걸어 주면 안 된다. 그런데 이 메서드의 계약은
        "이 글의 완성된 원고를 다오"이고, 지금 도는 그 잡이 만들어 낼 것이 바로 그것이다.

        예약 워커가 이 구별 없이 거절을 그대로 맞고 있었다(2026-08-06 신고). 작업이
        어떤 이유로든 다시 실행되면(재시도·제어 중단 뒤 재개) 앞 실행이 띄운 원고 잡이
        아직 돌고 있는데, 워커는 "이 글은 이미 원고를 생성하고 있습니다"를 받아 **작업을
        실패로 적었다.** 그 사이 원고는 정상적으로 끝나 저장됐다 — 발행 내역에는 '실패'가
        남고 글은 멀쩡히 완성돼 있는, 두 화면이 다른 말을 하는 상태다.
        """
        running = self._draft_jobs.get(post_id)
        if running is not None:
            logger.info(
                "원고 생성이 이미 돌고 있어 그것을 기다립니다 | %s", short(post_id)
            )
            await asyncio.gather(running, return_exceptions=True)
            return await self._repository.find_by_post_id(post_id)

        started = await self.start_draft_generation(post_id, raw_body)
        job = self._draft_jobs.get(started.post_id)
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)
        return await self._repository.find_by_post_id(started.post_id)

    async def _run_draft_generation(
        self,
        generating: BlogTask,
        request: Any,
        reporter: ProgressReporter,
    ) -> None:
        """임차를 잡은 프로세스만 실제로 돌린다.

        GENERATING 전이가 1차 잠금이지만 그것은 한 DB를 보는 프로세스들 사이의 경합만
        막는다. 임차는 그 위에서 '지금 살아 있는 워커가 붙들고 있는가'를 알려 준다 —
        시작 시 복구 스위퍼가 죽은 작업을 골라내는 근거이기도 하다.
        """
        held = await hold(self._lease, lease_key(generating.post_id, "m4"))
        if held is None:
            logger.info(
                "원고 생성 건너뜀 | %s - 다른 프로세스가 이미 돌리고 있습니다",
                short(generating.post_id),
            )
            return
        async with held:
            await self._run_draft_generation_locked(generating, request, reporter)

    async def _published_digests(self, draft_input) -> list:
        """중복 검사가 견줄 '이미 만들어 둔 내 글들'의 요약.

        **실패를 삼킨다.** 이 조회가 안 되면 검사가 SKIPPED가 될 뿐인데, 그것 때문에
        완성 직전의 원고를 잃으면 손해가 훨씬 크다. 옛 저장소(메서드가 없는 구형·테스트
        스텁)도 그대로 동작해야 하므로 메서드 존재부터 확인한다.
        """
        fetch = getattr(self._repository, "list_published_digests", None)
        if fetch is None:
            return []
        try:
            return await fetch(draft_input.user_id, exclude_post_id=draft_input.post_id)
        except Exception as error:  # noqa: BLE001 - 검사 하나가 원고를 잃게 하지 않는다
            logger.warning(
                "중복 검사용 기존 글 조회 실패(검사를 건너뜁니다) | %s - %s",
                short(draft_input.post_id),
                error,
            )
            return []

    async def _build_draft_input(
        self, task: BlogTask, style: str | None, format_: Any
    ) -> DraftGenerationInput:
        """원고 생성 입력. 실제 생성과 선행 설계(prefetch)가 같은 빌더를 쓴다 — 두 곳이
        서로 다른 입력을 만들면 캐시 키가 어긋나 선행 생성이 무의미해진다."""
        intent = task.selected_intent
        # M2에서 트렌드 제목을 고른 글이면 그 제목을 원고의 앵커로 넘긴다. 트렌드를
        # 건너뛴 글(skipped)은 None이라 원고 프롬프트가 예전과 동일하게 동작한다.
        trend = task.trend_selection
        trend_title = trend.final_topic if trend and not trend.skipped else None
        # 글의 방향을 한 덩어리로 묶어 원고 프롬프트에 넘긴다. 지금까지는 선택 의도 제목,
        # 의도 키워드, 제목 후킹이 서로 다른 자리에 흩어져 있거나(키워드는 아예 버려졌다)
        # 원고 단계에 닿지 않아, 긴 프롬프트 안에서 글의 각도가 흐려질 수 있었다.
        hook_type = trend.hook_type if trend and not trend.skipped else None
        intent_anchor = IntentAnchor(
            intent=intent.title,
            keywords=list(intent.keywords),
            hook_type=hook_type.value if hook_type else None,
        )
        return DraftGenerationInput(
            post_id=task.post_id,
            user_id=task.user_id,
            input=task.input,
            selected_intent=SelectedIntentForDraft(
                intent_id=intent.intent_id,
                title=intent.title,
                target_reader=intent.target_reader,
                rationale=intent.rationale,
                keywords=list(intent.keywords),
                # 과거 M3 문서에 자격증명·서명 URL이 남아 있어도 M4 provider·최종 캡션으로
                # 다시 전송하지 않는다. 새 수집은 앞단에서 막지만 재개 경로도 fail-closed다.
                sources=[
                    source
                    for source in (intent.sources or [])
                    if is_public_reference_url((source.url or "").strip())
                ],
            ),
            prompt_version=M4_PROMPT_VERSION,
            settings=await self._load_generation_settings(task.user_id),
            style=style,
            format=format_,
            trend_title=trend_title,
            # 사용자가 고른 원본 검색 키워드. 옛 문서에는 없어 빈 목록이고, 그때는 이
            # 값을 쓰는 규칙·검사가 통째로 빠진다(예전과 같은 동작).
            raw_keywords=(
                list(trend.selected_keywords) if trend and not trend.skipped else []
            ),
            intent_anchor=intent_anchor,
            # 이미 확정된 제목이 있으면 그대로 싣는다. 없으면 _with_title_plan이 만든다.
            title_plan=task.title_plan,
        )

    async def _run_draft_generation_locked(
        self,
        generating: BlogTask,
        request: Any,
        reporter: ProgressReporter,
    ) -> None:
        post_id = generating.post_id
        trace = perf.start_trace("m4-draft", post_id)
        try:
            draft_input = await self._build_draft_input(
                generating, request.style, request.format
            )

            # 이미지 단계에서 실패한 글의 재실행은 처음부터가 아니라 실패한 단계부터다.
            # 직전 실행이 본문까지 끝내고 남긴 저장점이 같은 입력이면, 근거·제목·스타일·
            # 설계·본문 생성을 통째로 건너뛴다.
            fingerprint = _draft_input_fingerprint(draft_input)
            checkpoint = await self._load_resumable_checkpoint(post_id, fingerprint)
            if checkpoint is not None:
                draft_input = checkpoint.draft_input
                result = checkpoint.result
                logger.info(
                    "원고 저장점에서 재개 | %s - 본문을 재사용하고 이미지 단계부터 다시 시작합니다",
                    short(post_id),
                )
            else:
                # 참고자료 근거를 가장 먼저 확정한다. 제목·스타일·설계·원고·이미지가 모두
                # "무엇이 확인됐는가"를 알고 시작해야, 자료가 있는데도 일반론으로 흐르거나
                # 사진 한 장에서 사용 경험을 끌어내는 일이 생기지 않는다.
                #
                # 구간마다 '지금 무엇을 하는 중'을 진행 카드에 흘린다(2026-08-07 사용자
                # 요청: 단계 이름만으로는 몇 분씩 기다리는 화면이 지루하다). 라벨은 실제로
                # 그 자리에서 도는 작업의 이름이어야 한다 — 측정 라벨 규칙과 같다.
                await _progress_detail(reporter, "Claude가 참고자료를 읽고 확인된 사실을 추리는 중이에요…")
                with perf.span("reference_evidence"):
                    draft_input = await self._with_reference_evidence(draft_input)

                # 제목을 확정한다. 설계 캐시 키가 확정 제목을 포함하므로 설계보다 앞서야
                # 한다. 의도 선택 직후의 선행 생성이 이미 만들어 저장해 뒀으면 DB 읽기 한 번으로
                # 끝난다.
                # 제목을 다시 고르는 것이 아니다 — 사용자가 고른 제목은 그대로고, 그 제목
                # 기준으로 검색 노출 구문(해시태그·본문 키워드의 재료)을 잡는 단계다.
                # 예전 문구("제목과 키워드를 확정")는 재결정처럼 읽혔다(2026-08-10 사용자 지적).
                await _progress_detail(reporter, "선택한 제목에 맞춰 검색 노출 키워드를 뽑는 중이에요…")
                with perf.span("title_plan"):
                    draft_input = await self._with_title_plan(draft_input, generating)

                # SEO는 확정 제목·근거만 필요하고 편집 스타일에는 의존하지 않는다. 먼저
                # 시작해 편집 스타일 판단과 겹친다. 콘텐츠 설계는 시각 예산을 쓰므로 반드시
                # 편집 스타일이 끝난 뒤에 시작한다.
                seo_task = self._start_seo_keyword_plan(generating, draft_input)
                try:
                    await _progress_detail(reporter, "글 소재에 맞는 편집 스타일을 고르는 중이에요…")
                    with perf.span("editorial_style"):
                        draft_input = await self._with_editorial_style(draft_input, generating)

                    await _progress_detail(
                        reporter, "Claude가 더 좋은 글을 주기 위해 글의 뼈대를 기획하는 중이에요…"
                    )
                    draft_input = await self._with_content_and_seo_plans(
                        draft_input, generating, seo_task=seo_task
                    )
                finally:
                    await self._finish_seo_keyword_plan(seo_task)

                await reporter.step(1)
                source_count = len(draft_input.selected_intent.sources or [])
                await _progress_detail(
                    reporter,
                    (
                        f"Claude 작가가 확인된 자료 {source_count}건을 대조해 가며 본문을 쓰는 중이에요… (가장 오래 걸리는 구간이에요)"
                        if source_count
                        else "Claude 작가가 본문을 쓰는 중이에요… (가장 오래 걸리는 구간이에요)"
                    ),
                )
                result = await self._generate_checked_draft(draft_input)
                # 본문까지는 끝났다 — 이미지에서 실패해도 여기까지는 다시 하지 않도록 남긴다.
                await self._store_draft_checkpoint(
                    post_id, fingerprint, draft_input, result
                )

            plan = getattr(result, "content_plan", None)
            if plan is not None and getattr(plan, "sections", None):
                # 무엇이 정해졌는지 한 줄로 남긴다(2026-08-11 사용자 요청). 예전에는
                # 5~8분 동안 단계 이름만 바뀌고 그 안에서 무엇이 정해졌는지는 서버
                # 로거에만 있었다.
                rendered = [s for s in plan.sections if s.visual_type != "NONE"]
                await _progress_detail(
                    reporter,
                    f"본문 구조를 {len(plan.sections)}개 소제목으로 짰어요"
                    + (f" (표·그래프 계획 {len(rendered)}곳)" if rendered else ""),
                )

            await reporter.step(2)
            # 사진 계획(원고 완성 후): 성공하면 자연 사진 파이프라인, 실패·미지원이면
            # 썸네일 중심 호환 경로로 진행한다.
            await _progress_detail(reporter, "본문에 어울리는 이미지 구성을 계획하는 중이에요…")
            with perf.span("visual_plan_llm"):
                planned, named_subject = await self._plan_visual_cards(
                    result, generating, draft_input
                )
            if planned is not None:
                plan, selected = planned
                result = result.model_copy(update={"card_plan": plan})
                visuals_note = (
                    f" + 표·그래프 {len(selected.visuals)}장" if selected.visuals else ""
                )
                await _progress_detail(
                    reporter,
                    f"사진 구성을 정했어요 — 대표 1장 + 본문 사진 {len(selected.body_cards)}장{visuals_note}",
                )
                result = await self._with_card_images(
                    result, generating, draft_input, selected, reporter=reporter
                )
            else:
                await _progress_detail(reporter, "본문에 넣을 사진을 찾아 배치하는 중이에요…")
                result = await self._with_post_images(
                    result, generating, draft_input, named_subject
                )
                # 사진 배치가 끝난 뒤에 코드 렌더링 시각자료(차트·과정도·인포그래픽)를 마커
                # 자리에 채운다 — 사진 배치 로직이 images 목록을 다시 세우므로 그 뒤여야
                # 렌더링 이미지가 목록에서 사라지지 않는다.
                result = self._with_rendered_visuals(result, draft_input)

            await reporter.step(3)
            # 마지막 관문: 완성된 원고와 이미지를 사용자 입력·조사 자료와 대조한다. 여기까지의
            # 검사는 전부 형식(길이·해시태그·낚시 표현·SEO 배치)을 봤다 — 글이 자료와 실제로
            # 맞는 말을 하는지는 아무도 확인하지 않았다.
            result = await self._with_final_review(result, draft_input, reporter)
            # 사실이 정리된 뒤에 문장을 다듬는다(5단계). 순서가 반대면 애써 다듬은 문장을
            # 검수가 다시 갈아엎고, 검수가 끼워 넣은 교정 문장은 아무도 다듬지 않는다.
            #
            # 단, 비평 → 통합 재작성이 실제로 원고를 다시 썼으면 건너뛴다(2026-08-07,
            # "4단계가 너무 오래 걸린다"). 재작성 프롬프트가 다듬기 지침(AI 말투·군더더기·
            # 문체 유지)을 안고 원고 전체를 다시 쓰므로, 그 위에 또 한 번 전문을 읽는
            # 다듬기 호출은 순차 LLM 대기 하나를 통째로 더할 뿐이다. 재작성이 거절·실패로
            # 원본을 유지했을 때(mode 없음)는 예전대로 다듬는다.
            final_review = getattr(result, "final_review", None)
            if getattr(final_review, "mode", None) == "critique-rewrite":
                await _progress_detail(
                    reporter, "품질 검수를 마쳤어요. 완성된 원고를 저장하는 중이에요…"
                )
            else:
                await _progress_detail(
                    reporter, "어색한 문장과 AI 말투를 자연스럽게 다듬는 중이에요…"
                )
                result = await self._with_polish(result, draft_input)
                await _progress_detail(
                    reporter, "품질 검수를 마쳤어요. 완성된 원고를 저장하는 중이에요…"
                )
            # 마무리 블록(2026-08-19). **여기가 맨 마지막이다** — 검수·다듬기가 끝난
            # 뒤에 붙인다. 그 앞에 두면 검수가 이 블록을 광고 문구로 읽고 지적하거나
            # 고쳐 버린다(본문에는 권유 문장을 금지해 두었기 때문에 더욱 그렇다).
            #
            # 모델이 쓰게 두지 않는 이유는 이 글자가 **사실**이기 때문이다 — 크레딧 수·
            # 가입 조건이 회차마다 흔들리면 안 된다.
            result = self._with_closing(result, draft_input)
            with perf.span("database_save"):
                await self._save_draft_generation_result(post_id, result)
            # 끝까지 성공했으니 저장점은 더 이상 근거가 아니다 — 남겨 두면 다음 재생성이
            # 새 글 대신 이 글의 본문을 재사용할 위험만 생긴다.
            await self._discard_draft_checkpoint(post_id)
            await reporter.clear(ok=True)
        except Exception as error:
            # 예외 종류와 스택을 함께 남긴다. 예전에는 str(error)만 찍어서, 메시지가 비어
            # 있거나 짧은 예외(KeyError·AttributeError·타임아웃)는 로그에 사실상 아무것도
            # 남기지 않았다 — 실패는 보이는데 왜 실패했는지는 어디에도 없었다. 원고 생성은
            # 3분짜리 비동기 작업이라 재현이 비싸므로, 한 번의 실패에서 원인을 알아야 한다.
            logger.warning(
                "원고 생성 실패 | %s - %s: %s",
                short(post_id),
                type(error).__name__,
                error,
                exc_info=True,
            )
            await self._mark_generation_failed(post_id)
            await reporter.clear(ok=False)
        finally:
            trace.finish()

    async def _review_once(self, draft_input: DraftGenerationInput, post) -> FinalReviewReport:
        """검수 한 회차. 검수기가 둘이면 **나란히 돌리고 지적을 합친다**(2026-08-07).

        원고를 쓴 모델이 자기 글을 보면 같은 자리를 같은 이유로 지나친다. 그래서 다른
        모델이 같은 원고를 한 번 더 보고, 그쪽은 **그림도 실제로 본다**(1차 검수는 픽셀을
        올리지 않고 대체텍스트·캡션만 본다).

        둘을 동시에 부르므로 이 회차의 시간은 느린 쪽 하나다 — 순서를 두면 두 배가 된다.

        **2차가 실패해도 1차 결과로 계속한다.** 이 단계는 마무리이지 관문이 아니고,
        1차만으로도 예전과 같은 검수가 된다. 반대로 1차가 실패하면 그대로 올린다 —
        호출부가 그것을 받아 검수 없이 원고를 내보낸다.
        """
        primary_call = self._draft_generator.review_final_draft(draft_input, post)
        if self._final_reviewer is None:
            return _as_review_report(await primary_call)

        primary, second = await asyncio.gather(
            primary_call,
            self._final_reviewer.review_final_draft(draft_input, post),
            return_exceptions=True,
        )
        if isinstance(primary, BaseException):
            raise primary
        report = _as_review_report(primary)
        if isinstance(second, BaseException):
            logger.warning(
                "2차 품질 검수 실패(1차 결과로 계속합니다) | %s - %s: %s",
                short(draft_input.post_id),
                type(second).__name__,
                second,
            )
            return report
        return _merge_review_reports(report, _as_review_report(second))

    async def _with_final_review(
        self, result: Any, draft_input: DraftGenerationInput, reporter: Any = None
    ) -> Any:
        """최종 검수(4단계).

        **두 갈래다(2026-08-07).** 원고를 쓴 어댑터가 비평·통합을 지원하면 새 경로
        (_with_dual_critique)로 간다 — 두 모델이 각자 결론을 내고, 그 통합으로 원고를
        다시 쓴다. 지원하지 않는 어댑터(구형·테스트 스텁·평가 harness)는 아래의 예전
        경로 그대로다: 지적(quote→replacement)을 받아 그 자리만 바꾼다.

        --- 이하 예전 경로 ---

        문제가 남아 있는 동안 정해진 회차만큼 검수하고 그 자리만 고친다.

        한 회차 = 모델 호출 한 번이다. 검수 모델이 항목별 판정과 **고칠 문장**을 함께
        돌려주므로 교정에 별도 호출이 필요 없다. 회차를 도는 이유는 첫 교정이 남긴 문제나
        새로 드러난 문제를 잡기 위해서다 — 고칠 것이 없으면 첫 회차에서 끝난다(대부분의 글).

        회차 상한은 설정값이다(FINAL_REVIEW_MAX_ROUNDS). 0으로 두면 검수를 건너뛴다.

        실패해도 원고를 버리지 않는다. 여기 도착한 원고는 이미 규격·SEO 검사를 통과한
        완성본이고, 검수는 그 위에 얹은 마무리다. 모델이 죽었다고 사용자가 결과를 못 받으면
        그게 더 나쁘다 — 그때는 사유만 결과에 남긴다.
        """
        if (
            getattr(self._draft_generator, "critique_final_draft", None) is not None
            and getattr(self._draft_generator, "integrate_critiques", None) is not None
            and final_review_max_rounds() > 0
        ):
            return await self._with_dual_critique(result, draft_input, reporter)

        reviewer = getattr(self._draft_generator, "review_final_draft", None)
        max_rounds = final_review_max_rounds()
        if reviewer is None or max_rounds <= 0:
            return result

        post = result.final_post
        applied_total = 0
        removed_total = 0
        remaining: list[FinalReviewIssue] = []
        targets: list[FinalReviewTarget] = []
        report: FinalReviewReport | None = None
        failure: str | None = None
        rounds = 0

        for round_number in range(1, max_rounds + 1):
            rounds = round_number
            try:
                await _progress_detail(
                    reporter, "Claude 편집자가 완성된 원고를 자료와 대조해 검수하는 중이에요…"
                )
                with perf.span(f"final_review_{round_number}") as meta:
                    report = await self._review_once(draft_input, post)
                    meta["issues"] = len(report.issues)
                    meta["status"] = report.overall_status
            except Exception as error:
                # 검수만 실패한 것이다. 원고는 이미 완성돼 있으므로 그대로 쓰고, 왜 검수가
                # 없는지만 결과에 남긴다 — 아무것도 남기지 않으면 '검수를 통과했다'와
                # '검수가 돌지 않았다'를 구분할 수 없다.
                failure = f"{type(error).__name__}: {error}"
                logger.warning(
                    "최종 검수 실패(원고는 그대로 씁니다) | %s - %s",
                    short(draft_input.post_id),
                    failure,
                )
                break

            critical = [issue for issue in report.issues if issue.severity == "critical"]
            if not critical:
                remaining = [issue for issue in report.issues if issue.severity != "critical"]
                break

            await _progress_detail(reporter, "검수에서 나온 지적 사항을 확인하는 중이에요…")
            if any(issue.kind == "image" for issue in critical):
                await _progress_detail(reporter, "본문과 이미지가 서로 맞는지 다시 보는 중이에요…")
            else:
                await _progress_detail(reporter, "지적된 문장을 고쳐 쓰는 중이에요…")

            post, applied, removed, unapplied, touched = apply_review(post, report.issues)
            applied_total += applied
            removed_total += removed
            remaining = unapplied
            targets.extend(touched)

            logger.info(
                "최종 검수 %d/%d회차 | %s - 판정 %s(%d점), 지적 %d건, 교정 %d건, 이미지 제외 %d장",
                round_number,
                max_rounds,
                short(draft_input.post_id),
                report.overall_status,
                report.overall_score,
                len(critical),
                applied,
                removed,
            )

            # 아무것도 반영하지 못했다면 한 번 더 물어도 같은 답이 온다. 원고는 그대로이고
            # 지적도 그대로이기 때문이다 — 회차만 태우지 않고 여기서 멈춘다.
            if applied == 0 and removed == 0:
                break

        await _progress_detail(reporter, "사실 검수를 마쳤어요…")
        review = FinalReviewResult(
            reviewed_at=_now(),
            provider=result.provider,
            model=result.model,
            rounds=rounds,
            overall_status=report.overall_status if report else "warning",
            overall_score=report.overall_score if report else 0,
            checks=report.checks if report else {},
            issues=remaining,
            revision_targets=targets,
            applied=applied_total,
            removed_images=removed_total,
            error=failure,
        )
        return result.model_copy(update={"final_post": post, "final_review": review})

    async def _with_dual_critique(
        self, result: Any, draft_input: DraftGenerationInput, reporter: Any = None
    ) -> Any:
        """비평 → 통합 재작성(2026-08-07 사용자 결정).

        1. 원고를 쓴 모델(Claude)과 다른 모델(GPT, 그림을 실제로 봄)이 **나란히** 각자
           결론을 낸다 — 좋은 점·아쉬운 점·개선점(+이미지·배치).
        2. 원고를 쓴 모델이 두 검토를 통합해 원고 전체를 다시 쓴다. 검토의 출처는
           가린다(A·B) — 자기 검토를 편들지 않게. 버린 지적도 이유와 함께 남긴다.
        3. 코드가 재작성본을 검사한다: 이미지 자리표가 전부 살아 있는가, 길이가 규격
           안인가, SEO·콘텐츠 검증(FAIL 셋: 제목 Primary·근거 없는 그래프·지어낸 경험)을
           통과하는가. 하나라도 어기면 **재작성 전체를 버리고 원본을 쓴다** — 이 단계는
           마무리이지 관문이 아니고, 여기 도착한 원고는 이미 완성본이다.

        어느 호출이 실패해도 원고를 잃지 않는다. 2차 검토가 죽으면 1차만으로 통합하고,
        통합이 죽으면 원본을 그대로 쓰고 사유만 남긴다.
        """
        post = result.final_post
        model_markdown, image_blocks = markdown_with_placeholders(post)

        second = (
            getattr(self._final_reviewer, "critique_final_draft", None)
            if self._final_reviewer is not None
            else None
        )
        # 문구는 실제 구성 그대로: 1차는 Claude, 2차는 GPT(그림을 실제로 봄)다.
        # 2차 검토기가 없는 배포에서는 Claude 혼자라고 말한다 — 있는 그대로만 말한다.
        await _progress_detail(
            reporter,
            (
                "Claude와 GPT가 나란히 원고를 읽고 고칠 점을 찾는 중이에요…"
                if second is not None
                else "Claude 비평가가 원고를 읽고 고칠 점을 찾는 중이에요…"
            ),
        )
        with perf.span("dual_critique") as meta:
            if second is not None:
                primary, secondary = await asyncio.gather(
                    self._draft_generator.critique_final_draft(
                        draft_input, post, model_markdown
                    ),
                    second(draft_input, post, model_markdown),
                    return_exceptions=True,
                )
            else:
                try:
                    primary = await self._draft_generator.critique_final_draft(
                        draft_input, post, model_markdown
                    )
                except Exception as error:  # noqa: BLE001 - 마무리이지 관문이 아니다
                    primary = error
                secondary = None
            meta["primary_ok"] = not isinstance(primary, BaseException)
            meta["secondary_ok"] = (
                secondary is not None and not isinstance(secondary, BaseException)
            )

        reviews: list[str] = []
        for label, critique in (("1차", primary), ("2차", secondary)):
            if critique is None:
                continue
            if isinstance(critique, BaseException):
                logger.warning(
                    "%s 원고 비평 실패 | %s - %s: %s",
                    label,
                    short(draft_input.post_id),
                    type(critique).__name__,
                    critique,
                )
                continue
            reviews.append(json.dumps(critique, ensure_ascii=False))

        def keep_original(failure: str) -> Any:
            review = FinalReviewResult(
                reviewed_at=_now(),
                provider=result.provider,
                model=result.model,
                rounds=1,
                overall_status="warning",
                overall_score=0,
                error=failure,
            )
            return result.model_copy(update={"final_review": review})

        if not reviews:
            return keep_original("두 검토가 모두 실패해 원고를 그대로 씁니다.")

        await _progress_detail(reporter, "비평 의견을 모아 원고를 다시 다듬어 쓰는 중이에요…")
        try:
            with perf.span("critique_integration"):
                integrated = await self._draft_generator.integrate_critiques(
                    draft_input,
                    post,
                    model_markdown,
                    reviews[0],
                    reviews[1] if len(reviews) > 1 else None,
                )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "검토 통합 실패(원고는 그대로 씁니다) | %s - %s: %s",
                short(draft_input.post_id),
                type(error).__name__,
                error,
            )
            return keep_original(f"검토 통합 실패: {type(error).__name__}")

        decisions = [
            item for item in (integrated.get("decisions") or []) if isinstance(item, dict)
        ]
        adopted = [item for item in decisions if item.get("adopted") is True]
        rejected = [item for item in decisions if item.get("adopted") is not True]
        improved = integrated.get("improvedMarkdown")
        if not isinstance(improved, str) or not improved.strip():
            return keep_original("통합이 개선 원고를 돌려주지 않았습니다.")

        rebuilt = rebuild_post(post, improved, image_blocks)
        if rebuilt is None:
            return keep_original(
                "재작성이 이미지 자리를 훼손해 원본 원고를 유지했습니다."
            )

        # 규격 재검사. 원본은 이미 통과했으므로, 어기는 것은 재작성이 만든 문제다.
        min_chars, _target = article_length_targets(draft_input.settings)
        max_chars = article_length_pass_max(draft_input.settings)
        measured = body_char_count(rebuilt.body)
        if measured < min_chars or measured > max_chars:
            return keep_original(
                f"재작성 길이({measured}자)가 규격({min_chars}~{max_chars}자)을 벗어나"
                " 원본 원고를 유지했습니다."
            )

        # SEO·콘텐츠 검증. FAIL 셋(제목 Primary·근거 없는 그래프·지어낸 경험)만 본다 —
        # WARN은 원본도 받던 신호라 재작성을 버릴 사유가 아니다. 중복 검사(published)는
        # 다시 돌리지 않는다: 같은 글을 다듬은 것이라 결과가 달라질 수 없다.
        validation = run_content_validations(
            rebuilt,
            draft_input.seo_keyword_plan,
            draft_input.selected_intent.sources,
            draft_input.input.reference_materials,
            visuals=result.visuals,
            purposes=list(draft_input.input.purpose or draft_input.input.keywords),
            evidence=draft_input.reference_evidence,
            title_locked=True,
            raw_keywords=list(draft_input.raw_keywords),
            published=None,
        )
        if validation.status == "FAIL":
            failed = [c.name for c in validation.checks if c.status == "FAIL"]
            return keep_original(
                f"재작성이 검증({', '.join(failed)})에 걸려 원본 원고를 유지했습니다."
            )

        for item in rejected:
            logger.info(
                "검토 지적 미반영 | %s - [%s] %s: %s",
                short(draft_input.post_id),
                item.get("source", "?"),
                str(item.get("point", ""))[:60],
                str(item.get("reason", ""))[:80],
            )
        logger.info(
            "비평 통합 재작성 반영 | %s - 반영 %d건, 미반영 %d건 (검토 %d편)",
            short(draft_input.post_id),
            len(adopted),
            len(rejected),
            len(reviews),
        )

        await _progress_detail(reporter, "원고 검토를 마쳤어요…")
        review = FinalReviewResult(
            reviewed_at=_now(),
            provider=result.provider,
            model=result.model,
            rounds=1,
            overall_status="revise" if adopted else "pass",
            overall_score=100,
            # 재작성이 실제로 반영됐다는 표식. 재작성 프롬프트가 표현 다듬기 지침을 안고
            # 있어, 호출부가 이것을 보고 별도 문장 다듬기 호출을 건너뛴다.
            mode="critique-rewrite",
            # 반영한 지적을 손댄 목록으로 남긴다 — 화면의 '일부 표현 자동 수정'이 읽는다.
            revision_targets=[
                FinalReviewTarget(
                    kind="paragraph",
                    reference=str(item.get("point", ""))[:60],
                    action="rewritten",
                    note=str(item.get("reason", ""))[:120],
                )
                for item in adopted
            ],
            applied=len(adopted),
            error=None,
        )
        return result.model_copy(update={"final_post": rebuilt, "final_review": review})

    async def _with_polish(self, result: Any, draft_input: DraftGenerationInput) -> Any:
        """문장 다듬기(5단계). 사실은 그대로 두고 어색한 문장·AI 말투만 그 자리에서 고친다.

        모델 호출은 한 번이다. 검수(4단계)가 회차를 도는 이유는 '고친 자리가 새 문제를
        만들 수 있어서'인데, 여기서 고치는 것은 표현이라 두 번째 회차가 잡을 것은 취향
        차이밖에 남지 않는다 — 완성된 원고를 두고 호출만 쌓인다.

        모델이 돌려준 교정을 그대로 믿지 않는다. 새 수치를 끌어들였거나, 없던 경험을
        지어냈거나, 검색 키워드를 떨어뜨렸거나, 이미지·표 표식을 물고 있는 교정은
        apply_polish가 버린다(무엇을 왜 버렸는지는 결과에 남는다).

        실패해도 원고를 버리지 않는다. 여기 도착한 원고는 규격·SEO 검사와 사실 검수까지
        끝낸 완성본이고, 다듬기는 그 위에 얹은 마무리다.
        """
        polisher = getattr(self._draft_generator, "polish_final_draft", None)
        if polisher is None:
            return result

        post = result.final_post
        allow_experience = _has_explicit_experience_material(draft_input)
        plan = draft_input.seo_keyword_plan
        keywords = tuple(k for k in ([plan.primary, *plan.secondary] if plan else []) if k)

        try:
            with perf.span("polish") as meta:
                edits = await polisher(
                    draft_input, post, has_experience_material=allow_experience
                )
                meta["edits"] = len(edits)
        except Exception as error:
            logger.warning(
                "문장 다듬기 실패(원고는 그대로 씁니다) | %s - %s: %s",
                short(draft_input.post_id),
                type(error).__name__,
                error,
            )
            return result

        polished, judged = apply_polish(
            post, list(edits), keywords=keywords, allow_experience=allow_experience
        )
        applied = [edit for edit in judged if edit.applied]

        # 무엇이 어떻게 바뀌었는지는 로그로 남긴다. 화면에는 다듬어진 글만 보이므로,
        # 문장이 이상해졌을 때 원인을 볼 수 있는 곳이 여기밖에 없다.
        for edit in applied:
            logger.info(
                "문장 다듬기(%s) | %s\n  전: %s\n  후: %s",
                edit.kind,
                short(draft_input.post_id),
                edit.before[:120],
                edit.after[:120] or "(삭제)",
            )
        logger.info(
            "문장 다듬기 | %s - 제안 %d건, 반영 %d건, 거절 %d건",
            short(draft_input.post_id),
            len(judged),
            len(applied),
            len(judged) - len(applied),
        )

        polish = PolishResult(
            polished_at=_now(),
            provider=result.provider,
            model=result.model,
            applied=len(applied),
            rejected=len(judged) - len(applied),
            edits=judged,
        )
        return result.model_copy(update={"final_post": polished, "polish": polish})

    async def _load_resumable_checkpoint(
        self, post_id: str, fingerprint: str
    ) -> DraftCheckpoint | None:
        """재개에 쓸 수 있는 저장점만 돌려준다. 조회 실패는 '없음'과 같다 — 저장점은
        최적화이지 생성의 전제 조건이 아니다."""
        try:
            checkpoint = await self._repository.load_draft_checkpoint(post_id)
        except Exception as error:
            logger.warning(
                "원고 저장점 조회 실패(처음부터 생성) | %s - %s", short(post_id), error
            )
            return None
        if checkpoint is None:
            return None
        if (
            checkpoint.stage != DRAFT_CHECKPOINT_STAGE_DRAFT_READY
            or checkpoint.fingerprint != fingerprint
        ):
            logger.info(
                "원고 저장점 무시 | %s - 입력이 바뀌어 처음부터 다시 생성합니다",
                short(post_id),
            )
            return None
        return checkpoint

    async def _store_draft_checkpoint(
        self,
        post_id: str,
        fingerprint: str,
        draft_input: DraftGenerationInput,
        result: DraftGenerationResult,
    ) -> None:
        try:
            await self._repository.save_draft_checkpoint(
                post_id,
                DraftCheckpoint(
                    fingerprint=fingerprint,
                    stage=DRAFT_CHECKPOINT_STAGE_DRAFT_READY,
                    draft_input=draft_input,
                    result=result,
                    saved_at=_now(),
                ),
            )
        except Exception as error:
            logger.warning("원고 저장점 기록 실패(무시) | %s - %s", short(post_id), error)

    async def _discard_draft_checkpoint(self, post_id: str) -> None:
        try:
            await self._repository.clear_draft_checkpoint(post_id)
        except Exception as error:
            logger.warning("원고 저장점 삭제 실패(무시) | %s - %s", short(post_id), error)

    async def _store_generation_result(
        self, post_id: str, result: DraftGenerationResult
    ) -> None:
        """완성된 원고를 저장한다. **DB가 한 번 흔들렸다고 원고를 버리지 않는다.**

        이 쓰기가 실패하면 5분 42초와 LLM·이미지 비용이 통째로 사라진다. 실제로 그랬다
        (2026-08-06: `stage=database_save dur=122.838s ok=False`, 사용자에게는 '원고 생성
        실패'로만 보였다). 그런데 여기까지 온 원고는 이미 다 만들어져 손에 있다 —
        다시 만드는 것보다 다시 저장해 보는 쪽이 언제나 싸다.

        **재시도가 안전한 이유**: 이 쓰기는 버전을 지키는 findAndModify다. 첫 시도가
        사실은 성공했는데 응답만 못 받았다면 두 번째 시도는 버전이 어긋나
        `INVALID_STATUS_TRANSITION`으로 떨어지고, 부르는 쪽이 그때 "이미 최종 원고가
        저장되어 있습니다"를 확인하고 조용히 끝낸다. 같은 원고가 두 번 저장되지 않는다.

        실패해도 저장점(`draftCheckpoint`)은 그대로 두므로, 다시 시도할 때 본문·이미지를
        처음부터 다시 만들지 않는다.
        """
        payload_bytes = _generation_payload_bytes(result)
        last_error: Exception | None = None
        for attempt in range(1, SAVE_ATTEMPTS + 1):
            try:
                await self._repository.save_draft_generation_result(
                    post_id, result, DRAFT_GENERATION_ACTOR
                )
                if attempt > 1:
                    logger.info(
                        "원고 저장 성공(%d번째 시도) | %s", attempt, short(post_id)
                    )
                return
            except BlogTaskError:
                # 상태·버전 문제다. 다시 보내도 같은 답이 오므로 부르는 쪽이 판단한다.
                raise
            except Exception as error:  # noqa: BLE001 — DB가 흔들린 것과 코드 문제를 여기서 가르지 않는다
                last_error = error
                # 크기를 함께 남긴다. 다음에 또 나면 '문서가 커서'인지 '연결이 흔들려서'인지
                # 이 숫자 하나로 갈린다 — 지금까지는 그것을 알 방법이 없었다.
                logger.warning(
                    "원고 저장 실패(%d/%d) | %s - %s: %s (보낸 크기 %.1fMB)",
                    attempt,
                    SAVE_ATTEMPTS,
                    short(post_id),
                    type(error).__name__,
                    error,
                    payload_bytes / 1_048_576,
                )
                if attempt < SAVE_ATTEMPTS:
                    await asyncio.sleep(SAVE_RETRY_SECONDS * attempt)
        assert last_error is not None
        if await self._generation_result_landed(post_id):
            return
        raise last_error

    async def _generation_result_landed(self, post_id: str) -> bool:
        """세 번 다 실패로 보였을 때, **정말 안 들어갔는지 한 번 되읽는다.**

        시간 초과는 '쓰기가 실패했다'는 뜻이 아니다. 서버가 갱신을 커밋해 놓고 응답만
        늦게 오면 드라이버는 똑같이 `NetworkTimeout`을 던진다 — 이 경로에서 특히 그렇다:
        `find_one_and_update`가 `ReturnDocument.AFTER`라 방금 쓴 문서를 통째로 되받는데,
        pymongo는 응답 **전체**에 대해 마감 시각을 한 번만 잡는다
        (`network_layer.receive_message`). 즉 커밋은 끝났는데 되받는 도중에 60초를
        넘기는 일이 실제로 가능하다.

        그러면 원고는 DB에 멀쩡히 있는데 사용자에게만 '생성 실패'로 보인다. 5분과
        LLM·이미지 비용을 쓴 결과를 그렇게 잃지 않는다.

        되읽기 자체가 또 실패하면 **모른다고 답한다**(False). 그때는 원래 오류를 그대로
        올리는 쪽이 맞다 — 확인하지 못한 것을 성공이라고 말하지 않는다.
        """
        try:
            current = await self._repository.find_by_post_id(post_id)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "원고 저장 확인 실패 | %s - %s: %s",
                short(post_id),
                type(error).__name__,
                error,
            )
            return False
        if current is None or current.final_post is None:
            return False
        logger.info(
            "원고 저장 실패로 보였지만 실제로는 저장되어 있었습니다 | %s", short(post_id)
        )
        return True

    async def _save_draft_generation_result(
        self, post_id: str, result: DraftGenerationResult
    ) -> None:
        try:
            await self._store_generation_result(post_id, result)
            return
        except BlogTaskError as error:
            if error.code != "INVALID_STATUS_TRANSITION":
                raise

            current = await self._repository.find_by_post_id(post_id)
            if current is None:
                raise

            if current.final_post is not None:
                logger.info(
                    "원고 생성 완료 무시 | %s - 이미 최종 원고가 저장되어 있습니다",
                    short(post_id),
                )
                return

            # 완성된 원고를 들고 있는데 저장할 자리가 없다. 아직 이 글에 최종 원고가
            # 없다면(위에서 확인) 버리는 것보다 저장하는 편이 언제나 낫다 — 4분과 이미지
            # 생성 비용이 이미 지불됐고, 사용자는 그 결과를 기다리고 있다.
            #
            # 여기 오는 두 경우:
            # - FAILED: 이 실행 중 다른 오류로 실패 처리됐다.
            # - INTENT_SELECTED: 서버가 재시작해 복구 스위퍼가 되돌렸다
            #   (modules/blog_task/recovery.py). 그 상태는 '아무도 돌리고 있지 않다'는
            #   뜻이므로, 이 결과와 경합할 다른 실행도 없다.
            # 그 밖의 상태(POSTING 등)는 사용자가 다른 일을 시작한 것이라 건드리지 않는다.
            if current.status in (
                BlogTaskStatus.FAILED,
                BlogTaskStatus.INTENT_SELECTED,
            ):
                logger.warning(
                    "원고 생성 성공 결과 복구 | %s - 상태가 %s였지만 finalPost가 없어 결과를 저장합니다",
                    short(post_id),
                    current.status.value,
                )
                await self._repository.rewind_status(
                    post_id, BlogTaskStatus.GENERATING, DRAFT_GENERATION_ACTOR
                )
                await self._store_generation_result(post_id, result)
                return

            logger.info(
                "원고 생성 완료 무시 | %s - 상태가 %s로 바뀌었습니다",
                short(post_id),
                current.status.value,
            )

    async def _mark_generation_failed(self, post_id: str) -> None:
        current = await self._repository.find_by_post_id(post_id)
        if current is None:
            return
        if current.status == BlogTaskStatus.FAILED:
            logger.info("원고 생성 실패 처리 생략 | %s - 이미 FAILED 상태입니다", short(post_id))
            return
        if current.status != BlogTaskStatus.GENERATING:
            logger.info(
                "원고 생성 실패 처리 생략 | %s - 상태가 %s로 바뀌었습니다",
                short(post_id),
                current.status.value,
            )
            return

        try:
            await self._repository.transition_status(
                post_id, BlogTaskStatus.FAILED, DRAFT_GENERATION_ACTOR
            )
        except BlogTaskError as error:
            latest = await self._repository.find_by_post_id(post_id)
            if latest and latest.status == BlogTaskStatus.FAILED:
                logger.info("원고 생성 실패 처리 생략 | %s - 이미 FAILED 상태입니다", short(post_id))
                return
            logger.warning("원고 생성 실패 상태 저장 실패 | %s - %s", short(post_id), error)

    async def _generate_checked_title_plan(self, draft_input: DraftGenerationInput):
        """제목 계획을 만들고 규격을 확인한다. 걸리면 **제목 생성만** 다시 한다.

        원고 품질 검사와 달리 여기서는 재시도가 싸다 — 본문을 아직 쓰지 않았기 때문이다.
        두 번 다 규격을 못 지키면 None을 돌려주고, 원고는 예전처럼 제목을 직접 짓는다.
        제목 하나 때문에 글 생성 자체를 실패시키지는 않는다.
        """
        generate = getattr(self._draft_generator, "generate_title_plan", None)
        if generate is None:
            return None

        fixed_title = draft_input.trend_title
        for attempt in range(1, TITLE_PLAN_ATTEMPTS + 1):
            try:
                with perf.span(f"title_plan_llm_attempt_{attempt}"):
                    plan = await generate(draft_input)
            except Exception as error:
                logger.warning(
                    "제목 계획 생성 실패 (%d/%d) | %s - %s",
                    attempt,
                    TITLE_PLAN_ATTEMPTS,
                    short(draft_input.post_id),
                    error,
                )
                continue
            if plan is None:
                continue

            problems = check_title_plan(plan, fixed_title=fixed_title)
            if not problems:
                logger.info(
                    "제목 확정 | %s - '%s' (키워드 '%s', 전략 %s)",
                    short(draft_input.post_id),
                    plan.primary_title,
                    plan.primary_keyword,
                    plan.title_strategy,
                )
                return plan

            logger.warning(
                "제목 계획 규격 위반 (%d/%d) | %s - %s",
                attempt,
                TITLE_PLAN_ATTEMPTS,
                short(draft_input.post_id),
                ", ".join(problems),
            )

        logger.info(
            "제목 계획 없이 진행 | %s - 원고가 제목을 직접 만든다", short(draft_input.post_id)
        )
        return None

    async def _ensure_title_plan(self, task: BlogTask, draft_input: DraftGenerationInput):
        """이 글의 확정 제목을 가져온다. 없으면 만들어 저장한다.

        **반드시 저장을 거쳐야 한다.** 선행 생성과 실제 생성이 각각 제목을 새로 만들면 두
        경로의 콘텐츠 설계 캐시 키가 어긋나(제목이 키에 들어간다) 선행 생성이 통째로 헛돌고
        설계 LLM이 두 번 돈다. 그래서 이미 있으면 그것을 쓰고, 없을 때만 만든다.
        """
        if task.title_plan is not None:
            return task.title_plan
        if getattr(self._draft_generator, "generate_title_plan", None) is None:
            return None

        # 선행 생성이 방금 저장했는데 이 task 스냅샷이 그보다 오래됐을 수 있다. LLM을 한 번
        # 더 부르는 것보다 DB를 한 번 더 읽는 쪽이 훨씬 싸다.
        current = await self._repository.find_by_post_id(task.post_id)
        if current is not None and current.title_plan is not None:
            return current.title_plan

        inflight = self._title_plan_inflight.get(task.post_id)
        if inflight is None:
            inflight = asyncio.create_task(self._generate_checked_title_plan(draft_input))
            self._title_plan_inflight[task.post_id] = inflight
            inflight.add_done_callback(
                lambda _t: self._title_plan_inflight.pop(task.post_id, None)
            )
        plan = await inflight
        if plan is None:
            return None

        try:
            await self._repository.save_title_plan(task.post_id, plan)
        except Exception as error:
            # 저장에 실패해도 이번 생성에는 계획을 쓴다. 다음 호출이 다시 만들 뿐이다.
            logger.warning("제목 계획 저장 실패 | %s - %s", short(task.post_id), error)
        return plan

    async def _with_title_plan(
        self, draft_input: DraftGenerationInput, task: BlogTask
    ) -> DraftGenerationInput:
        """확정 제목을 원고 입력에 실어 준다. 콘텐츠 설계 캐시 키를 계산하기 **전에**
        불러야 한다 — 키가 제목·핵심 검색 구문·전략을 포함하기 때문이다."""
        try:
            plan = await self._ensure_title_plan(task, draft_input)
        except Exception as error:
            logger.warning("제목 계획 준비 실패 | %s - %s", short(task.post_id), error)
            return draft_input
        if plan is None:
            return draft_input
        return draft_input.model_copy(update={"title_plan": plan})

    async def _with_reference_evidence(
        self, draft_input: DraftGenerationInput
    ) -> DraftGenerationInput:
        """참고자료 근거 프로필을 만들어 입력에 싣는다.

        코드가 뼈대를 만들고(무엇이 몇 장 있고, 사용자가 경험을 적었는가), 어댑터가 지원하면
        모델이 대상·브랜드·확인된 특징을 채운다. **모델 호출이 실패해도 프로필은 항상
        있다** — 참고 이미지 매핑과 경험 판정은 프로필 없이는 돌아가지 않기 때문이다.
        """
        profile = build_profile(
            draft_input.input.reference_materials,
            draft_input.selected_intent.sources,
            topic=draft_input.input.topic,
        )
        generate = getattr(self._draft_generator, "generate_reference_evidence", None)
        # 참고자료가 하나도 없으면 모델에 물어볼 것이 없다 — 호출 한 번을 아낀다.
        # 다만 사용자가 고른 원본 검색어가 있으면 물어볼 것이 하나 더 있다: 그 검색어가
        # 가리키는 소재가 실제로 무엇인가(일반 명사인가, 프로그램 이름인가).
        if generate is not None and (profile.has_references or draft_input.raw_keywords):
            try:
                # 같은 입력이면 선행 생성(prefetch)이 만든 모델 출력을 그대로 쓴다.
                # 근거 추리는 원고 생성의 첫 관문이라, 캐시가 없으면 재생성·선행 생성이
                # 겹칠 때마다 수십 초짜리 호출이 통째로 다시 돌았다(2026-08-10).
                with perf.span("reference_evidence_llm") as meta:
                    model_profile, cache_hit = await self._with_ttl_cache(
                        self._evidence_cache,
                        self._evidence_inflight,
                        self._evidence_cache_key(draft_input),
                        lambda: generate(draft_input),
                    )
                    meta["cache_hit"] = cache_hit
                profile = enrich(profile, model_profile)
            except Exception as error:
                logger.warning(
                    "참고자료 근거 분석 실패 - 코드 판정만 사용 | %s - %s",
                    short(draft_input.post_id),
                    error,
                )
        logger.info(
            "참고자료 근거 | %s - 대상 '%s', 이미지 %d장, 경험 자료 %s",
            short(draft_input.post_id),
            profile.anchor or "확인 안 됨",
            len(profile.reference_image_roles),
            "있음" if profile.has_user_experience_evidence else "없음",
        )
        return draft_input.model_copy(update={"reference_evidence": profile})

    async def _with_editorial_style(
        self, draft_input: DraftGenerationInput, task: BlogTask
    ) -> DraftGenerationInput:
        """편집 스타일 계획을 확정해 입력에 싣는다.

        모델이 카테고리·형태를 정하고, 코드가 그 위에서 테마·팔레트·레이아웃·예산을
        확정한다(normalize_style_plan). 모델이 없거나 실패해도 계획은 항상 있다 — 없으면
        모든 글이 다시 같은 파란 도표로 돌아간다.
        """
        model_plan = None
        generate = getattr(self._draft_generator, "generate_editorial_style_plan", None)
        if generate is not None:
            try:
                # 모델 출력만 캐시한다. 테마·팔레트·예산 확정(normalize_style_plan)은
                # revision이 섞이므로 매번 다시 계산한다 — 재생성에서 팔레트가 도는
                # 기존 동작이 캐시 때문에 멈추면 안 된다.
                with perf.span("editorial_style_llm") as meta:
                    model_plan, cache_hit = await self._with_ttl_cache(
                        self._style_cache,
                        self._style_inflight,
                        self._style_cache_key(draft_input),
                        lambda: generate(draft_input),
                    )
                    meta["cache_hit"] = cache_hit
            except Exception as error:
                logger.warning(
                    "편집 스타일 계획 생성 실패 - 코드 추정으로 진행 | %s - %s",
                    short(draft_input.post_id),
                    error,
                )
        settings = draft_input.settings
        plan = normalize_style_plan(
            model_plan,
            post_id=draft_input.post_id,
            revision=_generation_revision(task),
            topic=draft_input.input.topic,
            subject=draft_input.input.subject,
            purposes=list(draft_input.input.purpose or draft_input.input.keywords),
            article_length=settings.article_length if settings else "medium",
            evidence=draft_input.reference_evidence,
        )
        logger.info(
            "편집 스타일 | %s - %s / %s, 테마 %s, 썸네일 %s, 도표 상한 %d",
            short(draft_input.post_id),
            plan.content_category,
            plan.editorial_archetype,
            plan.chart_theme,
            plan.thumbnail_layout,
            plan.visual_budget.rendered_visuals_max,
        )
        return draft_input.model_copy(update={"editorial_style": plan})

    async def _ensure_seo_keyword_plan(self, task: BlogTask, draft_input: DraftGenerationInput):
        """이 글의 SEO 키워드 계획을 가져온다. 없으면 만들어 저장한다(제목 계획과 같은 방식).

        같은 글의 재생성에서는 저장된 계획을 재사용해 LLM을 다시 부르지 않는다 —
        title_plan과 같은 업데이트 정책이다. 어댑터가 지원하지 않으면(테스트 스텁·구형
        어댑터) None을 돌려주고, 그러면 원고 프롬프트·검증이 예전과 똑같이 동작한다.
        """
        if task.seo_keyword_plan is not None:
            return task.seo_keyword_plan
        if getattr(self._draft_generator, "generate_seo_keyword_plan", None) is None:
            return None

        # 다른 경로가 방금 저장했을 수 있다. LLM 한 번보다 DB 읽기 한 번이 훨씬 싸다.
        current = await self._repository.find_by_post_id(task.post_id)
        if current is not None and current.seo_keyword_plan is not None:
            return current.seo_keyword_plan

        inflight = self._seo_plan_inflight.get(task.post_id)
        if inflight is None:
            inflight = asyncio.create_task(
                self._draft_generator.generate_seo_keyword_plan(draft_input)
            )
            self._seo_plan_inflight[task.post_id] = inflight
            inflight.add_done_callback(
                lambda _t: self._seo_plan_inflight.pop(task.post_id, None)
            )
        try:
            plan = await inflight
        except Exception as error:
            logger.warning(
                "SEO 키워드 계획 생성 실패 - 계획 없이 진행 | %s - %s",
                short(draft_input.post_id),
                error,
            )
            return None
        if plan is None:
            return None

        logger.info(
            "SEO 키워드 계획 | %s - primary '%s', secondary %d개, avoid %d개",
            short(draft_input.post_id),
            plan.primary,
            len(plan.secondary),
            len(plan.avoid),
        )
        try:
            await self._repository.save_seo_keyword_plan(task.post_id, plan)
        except Exception as error:
            # 저장에 실패해도 이번 생성에는 계획을 쓴다. 다음 호출이 다시 만들 뿐이다.
            logger.warning("SEO 키워드 계획 저장 실패 | %s - %s", short(task.post_id), error)
        return plan

    def _title_anchored_seo_plan(self, plan, draft_input: DraftGenerationInput):
        """SEO 계획의 primary를 이 글의 확정 제목에 맞춘다.

        계획은 DB에 저장해 재사용하므로, 제목이 확정되기 전에 만들어진 계획이 그대로 돌아올
        수 있다(선행 생성에서 제목 계획이 실패한 경우). 그 primary가 확정 제목에 없으면
        생성 후 검증(seo_primary_in_title)이 매번 실패하는데, 제목은 사용자가 M2에서 고른
        값이라 원고를 다시 써도 고쳐지지 않는다 — 그래서 쓰는 시점에 제목 기준으로 맞춘다.
        """
        title = (
            draft_input.title_plan.primary_title
            if draft_input.title_plan
            else (draft_input.trend_title or "")
        )
        aligned = align_seo_plan_with_title(
            plan,
            title,
            draft_input.title_plan.primary_keyword if draft_input.title_plan else None,
        )
        if aligned is not None and plan is not None and aligned.primary != plan.primary:
            logger.info(
                "SEO primary 재정렬 | %s - '%s' → '%s' (확정 제목에 없는 키워드)",
                short(draft_input.post_id),
                plan.primary,
                aligned.primary,
            )
        return aligned

    async def _seo_keyword_plan_with_perf(
        self, task: BlogTask, draft_input: DraftGenerationInput
    ):
        """SEO 계획 한 건. 조기 시작해도 전체 대기 시간이 같은 span에 잡히게 한다."""
        with perf.span("seo_keyword_plan_llm"):
            return await self._ensure_seo_keyword_plan(task, draft_input)

    def _start_seo_keyword_plan(
        self, task: BlogTask, draft_input: DraftGenerationInput
    ) -> asyncio.Task:
        """확정 제목 직후 SEO 계획을 시작한다. 호출자는 반드시 finish를 거친다."""
        return asyncio.create_task(self._seo_keyword_plan_with_perf(task, draft_input))

    async def _finish_seo_keyword_plan(self, task: asyncio.Task) -> None:
        """중간 취소·예외에도 조기 SEO 태스크를 남겨 두지 않는다."""
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _with_content_and_seo_plans(
        self,
        draft_input: DraftGenerationInput,
        task: BlogTask,
        *,
        seo_task: asyncio.Task | None = None,
    ) -> DraftGenerationInput:
        """콘텐츠·SEO 계획을 합친다.

        ``seo_task``가 있으면 제목 직후 이미 시작한 결과를 기다린다. 없으면 기존 호출자와
        테스트를 위해 이 자리에서 시작한다. 어느 경로든 SEO provider 호출은
        ``_ensure_seo_keyword_plan``의 single-flight를 지나 한 번만 돈다.
        """

        async def content_plan():
            with perf.span("content_plan_llm") as meta:
                plan, cache_hit = await self._content_plan_with_cache(draft_input)
                meta["cache_hit"] = cache_hit
                return plan

        owns_seo_task = seo_task is None
        running_seo = seo_task or self._start_seo_keyword_plan(task, draft_input)
        try:
            plan, seo = await asyncio.gather(content_plan(), running_seo)
        finally:
            if owns_seo_task:
                await self._finish_seo_keyword_plan(running_seo)
        seo = self._title_anchored_seo_plan(seo, draft_input)
        updates: dict[str, Any] = {}
        if plan is not None:
            updates["content_plan"] = plan
        if seo is not None:
            updates["seo_keyword_plan"] = seo
        return draft_input.model_copy(update=updates) if updates else draft_input

    async def _generate_content_plan(self, draft_input: DraftGenerationInput):
        """콘텐츠 설계 호출. 어댑터가 설계를 지원하지 않거나(테스트 스텁·구형 어댑터)
        호출이 실패하면 None — 원고는 설계 없이 기존 방식으로 쓴다."""
        generate = getattr(self._draft_generator, "generate_content_plan", None)
        if generate is None:
            return None
        try:
            plan = await generate(draft_input)
        except Exception as error:
            logger.warning(
                "콘텐츠 설계 생성 실패 - 설계 없이 진행 | %s - %s",
                short(draft_input.post_id),
                error,
            )
            return None
        if plan is not None:
            logger.info(
                "콘텐츠 설계 | %s - %s, 섹션 %d개, 시각자료 %s",
                short(draft_input.post_id),
                plan.article_type,
                len(plan.sections),
                [s.visual_type for s in plan.sections if s.visual_type != "NONE"] or "없음",
            )
        return plan

    def _plan_cache_key(self, draft_input: DraftGenerationInput) -> str:
        """콘텐츠 설계 캐시 키. 설계 결과에 실제로 영향을 주는 것만 담는다: 정규화된
        입력(소재·목적·키워드·참고자료), 선택 의도, 출처 집합, 트렌드 제목, 확정 제목 계획,
        사용자 설정(페르소나·분량·결합 방향), 프롬프트 버전. style/format은 설계 프롬프트가
        쓰지 않으므로 넣지 않는다 — 넣으면 선행 생성(스타일을 모르는 시점)과 키가 어긋난다.

        제목 계획이 키에 들어가므로, 선행 생성과 실제 생성은 반드시 **같은** 계획을 봐야
        한다. 그래서 계획은 매번 새로 만들지 않고 DB에 저장해 두고 읽는다
        (_ensure_title_plan)."""
        import hashlib

        blog_input = draft_input.input
        settings = draft_input.settings
        title_plan = draft_input.title_plan
        materials = "|".join(
            f"{m.type.value}:{hashlib.sha256(m.value.encode()).hexdigest()[:16]}"
            for m in blog_input.reference_materials
        )
        raw = "|".join(
            [
                blog_input.topic.strip(),
                (blog_input.subject or "").strip(),
                ",".join(blog_input.purpose or []),
                ",".join(blog_input.keywords),
                materials,
                draft_input.selected_intent.intent_id,
                draft_input.selected_intent.title,
                draft_input.selected_intent.target_reader,
                ",".join(s.url for s in (draft_input.selected_intent.sources or [])),
                draft_input.trend_title or "",
                # 확정 제목이 바뀌면 설계도 달라져야 한다. 제목이 건 약속을 설계가 채우기
                # 때문이다 — 옛 제목으로 만든 설계를 재사용하면 제목과 구조가 어긋난다.
                title_plan.primary_title if title_plan else "",
                title_plan.primary_keyword if title_plan else "",
                title_plan.title_strategy if title_plan else "",
                # 편집 스타일이 설계 프롬프트의 시각자료 허용 목록을 정하므로 키에 들어간다.
                # 빠뜨리면 카테고리가 달라진 재생성이 옛 설계를 그대로 재사용한다.
                style.content_category if (style := draft_input.editorial_style) else "",
                style.editorial_archetype if style else "",
                str(style.visual_budget.rendered_visuals_max) if style else "",
                # 근거가 달라지면 설계도 달라져야 한다(확인된 대상이 섹션 구성을 바꾼다).
                (draft_input.reference_evidence.anchor if draft_input.reference_evidence else ""),
                # 소재 정체도 설계를 바꾼다(영상 콘텐츠는 핵심 포맷이 첫 섹션에 온다).
                # anchor만으로는 갈리지 않으므로 유형·정식명을 따로 넣는다.
                _content_entity_key(draft_input.reference_evidence),
                str(settings.hashtag_count) if settings else "",
                settings.article_length if settings else "",
                settings.blend_mode if settings else "",
                (settings.default_persona or "") if settings else "",
                (settings.custom_persona or "") if settings else "",
                draft_input.prompt_version,
                # 모델·effort·thinking도 키에 들어간다. 이것이 없으면 M4_DRAFT_MODEL을 바꿔도
                # 키가 같아서 **옛 모델이 만든 설계를 그대로 재사용**한다 — 모델을 바꾼 이유가
                # 설계 품질이었다면 그 변경이 아무 효과가 없다. effort·thinking도 같은 이유이고,
                # Anthropic 문서에 따르면 이 둘은 프롬프트에 렌더링되므로 결과가 달라질 수 있다.
                *self._model_fingerprint(),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def _model_fingerprint(self, stage: str = "m4-content-plan") -> list[str]:
        """그 단계 결과를 만든 모델의 지문(모델 ID · effort · thinking).

        어댑터가 노출하지 않으면 빈 문자열을 넣는다 — 목업·구형 어댑터에서도 키가 안정적이어야
        한다(값이 없으면 예전과 같은 키가 된다).
        """
        role = getattr(self._draft_generator, "_role", None)
        model = getattr(role, "model", "") or ""
        try:
            from app.llm.live_adapters import STAGE_BUDGETS

            budget = STAGE_BUDGETS[stage]
            return [str(model), budget.effort, budget.thinking]
        except Exception:
            return [str(model), "", ""]

    async def _content_plan_with_cache(self, draft_input: DraftGenerationInput):
        """(설계, 캐시 적중 여부). 같은 키의 생성이 진행 중이면 그 결과를 기다린다 —
        선행 생성과 실제 생성이 겹쳐도 API 요청은 한 번이다. 실패(None)는 캐시하지
        않는다: 다음 시도가 새로 만든다."""
        if getattr(self._draft_generator, "generate_content_plan", None) is None:
            return None, False
        key = self._plan_cache_key(draft_input)
        cached = self._plan_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1], True

        inflight = self._plan_inflight.get(key)
        joined = inflight is not None
        if inflight is None:
            inflight = asyncio.create_task(self._generate_content_plan(draft_input))
            self._plan_inflight[key] = inflight
            inflight.add_done_callback(lambda _t: self._plan_inflight.pop(key, None))
        plan = await inflight
        if plan is not None:
            if len(self._plan_cache) >= CONTENT_PLAN_CACHE_MAX_ENTRIES:
                self._plan_cache.pop(next(iter(self._plan_cache)))
            self._plan_cache[key] = (
                time.monotonic() + CONTENT_PLAN_CACHE_TTL_SECONDS,
                plan,
            )
        return plan, joined

    async def _with_ttl_cache(
        self,
        cache: dict[str, tuple[float, Any]],
        inflight: dict[str, asyncio.Task],
        key: str,
        factory,
    ) -> tuple[Any, bool]:
        """(값, 캐시 적중·합류 여부). 설계 캐시와 같은 규칙이다: 성공(None 아님)만
        캐시하고, 같은 키의 호출이 진행 중이면 새 요청 대신 그 결과를 기다린다.
        실패는 캐시하지 않고 그대로 전파한다 — 호출부의 기존 폴백이 처리한다."""
        cached = cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1], True
        task = inflight.get(key)
        joined = task is not None
        if task is None:
            task = asyncio.create_task(factory())
            inflight[key] = task
            task.add_done_callback(lambda _t: inflight.pop(key, None))
        value = await task
        if value is not None:
            if len(cache) >= CONTENT_PLAN_CACHE_MAX_ENTRIES:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic() + CONTENT_PLAN_CACHE_TTL_SECONDS, value)
        return value, joined

    def _evidence_cache_key(self, draft_input: DraftGenerationInput) -> str:
        """근거 추리 모델 출력의 캐시 키. 근거를 정하는 입력만 담는다: 소재·부제·목적·
        키워드·참고자료·출처·원본 검색어·프롬프트 버전·모델 지문. 제목·스타일은 근거
        추리보다 뒤에 정해지므로 넣지 않는다(넣으면 선행 생성과 키가 어긋난다)."""
        import hashlib

        blog_input = draft_input.input
        materials = "|".join(
            f"{m.type.value}:{hashlib.sha256(m.value.encode()).hexdigest()[:16]}"
            for m in blog_input.reference_materials
        )
        raw = "|".join(
            [
                blog_input.topic.strip(),
                (blog_input.subject or "").strip(),
                ",".join(blog_input.purpose or []),
                ",".join(blog_input.keywords),
                materials,
                draft_input.selected_intent.intent_id,
                ",".join(s.url for s in (draft_input.selected_intent.sources or [])),
                ",".join(draft_input.raw_keywords or []),
                draft_input.prompt_version,
                *self._model_fingerprint("m4-reference-evidence"),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def _style_cache_key(self, draft_input: DraftGenerationInput) -> str:
        """편집 스타일 모델 출력의 캐시 키. 스타일 판단에 들어가는 입력: 소재·부제·목적·
        키워드·근거(anchor·소재 정체)·분량·페르소나·프롬프트 버전·모델 지문."""
        import hashlib

        blog_input = draft_input.input
        settings = draft_input.settings
        evidence = draft_input.reference_evidence
        raw = "|".join(
            [
                blog_input.topic.strip(),
                (blog_input.subject or "").strip(),
                ",".join(blog_input.purpose or []),
                ",".join(blog_input.keywords),
                (evidence.anchor if evidence else "") or "",
                _content_entity_key(evidence),
                settings.article_length if settings else "",
                (settings.default_persona or "") if settings else "",
                (settings.custom_persona or "") if settings else "",
                draft_input.prompt_version,
                *self._model_fingerprint("m4-editorial-style"),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def start_content_plan_prefetch(self, task: BlogTask) -> None:
        """의도 선택 직후 콘텐츠 설계를 미리 만들어 둔다.

        설계는 입력·의도·출처·설정에만 의존하고 원고 생성 요청의 style/format을 쓰지
        않으므로, 의도가 정해진 순간 만들 수 있다. 사용자가 '원고 생성'을 누르는 시점에는
        설계가 캐시에 있거나 진행 중이라, 첫 단계의 수십 초가 체감에서 사라진다."""
        if task.selected_intent is None:
            return
        # 제목·콘텐츠 설계·SEO 계획 중 하나라도 지원하면 선행할 값이 있다.
        supports_plan = getattr(self._draft_generator, "generate_content_plan", None) is not None
        supports_title = getattr(self._draft_generator, "generate_title_plan", None) is not None
        supports_seo = (
            getattr(self._draft_generator, "generate_seo_keyword_plan", None) is not None
        )
        if not supports_plan and not supports_title and not supports_seo:
            return
        self._jobs.start(self._prefetch_content_plan(task))

    async def _prefetch_content_plan(self, task: BlogTask) -> None:
        try:
            draft_input = await self._build_draft_input(task, style=None, format_=DraftFormat.HTML)
            # 실제 생성과 같은 순서로 선행한다 — 근거·제목·편집 스타일이 모두 설계 캐시 키에
            # 들어가므로, 하나라도 빠지면 키가 어긋나 선행 생성이 통째로 헛돈다.
            with perf.span("reference_evidence_prefetch"):
                draft_input = await self._with_reference_evidence(draft_input)
            with perf.span("title_plan_prefetch"):
                draft_input = await self._with_title_plan(draft_input, task)
            seo_task = self._start_seo_keyword_plan(task, draft_input)
            try:
                with perf.span("editorial_style_prefetch"):
                    draft_input = await self._with_editorial_style(draft_input, task)
                # 콘텐츠 설계는 편집 스타일이 정한 시각 예산을 받은 뒤 시작한다. SEO는 이미
                # 제목 직후부터 돌고 있어 이 구간에서는 그 같은 결과를 합치기만 한다.
                with perf.span("generation_plans_prefetch"):
                    await self._with_content_and_seo_plans(
                        draft_input, task, seo_task=seo_task
                    )
            finally:
                await self._finish_seo_keyword_plan(seo_task)
        except Exception as error:
            # 선행 생성은 가속 장치일 뿐이다. 실패해도 원고 생성이 어차피 다시 만든다.
            logger.info("콘텐츠 설계 선행 생성 실패(무시) | %s - %s", short(task.post_id), error)

    def _with_rendered_visuals(self, result, draft_input: DraftGenerationInput):
        """(폴백 경로) 모델이 반환한 시각자료 데이터를 검증·렌더링해 [[VISUAL:]] 마커
        자리에 넣는다.

        검증(그래프는 실측 수치·출처 필수)에 탈락한 자료는 그리지 않고 마커만 걷어낸다 —
        근거 없는 그래프보다 없는 편이 낫다. 총량은 글 길이가 아니라 전체 이미지 예산
        (썸네일 포함 6장)으로만 자르고, 렌더링된 이미지는 images 목록에도 넣어 편집·발행
        경로가 추적하게 한다. 카드 계획이 성공한 글은 이 메서드 대신
        _with_card_images가 예산·순번까지 관리한다."""
        final_post = result.final_post
        visuals = result.visuals or []

        existing = _images_from_post(final_post)
        # 표·그래프는 **사진 장수 규격과 별개**다(2026-08-03 사용자 결정). 카드 경로는
        # 이미 그렇게 동작하는데(select_cards가 도표를 예산에서 뺀다) 폴백만 사진 규격으로
        # 도표를 잘라, 같은 글이 경로에 따라 다른 밀도로 나갔다. 여기 남는 상한은 폭주
        # 방지선(MAX_TOTAL_IMAGES)뿐이고, 무엇을 그릴지는 근거 게이트가 이미 정했다.
        budget = max(0, MAX_TOTAL_IMAGES - len(existing))

        html_by_id: dict[str, str] = {}
        markdown_by_id: dict[str, str] = {}
        rendered: list[GeneratedPostImage] = []
        for visual in visuals:
            if len(rendered) >= budget:
                logger.info(
                    "시각자료 %s 제외: 이미지 폭주 방지선 %d장 초과",
                    visual.visual_id,
                    MAX_TOTAL_IMAGES,
                )
                continue
            # 출처(source-N) 해석·실측값 대조는 생성 직후(_generate_checked_draft)에서
            # 한 번만 한다 — 여기서 또 하면 이미 실제 출처명으로 해석된 차트가 source-N
            # 형식이 아니라는 이유로 전부 탈락한다.
            image = render_planned_visual(visual)
            if image is None:
                continue
            rendered.append(image)
            html_by_id[visual.visual_id] = visual_html(image)
            markdown_by_id[visual.visual_id] = visual_markdown(image)

        # 마커는 렌더링 성공 여부와 무관하게 전부 처리한다 — 남은 마커가 발행 글에 글자
        # 그대로 찍히면 안 된다. body는 텍스트라 마커를 걷어내기만 한다.
        updated = final_post.model_copy(
            update={
                "body": replace_visual_markers(final_post.body, {}),
                "html_content": replace_visual_markers(final_post.html_content, html_by_id),
                "markdown_content": (
                    replace_visual_markers(final_post.markdown_content, markdown_by_id)
                    if final_post.markdown_content
                    else final_post.markdown_content
                ),
            }
        )
        if rendered:
            all_images = dedupe_images([*existing, *rendered])
            updated = updated.model_copy(
                update={
                    "images": all_images,
                    "featured_image": all_images[0] if all_images else None,
                }
            )
        return result.model_copy(update={"final_post": updated})

    def _gated_visuals(self, result, draft_input: DraftGenerationInput):
        """목적·근거·중복으로 시각자료를 잘라 낸다. 원고 자체는 반려하지 않는다.

        걸러진 자료의 마커는 배치 단계가 걷어낸다 — 근거 없는 표보다 표가 없는 편이 낫다.
        살아남은 자료는 글 하나의 테마로 통일한다(글끼리 달라야지 자료끼리 다르면 안 된다).
        """
        if not result.visuals:
            return result

        style = draft_input.editorial_style
        policy = purpose_policy(list(draft_input.input.purpose or draft_input.input.keywords))
        # 표·그래프 개수는 글 길이와 무관하다(2026-08-03 사용자 결정) — 스타일 계획이
        # 없으면 목적 정책의 근거 원칙(rendered_max)만 상한이 된다.
        budget = (
            style.visual_budget
            if style
            else VisualBudget(rendered_visuals_max=policy.rendered_max)
        )
        gated = gate_visuals(
            result.visuals,
            policy=policy,
            budget=budget,
            # 중복 판정은 마커 바로 앞 문단과 비교하므로 마크다운 원문을 본다.
            body=result.final_post.markdown_content or result.final_post.body,
            has_user_numeric_data=has_numeric_user_material(
                draft_input.input.reference_materials
            ),
        )
        if gated.rejections:
            logger.info(
                "시각자료 게이트 | %s - %s",
                short(draft_input.post_id),
                "; ".join(gated.rejections),
            )
        kept = apply_visual_theme(gated.kept, style.chart_theme if style else None)
        return result.model_copy(update={"visuals": kept or None})

    async def _generate_checked_draft(self, draft_input: DraftGenerationInput):
        """원고를 받고, 요구사항을 지켰는지 확인한다.

        프롬프트는 본문 길이와 이미지 태그 개수를 시키지만 모델은 그것을 어길 수 있다.
        지금까지는 아무도 확인하지 않았으므로, 900자짜리 원고도 그대로 화면에 올라갔다.
        걸리면 한 번 더 생성한다 — 모델은 같은 프롬프트에도 다른 답을 내놓는다.
        """
        hashtag_count = draft_input.settings.hashtag_count if draft_input.settings else 5
        min_body_chars, _target_max = article_length_targets(draft_input.settings)
        # 검사 상한은 프롬프트 목표 상한이 아니라 '통과 상한'이다. 목표(중간 1,800~2,300자)는
        # 모델이 겨냥할 지점이고, 그것을 조금 넘겼다고 원고를 다시 쓰게 하면 정보를 덜어내는
        # 재작성이 되어 대개 더 나빠진다. 2026-08-05 사용자 결정으로 중간 글은 3,000자까지
        # 그대로 통과시킨다.
        max_body_chars = article_length_pass_max(draft_input.settings)
        has_experience_material = _has_explicit_experience_material(draft_input)
        # 마지막 시도의 SEO·콘텐츠 검증 결과. 수용/최종 반려 시점에 로그로 남긴다.
        validation = None
        # 이미 규격을 통과한 원고. 길이를 다듬으려고 한 번 더 부를 때만 채워진다.
        # 그 재시도는 **선택**이므로, 실패하면 이것으로 돌아간다(아래 참고).
        accepted: tuple[Any, Any] | None = None

        # 재시도는 같은 생성 실행 안의 같은 글을 다듬는 일이다. 그 사이 비교 대상인 기존
        # 발행 글은 바뀌지 않는 하나의 스냅샷으로 보는 편이 결정적이고, 원격 DB 조회도 한
        # 번이면 충분하다. 조회 실패는 _published_digests가 빈 목록으로 낮춘다.
        published_digests = await self._published_digests(draft_input)

        for attempt in range(1, DRAFT_ATTEMPTS + 1):
            with perf.span(f"draft_llm_attempt_{attempt}") as meta:
                try:
                    result = await self._draft_generator.generate_draft(draft_input)
                except ProviderTruncatedError as error:
                    # 모델이 최대 출력 토큰을 다 쓰고 잘렸다. 실측(2026-08-03): 첫 시도는
                    # 41초에 정상이었는데, 길이를 줄이라는 재시도가 252초 동안 32,000
                    # 토큰을 전부 쓰고 잘렸다.
                    meta["truncated"] = True
                    if accepted is not None:
                        # **완성된 원고를 다듬기 실패로 버리지 않는다.** 이 자리에 온
                        # 원고는 이미 품질 검사를 통과했고, 남은 문제는 '권장 길이를
                        # 넘겼다'는 경고 하나뿐이다. 그 경고 때문에 7분짜리 작업 전체를
                        # 실패로 만드는 것은 사용자에게 아무것도 남기지 않는다.
                        logger.warning(
                            "원고 다듬기가 잘렸습니다 - 직전 원고를 그대로 씁니다 | %s - %s",
                            short(draft_input.post_id),
                            error,
                        )
                        kept, kept_validation = accepted
                        write_validation_log(
                            kept_validation,
                            draft_input.post_id,
                            draft_input.user_id,
                            CONTENT_VALIDATION_VERSION,
                        )
                        return kept
                    if attempt < DRAFT_ATTEMPTS:
                        # 돌아갈 원고가 없다. 잘림은 예산 문제라 같은 요청이라도 다음
                        # 번에는 통과할 수 있으므로 남은 시도를 쓴다.
                        logger.warning(
                            "원고 생성이 잘렸습니다 (%d/%d) - 다시 시도합니다 | %s - %s",
                            attempt,
                            DRAFT_ATTEMPTS,
                            short(draft_input.post_id),
                            error,
                        )
                        continue
                    raise
                meta["body_chars"] = body_char_count(result.final_post.body or "")

            # 시각자료를 **검사보다 먼저** 거른다. 출처 대조와 목적별 게이트를 통과한 것만
            # 남겨야 품질 검사·콘텐츠 검증·배치가 모두 같은 목록을 본다 — 예전에는 검증이
            # 곧 버려질 자료를 보고 반려 사유를 만들어, 멀쩡한 원고를 다시 쓰게 했다.
            if result.visuals:
                verified = []
                for visual in result.visuals:
                    resolved = _verified_visual(visual, draft_input)
                    if resolved is None:
                        logger.info(
                            "시각자료 %s 제외: 검증 출처와 수치 불일치", visual.visual_id
                        )
                        continue
                    verified.append(resolved)
                result = result.model_copy(update={"visuals": verified or None})
                result = self._gated_visuals(result, draft_input)

            with perf.span(f"draft_quality_check_{attempt}") as check_meta:
                report = check_draft(
                    result.final_post,
                    hashtag_count,
                    min_body_chars=min_body_chars,
                    max_body_chars=max_body_chars,
                    trend_title=draft_input.trend_title,
                    trend_keyword=(
                        draft_input.title_plan.primary_keyword
                        if draft_input.title_plan
                        else (
                            draft_input.seo_keyword_plan.primary
                            if draft_input.seo_keyword_plan
                            else None
                        )
                    ),
                    has_experience_material=has_experience_material,
                    # 제목이 원고보다 먼저 확정된 글이면, 원고가 그 제목을 그대로 썼는지
                    # 확인한다(파싱이 이미 강제하므로 안전망이다).
                    final_title=(
                        draft_input.title_plan.primary_title if draft_input.title_plan else None
                    ),
                    # 본문 사진은 원고가 아니라 원고 완성 후의 카드 계획이 정한다 — 원고에는
                    # [[IMAGE:]] 태그가 없어야 정상이다(프롬프트도 금지한다).
                    photo_count=0,
                )
                check_meta["passed"] = report.ok
                # 재생성 원인 집계용: 어떤 검사가 원고를 반려시키는지 로그로 세면
                # 프롬프트·스키마에서 그 원인을 미리 막을 수 있다(첫 시도 성공률 개선).
                check_meta["problems"] = len(report.problems)
                check_meta["warnings"] = len(report.warnings)

            # SEO·콘텐츠 검증(생성 후). WARN은 로그로만 수집하고, 확정 제목에서 Primary가
            # 빠진 SEO 계약 위반만 기존 재생성 루프가 처리하도록 report.problems에 더한다.
            # 나머지 품질 WARN은 report에 섞지 않아 재생성/반려에 영향을 주지 않는다(§14).
            with perf.span(f"content_validation_{attempt}") as validation_meta:
                validation = run_content_validations(
                    result.final_post,
                    draft_input.seo_keyword_plan,
                    draft_input.selected_intent.sources,
                    draft_input.input.reference_materials,
                    visuals=result.visuals,
                    purposes=list(
                        draft_input.input.purpose or draft_input.input.keywords
                    ),
                    evidence=draft_input.reference_evidence,
                    # 제목이 확정된 글은 원고가 제목을 바꿀 수 없다. 제목 때문에 걸리는
                    # 검사를 반려 사유로 삼으면 재생성이 영원히 같은 곳에서 걸린다.
                    title_locked=bool(
                        draft_input.title_plan or (draft_input.trend_title or "").strip()
                    ),
                    # 사용자가 고른 원본 검색어. 그것을 하나의 명사처럼 쓴 문장을 잡는다.
                    raw_keywords=list(draft_input.raw_keywords),
                    # 이미 만들어 둔 내 글들. 자동 생성의 위험은 한 편의 품질이 아니라
                    # 쌓였을 때의 닮음인데, 한 편씩 보는 다른 검사는 그것을 못 잡는다.
                    published=published_digests,
                )
                validation_meta["status"] = validation.status
            # SEO Primary FAIL은 '다음 시도에서 고치라'는 재생성 신호이지, 원고를 통째로 버릴
            # 사유는 아니다. 마지막 시도에서는 report에 섞지 않는다 — 본문 자체(check_draft)가
            # 멀쩡한데 Primary 키워드가 제목에 없다는 이유만으로 원고 생성을 영구 실패시키면,
            # 사용자는 결과물 없이 막힌다(기존엔 나왔을 원고다). 미충족은 아래
            # write_validation_log로 남아 승격 판단 근거로 쓰인다(§14: 품질 신호는 수집하되
            # 생성을 막지 않는다).
            if attempt < DRAFT_ATTEMPTS:
                report.problems.extend(validation.fail_messages())

            if report.warnings:
                # 코드가 감당하는 것들이다. 원고를 버리지는 않지만, 모델이 프롬프트를
                # 얼마나 지키는지는 알아야 한다.
                logger.info(
                    "원고 확인 | %s - %s", short(draft_input.post_id), ", ".join(report.warnings)
                )

            if report.ok:
                length_warnings = [
                    warning for warning in report.warnings if "권장 최대" in warning
                ]
                revision_warnings = [
                    warning
                    for warning in report.warnings
                    if "권장 최대" in warning or "선택한 트렌드" in warning
                ]
                # 통과 상한(중간 3,000자)을 넘긴 원고만 직전 마크다운을 함께 주고 한 번
                # 다듬는다. 예전에는 목표 상한(2,300자)의 125%를 기준으로 삼았는데, 그 값과
                # 검사 상한이 각각 움직여 "경고는 났지만 다듬지는 않는" 구간이 생겼다.
                # 이제 기준은 하나다 — 통과 상한을 넘으면 다듬고, 그 아래는 그대로 쓴다.
                # 같은 검사에서 트렌드 누락도 확인됐다면 그 사유까지 함께 줘, 한 번뿐인
                # 재작성으로 두 문제를 모두 고치게 한다.
                # 계획·근거·스타일을 결과에 남긴다. 저장되므로 새로고침해도 같은 디자인이고,
                # 어떤 근거로 무엇을 만들었는지 나중에 확인할 수 있다.
                result = result.model_copy(
                    update={
                        "editorial_style_plan": draft_input.editorial_style,
                        "reference_evidence_profile": draft_input.reference_evidence,
                    }
                )
                if length_warnings and attempt < DRAFT_ATTEMPTS:
                    logger.info(
                        "원고 조정 재시도 (다음 시도 %d/%d) | %s - %s",
                        attempt + 1,
                        DRAFT_ATTEMPTS,
                        short(draft_input.post_id),
                        ", ".join(revision_warnings),
                    )
                    # 이 원고는 이미 쓸 수 있다 — 다듬기가 실패하면 여기로 돌아온다.
                    # 완성본을 손에 쥔 채로 재시도해야 다듬기가 '더 좋아질 기회'가 되고,
                    # '전부 잃을 위험'이 되지 않는다.
                    accepted = (result, validation)
                    draft_input = draft_input.model_copy(
                        update={
                            "revision_notes": revision_warnings,
                            "previous_draft": result.final_post,
                        }
                    )
                    continue
                # 원고 수용: SEO·콘텐츠 검증 결과와 WARN을 로그·통계로 남긴다(§12·§13).
                write_validation_log(
                    validation,
                    draft_input.post_id,
                    draft_input.user_id,
                    CONTENT_VALIDATION_VERSION,
                )
                return result

            # 마지막 시도가 아니면 자동 재시도로 이어지는 정상 흐름이다 — 다음 줄이 바로
            # revision_notes를 채워 재시도를 준비한다. WARNING은 재시도까지 실패해 사용자가
            # 실제로 결과를 못 받는 마지막 시도에만 남긴다.
            log = logger.warning if attempt >= DRAFT_ATTEMPTS else logger.info
            log(
                "원고 품질 검사 실패 (%d/%d) | %s - %s",
                attempt,
                DRAFT_ATTEMPTS,
                short(draft_input.post_id),
                report,
            )

            # 전체를 무턱대고 다시 생성하지 않는다. 무엇이 왜 걸렸는지를 다음 시도 프롬프트에
            # 넣어, 모델이 기존 사실·구성을 유지한 채 그 문제만 고치게 한다. 코드가 이미
            # 감당하는 경고까지 섞으면 고칠 범위가 불필요하게 넓어지므로 실제 반려 사유만 준다.
            draft_input = draft_input.model_copy(
                update={
                    "revision_notes": list(report.problems),
                    "previous_draft": result.final_post,
                }
            )

        # 두 번 다 어겼다. 짧거나 낚시성인 원고를 조용히 내주느니 실패라고 말한다.
        # SEO 필수 FAIL로 반려된 경우를 포함해 마지막 검증 결과를 남긴다(반려율 집계).
        if validation is not None:
            write_validation_log(
                validation,
                draft_input.post_id,
                draft_input.user_id,
                CONTENT_VALIDATION_VERSION,
            )
        raise BlogTaskError("LLM_RESPONSE_INVALID", f"원고 품질 검사를 통과하지 못했습니다: {report}")

    def _with_closing(self, result, draft_input: DraftGenerationInput):
        """브랜드 삽화를 본문에 넣고, 맨 끝에 마무리를 붙인다.

        모델을 부르지 않으므로 실패할 것이 없다 — 문구는 글을 저장할 때 이미 베껴 둔
        값이고(``BlogTaskInput.brand_closing``), 그림은 글의 참고자료 안에서 찾는다.

        **순서가 있다.** 삽화를 먼저 넣고 마무리를 뒤에 붙인다: 마무리는 글의 맨 끝에
        붙는 블록이라, 그 뒤에 삽화를 끼우면 안내 아래에 그림이 떨어진다.
        """
        post = getattr(result, "final_post", None)
        if post is None:
            return result
        updated = insert_brand_art(
            post,
            draft_input.input.reference_materials,
            post_id=draft_input.post_id,
            closing=draft_input.input.brand_closing,
        )
        updated = append_closing(
            updated,
            draft_input.input.brand_closing,
            draft_input.input.reference_materials,
        )
        return result if updated is post else result.model_copy(update={"final_post": updated})

    async def update_draft_text(self, post_id: str, raw_body: Any) -> BlogTask:
        """사용자가 미리보기에서 원고를 손수 다시 썼다.

        모델은 호출하지 않는다: 원하는 바를 직접 말했으니 해석할 게 없다. 이미지는 그대로
        두고 HTML을 새 텍스트에 맞춰 다시 세운다.
        """
        task = await self._require_ready_draft_task(post_id)
        title, html = validate_update_draft_request(raw_body)

        updated = _rebuilt_from_html(task.final_post, title, html)
        result = task.draft_generation_result.model_copy(
            update={"final_post": updated, "generated_at": _now()}
        )
        return await self._repository.update_final_post(post_id, result, DRAFT_EDIT_ACTOR)

    async def _generate_thumbnail_with_subject_fallback(
        self,
        named_subject: NamedSubject | None,
        generate,
    ) -> GeneratedPostImage | None:
        """named_subject를 실은 썸네일 생성이 실패하면 대상 이름 없이 한 번 더.

        실존 인물(감독·아이돌 등)이 소재인 글은 이미지 provider가 프롬프트 단계에서
        거절한다(moderation_blocked). 이름을 유지한 재시도는 반드시 같은 이유로 죽으므로,
        여기서만 프롬프트를 바꾼다. 이름을 잃은 일반 썸네일이 아쉬워도, 썸네일 한 장
        때문에 이미 완성된 본문 전체를 버리는 것보다 낫다.

        **안전 차단은 여기서 끝난다.** 예전에는 '이름 없는 시도'의 안전 차단을 그대로
        전파했는데, 그 예외가 원고 생성 전체를 FAILED로 만들었다(2026-08-10 새벽,
        스파이더맨 글 4연속 실패 — 본문이 다 있는데 대표 이미지 한 장이 글을 죽였다).
        같은 프롬프트의 재시도는 반드시 같은 이유로 죽으므로, 이름 없는 시도까지
        차단되면 None을 돌려주고 호출부가 대표 이미지 없이 글을 완성한다.

        안전 차단이 **아닌** 실패(혼잡·타임아웃)는 그대로 전파한다 — 원고는 FAILED가
        되지만 본문 저장점이 있어 '다시 생성하기'가 이미지 단계부터 싸게 재개하고,
        일시 장애가 걷히면 대표 이미지가 있는 완성본을 얻는다.
        """
        if named_subject is not None and _identity_safety_blocked(named_subject.identity):
            # 이 이름은 이미 이 프로세스에서 차단됐다(계획 썸네일·본문 카드 어디서든).
            # 이름 유지 폴백은 반드시 또 죽으므로(실측 149초 낭비) 바로 이름 없이 간다.
            logger.info(
                "썸네일 폴백: 안전 차단 이력이 있는 대상(%s) - 이름 없이 바로 생성합니다",
                named_subject.identity,
            )
            named_subject = None
        if named_subject is not None:
            try:
                return await generate(named_subject)
            except Exception as error:
                if not _is_image_safety_block(error):
                    raise
                _remember_blocked_identity(named_subject.identity)
                logger.warning(
                    "썸네일 프롬프트가 이미지 안전 시스템에 차단됨 - 고유 대상(%s) 없이 다시 시도합니다",
                    named_subject.identity,
                )
        try:
            return await generate(None)
        except Exception as error:
            if not _is_image_safety_block(error):
                raise
            logger.warning(
                "이름 없는 썸네일 프롬프트도 안전 시스템에 차단됨 - 대표 이미지 없이 원고를 완성합니다 | %s",
                error,
            )
            return None

    async def _generate_image(
        self,
        task: BlogTask,
        draft_input: DraftGenerationInput,
        final_post: FinalPost,
        index: int,
        visual_style: str,
        total_images: int,
        content_prompt: str | None = None,
        content_alt: str | None = None,
        is_thumbnail: bool = False,
        thumbnail_copy: list[str] | None = None,
        reference_image: str | None = None,
        thumbnail_layout=None,
        named_subject: NamedSubject | None = None,
        person_references: list[str] | None = None,
        web_photo: WebPhoto | None = None,
        suppress_subject_identity: bool = False,
    ) -> GeneratedPostImage:
        """계획 없이(또는 계획 실패 후) 만드는 사진 한 장.

        named_subject가 있으면 그 고유 대상 정보가 참고자료 근거보다 먼저다 — 계획 썸네일이
        실패해 이 경로로 내려온 글이 이름 없는 일반 인물 사진으로 조용히 바뀌지 않게 한다.
        인물 확인용 참고 이미지도 함께 넘긴다(URL만 들고 있고 모델에는 안 보내는 일이
        없어야 한다).

        suppress_subject_identity는 '이름을 내려놓은' 안전 폴백 전용이다. 이것이 없으면
        named_subject를 비워도 subject_identity가 근거 anchor로, 확인된 특징이
        fidelity로 다시 채워져 — 프롬프트에는 "The subject is specifically: (그 이름)"이
        그대로 남는다. 스파이더맨 글이 폴백까지 전부 차단돼 원고 자체가 FAILED로 죽던
        실측 원인이다(2026-08-10, 새벽 4연속 실패).
        """
        style = draft_input.editorial_style
        evidence = draft_input.reference_evidence
        return await self._post_image_generator.generate_post_image(
            PostImageGenerationInput(
                post_id=task.post_id,
                user_id=task.user_id,
                input=task.input,
                selected_intent=draft_input.selected_intent,
                final_post=final_post,
                prompt_version=M5_PROMPT_VERSION,
                image_index=index,
                total_images=total_images,
                content_prompt=content_prompt,
                content_alt=content_alt,
                trend_title=draft_input.trend_title,
                is_thumbnail=is_thumbnail,
                visual_style=visual_style,
                thumbnail_copy=thumbnail_copy or [],
                thumbnail_accent_family=(
                    style.accent_family if is_thumbnail and style else None
                ),
                reference_image=reference_image,
                thumbnail_layout=thumbnail_layout,
                photo_language=style.photo_language if style else None,
                subject_identity=(
                    None
                    if suppress_subject_identity
                    else (
                        (named_subject.identity if named_subject else None)
                        or _anchor_unless_blocked(evidence)
                    )
                ),
                fidelity_requirements=(
                    []
                    if suppress_subject_identity
                    else (list(evidence.confirmed_attributes) if evidence else [])
                ),
                subject_kind=named_subject.kind if named_subject else "NON_PERSON",
                must_show_subject=bool(named_subject and named_subject.must_show),
                identity_confidence=named_subject.confidence if named_subject else 0.0,
                reference_person_images=list(person_references or []),
                web_photo=web_photo,
                preserve_brand_marks=_preserves_brand_marks(evidence, reference_image),
                # 이름을 내려놓은 폴백은 소재 앵커("about: {topic}")도 내려놓는다 —
                # 소재명이 곧 그 이름인 글(스파이더맨)에서 그 줄 하나로 또 차단됐다.
                suppress_topic_anchor=suppress_subject_identity,
            )
        )

    async def _plan_visual_cards(self, result, task: BlogTask, draft_input):
        """원고 완성 후 자연 사진 계획을 만들고 규격(80점 게이트·예산 1~6)으로 선정한다.

        어댑터가 카드 계획을 지원하지 않거나(테스트 스텁·구형 어댑터), 호출이 실패하거나,
        계획이 규격을 통과하지 못하면 None — 썸네일 중심 폴백으로 진행한다.

        계획을 버리는 경우에도 그 계획이 알아낸 고유 대상(캐릭터명·인물명)은 함께 돌려준다.
        호출부가 폴백 썸네일에 그대로 실어, 소재의 정체성이 폴백에서 증발하지 않게 한다."""
        generate = getattr(self._draft_generator, "generate_visual_card_plan", None)
        if generate is None or self._post_image_generator is None:
            return None, None

        final_post = result.final_post
        valid_visuals = renderable_visuals(result.visuals)
        reference_count = len(
            _reference_images(task, final_post.title, draft_input.reference_evidence)
        )
        try:
            plan = await generate(draft_input, final_post, len(valid_visuals), reference_count)
        except Exception as error:
            logger.warning(
                "카드 계획 생성 실패 - 기존 방식으로 진행 | %s - %s",
                short(draft_input.post_id),
                error,
            )
            return None, None
        if plan is None:
            return None, None

        rejections: list[str] = []
        selected = select_cards(
            plan,
            final_post.body,
            valid_visuals,
            reference_count,
            rejections,
            # 사진 장수는 글 길이가 정한다: 짧게 2~3장, 중간 3~5장(썸네일 포함).
            max_total=length_total_image_cap(
                draft_input.settings.article_length if draft_input.settings else "medium"
            ),
        )
        if rejections:
            logger.info(
                "카드 선정 제외 | %s - %s", short(draft_input.post_id), "; ".join(rejections)
            )
        if selected is None:
            return None, plan_named_subject(plan)

        logger.info(
            "카드 계획 | %s - 썸네일 1 + 사진 카드 %d + 표·그래프 %d + 첨부 %d = 총 %d장",
            short(draft_input.post_id),
            len(selected.body_cards),
            len(selected.visuals),
            selected.reference_count,
            selected.total,
        )
        return (plan, selected), None

    async def _generate_card_scene(
        self,
        task: BlogTask,
        draft_input: DraftGenerationInput,
        final_post: FinalPost,
        brief,
        design,
        index: int,
        total: int,
        is_thumbnail: bool,
        reference_image: str | None = None,
        thumbnail_layout=None,
        person_references: list[str] | None = None,
        web_photo: WebPhoto | None = None,
    ) -> GeneratedPostImage | None:
        """사진 계획 한 장을 생성한다. provider 재시도 뒤에도 실패하면 None으로 두고,
        호출부가 해당 본문 사진만 제외한다.

        web_photo가 있으면 어댑터가 생성 호출을 건너뛰고 그 사진을 결과로 쓴다.

        reference_image가 있으면 그 참고 이미지를 시각 기준으로 삼아 image-to-image로
        장면을 생성한다(참고 이미지를 닮게).

        고유 인물·캐릭터 카드는 실패했을 때 한 번 더 시도한다 — 배경과 행동을 걷어낸
        단순한 인물 중심 구도로, **같은 인물 정보를 그대로 들고**. 실패를 이름 없는
        일반인 사진으로 메우지 않기 위한 재시도이며, 그 밖의 카드는 예전처럼 한 번만
        호출한다(고비용 요청을 늘리지 않는다)."""
        style = draft_input.editorial_style
        evidence = draft_input.reference_evidence
        person_references = person_references or []
        reuses_reference = _reuses_reference(brief)
        if reuses_reference:
            if not brief.reference_id or not reference_image:
                logger.warning(
                    "REUSED 카드에 검증된 참고 이미지가 없어 제외합니다 | %s - %s",
                    short(task.post_id),
                    brief.card_id,
                )
                return None
            # WebPhoto 경로는 OpenAI 생성/편집 API를 호출하지 않고 로컬 크롭·문구 렌더링만
            # 수행한다. 결과를 받은 뒤 provenance는 사용자 reference로 바로잡는다.
            web_photo = WebPhoto(
                data_url=reference_image,
                source_url=f"user-upload://{brief.reference_id}",
                source_host="user-reference",
                title=brief.alt_text or brief.visual_subject,
                query="사용자 제공 참고 이미지",
                meets_spec=True,
            )
            person_references = []
        named = brief.subject_kind in NAMED_SUBJECT_KINDS and brief.must_show_subject
        # 카드가 확인한 대상이 먼저다(사진마다 다를 수 있다). 없으면 글의 대상.
        # 참고 이미지가 없다는 이유로 캐릭터명·인물명을 지우지 않는다(차단 이력이 있는
        # anchor만 예외 — 실어 보내면 반드시 다시 차단된다).
        subject_identity = brief.subject_identity or _anchor_unless_blocked(evidence)
        if named and not reuses_reference and web_photo is None:
            blocked_before = _identity_safety_blocked(subject_identity)
            # 참고할 실물 근거(웹 사진·사용자 인물 사진·참고 이미지)가 하나도 없으면 이름을
            # 들고 생성하지 않는다(2026-08-10 사용자 지시 "차단 자체를 해결"). 이미지
            # 모델은 실존 인물·저작권 캐릭터를 이름만으로 재현하지 못한다 — 프롬프트에
            # 이름이 있으면 안전 시스템이 입력 단계에서, 결과가 닮으면 출력 단계에서
            # 거절하고(카드당 수십~백여 초), 통과해도 '닮은 남'이 나온다. 이 카드의 정답
            # 경로는 웹 사진이며, 없으면 접는다(이름 잃은 사진으로 메우지 않는 계약
            # 그대로 — 썸네일이라면 호출부의 일반 폴백이 이름 없이 만든다).
            #
            # 근거가 있어도 같은 이름이 이미 차단됐으면(문자열이 같을 때) 재도전하지 않는다.
            if blocked_before or (not person_references and not reference_image):
                logger.info(
                    "카드 %s: %s(%s) - 생성 호출 없이 제외 | %s",
                    brief.card_id,
                    "안전 차단 이력이 있는 대상"
                    if blocked_before
                    else "참고 근거 없는 고유 대상",
                    subject_identity,
                    short(task.post_id),
                )
                return None
        attempts = 1 if reuses_reference else CARD_GENERATION_ATTEMPTS + (1 if named else 0)
        for attempt in range(1, attempts + 1):
            try:
                image = await self._post_image_generator.generate_post_image(
                    PostImageGenerationInput(
                        post_id=task.post_id,
                        user_id=task.user_id,
                        input=task.input,
                        selected_intent=draft_input.selected_intent,
                        final_post=final_post,
                        prompt_version=M5_PROMPT_VERSION,
                        image_index=index,
                        total_images=total,
                        trend_title=draft_input.trend_title,
                        is_thumbnail=is_thumbnail,
                        # 색·광원 방향은 카테고리가 정한다. post_id로 네 팔레트를 돌려
                        # 쓰던 방식은 뷰티 글과 테크 글을 같은 사진으로 수렴시켰다.
                        visual_style=(
                            colour_direction_for(style)
                            if style
                            else visual_style_for(task.post_id)
                        ),
                        # 배치 계획이 실제로 얹을 줄만 넘긴다. 생성기가 두 줄을 받고
                        # 렌더러가 한 줄만 그리면, 화면에 보이는 문구와 저장된 문구가 갈린다.
                        thumbnail_copy=(
                            list(thumbnail_layout.copy_lines)
                            if is_thumbnail and thumbnail_layout is not None
                            else (
                                thumbnail_lines(final_post.thumbnail_copy, final_post.title)
                                if is_thumbnail and final_post.thumbnail_copy
                                else []
                            )
                        ),
                        thumbnail_accent_family=(
                            style.accent_family if is_thumbnail and style else None
                        ),
                        card=brief,
                        design=design,
                        reference_image=None if reuses_reference else reference_image,
                        thumbnail_layout=thumbnail_layout,
                        photo_language=style.photo_language if style else None,
                        subject_identity=subject_identity,
                        fidelity_requirements=(
                            brief.product_fidelity_requirements
                            or (list(evidence.confirmed_attributes) if evidence else [])
                        ),
                        # 고유 대상이면 이미지 프롬프트가 '그 대상 본인을 주요 피사체로'
                        # 규칙으로 바뀐다. 계획이 정하고 코드가 정규화한 값을 그대로 넘긴다.
                        subject_kind=brief.subject_kind,
                        must_show_subject=brief.must_show_subject,
                        identity_confidence=brief.identity_confidence,
                        reference_person_images=person_references,
                        web_photo=web_photo,
                        # 2회차는 배경·행동을 걷어낸 단순 구도. 인물은 그대로다.
                        simplified_identity_retry=attempt > 1,
                        preserve_brand_marks=_preserves_brand_marks(
                            evidence, reference_image
                        ),
                    )
                )
                if reuses_reference:
                    return image.model_copy(
                        update={
                            "provider": "reference",
                            "model": "local-render",
                            "source": "reference",
                            "prompt": "사용자 제공 참고 이미지를 생성 없이 재사용·배치함.",
                            "caption": "사용자 제공 자료",
                            # 사용자가 올린 자료는 외부 웹 출처도 AI 생성물도 아니다.
                            # 어댑터가 WebPhoto 경로로 붙여 둔 external 표시를 지운다 —
                            # 남겨 두면 없는 웹사이트 출처를 표시하게 된다(2026-08-11).
                            "image_source": None,
                        }
                    )
                return image
            except Exception as error:
                safety_blocked = _is_image_safety_block(error)
                if safety_blocked and named:
                    _remember_blocked_identity(subject_identity)
                will_retry = attempt < attempts and not safety_blocked
                logger.warning(
                    "카드 %s 생성 실패 (%d/%d)%s | %s - %s",
                    brief.card_id,
                    attempt,
                    attempts,
                    (
                        " - 같은 인물로 단순 구도 재시도"
                        if named and will_retry
                        else (
                            " - 안전 차단, 같은 이름 재시도 없이 접음"
                            if safety_blocked
                            else ""
                        )
                    ),
                    short(task.post_id),
                    error,
                )
                if safety_blocked:
                    # 안전 차단은 이름 때문이라 같은 이름의 재시도(단순 구도 포함)는
                    # 반드시 같은 이유로 죽는다 — 남은 재시도를 버린다(실측 8분의 원인).
                    # 이름 잃은 사진으로 메우지 않는 계약은 그대로다: 본문 카드는 제외,
                    # 썸네일은 호출부 폴백이 이름 없이 만든다.
                    break
        return None

    async def _usable_web_photos(
        self, draft_input: DraftGenerationInput, photos: list[WebPhoto]
    ) -> list[bool]:
        """웹 검색 사진을 실제로 싣기 전에 그림을 보고 거른다. photos와 같은 길이의
        '써도 되는가' 목록을 돌려준다.

        검색 선정(photo_search)은 픽셀 내용을 못 본다 — 검색 결과 제목·구도·해상도만
        재므로, '닷사이 23' 페이지에 실린 애니 일러스트가 만점으로 통과해 대표 썸네일이
        됐다(2026-08-07 실사례). 그림을 실제로 보는 판정자(OpenAI 2차 검토기)가 있으면
        피사체 불일치·비실사(일러스트·만화)를 거른다.

        판정자가 없거나(구형 배포·스텁) 판정이 실패하면 **모두 통과**다 — 관문 하나가
        사진 없는 글을 만들면 안 된다. 걸러진 자리는 기존 생성 폴백이 그대로 메운다.
        """
        verifier = (
            getattr(self._final_reviewer, "verify_photo_subjects", None)
            if self._final_reviewer is not None
            else None
        )
        if verifier is None or not photos:
            return [True] * len(photos)
        try:
            with perf.span("web_photo_gate") as meta:
                keeps = await verifier(draft_input.input.topic, photos)
                meta["photos"] = len(photos)
                meta["rejected"] = sum(1 for keep in keeps if not keep)
        except Exception as error:  # noqa: BLE001 - 판정 실패가 사진을 잃게 하지 않는다
            logger.warning(
                "웹 사진 판정 실패(모두 사용) | %s - %s: %s",
                short(draft_input.post_id),
                type(error).__name__,
                error,
            )
            return [True] * len(photos)
        if len(keeps) != len(photos):
            return [True] * len(photos)
        for photo, keep in zip(photos, keeps, strict=True):
            if not keep:
                logger.info(
                    "웹 사진 제외(판정 게이트: 엉뚱한 대상·그림·개인정보) | %s - %s (질의: %s)",
                    short(draft_input.post_id),
                    photo.source_url[:80],
                    photo.query or draft_input.input.topic,
                )
        return keeps

    async def _photos_for_source(
        self,
        task: BlogTask,
        named_subject,
        count: int,
        image_source: str,
        entity=None,
        visual_subject: str = "",
    ) -> list[WebPhoto]:
        """카드가 고른 소스(imageSource)에 따라 사진을 확보한다. 계약:

        - **모든 카드는 웹 검색이 먼저다**(2026-08-03 사용자 결정 — AI_GENERATED 판정도
          검색을 건너뛰지 못한다. 검색으로 아예 못 구했을 때만 생성으로 간다).
        - YOUTUBE_THUMBNAIL → 유튜브 썸네일 먼저, 없으면 네이버로 폴백.
        - 그 외(WEB_PHOTO·AI_GENERATED·미지정) → 네이버 먼저, 없으면 유튜브로 폴백.
        - 규격 미달(meets_spec=False) 사진만 있으면 그것을 돌려준다 — 호출부가 직접
          싣는 대신 생성의 참고 이미지로 쓴다.

        소재가 **유튜브에 공식 영상이 있는 콘텐츠**로 확인됐으면(entity), 카드가 명시적으로
        WEB_PHOTO를 고르지 않은 한 유튜브를 먼저 본다. 카드 계획 모델이 매번 소스를 옳게
        고르리라 기대하지 않는다 — 그 콘텐츠의 공식 회차 썸네일이 있는데 일반 웹 사진을
        먼저 집는 것은 정보의 손해다.
        """
        official_first = (
            entity is not None
            and entity.wants_official_youtube_thumbnail
            and image_source != "WEB_PHOTO"
        )
        if official_first and image_source not in ("", "YOUTUBE_THUMBNAIL"):
            # 카드 계획 모델의 판단과 검색 근거가 어긋난 경우다. 검색 근거가 이긴다 —
            # 실제로 존재하는 영상 콘텐츠라는 것은 출처가 확인해 준 사실이고, imageSource는
            # 본문만 보고 고른 추정이다. 어느 판정이 틀렸는지 나중에 볼 수 있게 남긴다.
            logger.info(
                "이미지 소스 판정 불일치 - 소재 정체를 따른다 | %s - 카드 %s vs 소재 %s(%s)",
                short(task.post_id),
                image_source,
                entity.entity_type,
                entity.canonical_name,
            )
        if image_source == "YOUTUBE_THUMBNAIL" or official_first:
            searchers = [self._youtube_search, self._photo_search]
        else:
            searchers = [self._photo_search, self._youtube_search]
        reference_only: list[WebPhoto] = []
        for searcher in (s for s in searchers if s is not None):
            photos = await self._find_web_photos(
                task,
                named_subject,
                count,
                searcher=searcher,
                entity=entity,
                visual_subject=visual_subject,
            )
            if any(photo.meets_spec for photo in photos):
                return photos
            if photos and not reference_only:
                reference_only = photos
        return reference_only

    async def _find_web_photos(
        self,
        task: BlogTask,
        named_subject,
        count: int,
        searcher=None,
        entity=None,
        visual_subject: str = "",
    ) -> list[WebPhoto]:
        """실제 사진을 웹에서 확보한다. 못 구하면 빈 목록 — 생성 경로로 되돌아간다.

        ``searcher``가 없으면 네이버 이미지 검색을 쓴다. 고유 대상(실존 인물·캐릭터)이
        있으면 그 이름으로 정밀하게 묻고, 없으면 소재로 묻는다 — 이미지 모델은 이름을
        알아도 그 사람의 얼굴을 그리지 못하고, 일반 장면도 실제 사진이 있으면 그쪽을 쓴다.

        **사용자가 참고 이미지를 올렸으면 검색하지 않는다.** 올린 사진이 그 소재에 대한
        더 나은 근거이고, 업로드 사진의 기존 쓰임(제품 충실도·image-to-image 기준)을
        여기서 바꾸지 않는다.

        검색·내려받기 실패는 원고를 실패시키지 않는다. 사진을 못 구한 것은 아쉬운 일이지,
        다 쓴 원고를 버릴 이유가 아니다.
        """
        if searcher is None:
            searcher = self._photo_search
        if searcher is None or count < 1:
            return []
        count = min(count, MAX_WEB_PHOTOS)
        queries = _web_photo_queries(task, named_subject, entity, visual_subject)
        # 검색으로 확인된 이름을 함께 넘긴다. 유튜브 검색기는 이 값이 있을 때만 후보를
        # 채점해 공식 회차를 고르고, 없으면 예전처럼 관련도 순서를 따른다. 네이버 검색기는
        # 이 인자를 받지 않으므로 키워드 인자를 지원할 때만 붙인다.
        grounding = (
            {
                "program_name": entity.canonical_name,
                # 짧은 이름(원본 검색어·브랜드)도 함께 — 정식 명칭이 길어 제목에 통째로
                # 안 담기는 콘텐츠에서 제목 앵커가 된다(2026-08-10, 스파이더맨 전멸 실측).
                "person_names": _anchor_names(entity),
                "official_channel": entity.official_channel,
            }
            if entity is not None and entity.wants_real_image
            else {}
        )
        if grounding and not _accepts_grounding(searcher):
            grounding = {}
        sub_spec: WebPhoto | None = None
        for position, query in enumerate(queries):
            try:
                photos = await searcher.find_photos(query, count, **grounding)
            except Exception as error:
                logger.warning(
                    "웹 사진 검색 실패 - 이미지 생성으로 진행 | %s - '%s': %s",
                    short(task.post_id),
                    query,
                    error,
                )
                return []
            if any(photo.meets_spec for photo in photos):
                logger.info(
                    "웹 사진 사용 | %s - '%s' %d장%s",
                    short(task.post_id),
                    query,
                    len(photos),
                    (
                        ""
                        if position == 0
                        else f" (앞선 질의 {position}개 실패 후 넓힌 것)"
                    ),
                )
                return photos
            # 규격 미달만 나온 질의는 다음 질의로 넓혀 보되, 첫 미달 사진은 기억해 둔다 —
            # 끝까지 규격 사진이 없으면 생성의 참고 이미지로 쓴다.
            if photos and sub_spec is None:
                sub_spec = photos[0]
        if sub_spec is not None:
            logger.info(
                "웹 사진 규격 미달만 발견 - 생성 참고용으로 사용 | %s - '%s'",
                short(task.post_id),
                sub_spec.query,
            )
            return [sub_spec]
        logger.info(
            "웹 사진 없음 - 이미지 생성으로 진행 | %s - '%s'",
            short(task.post_id),
            named_subject.identity if named_subject else (task.input.topic or ""),
        )
        return []

    async def _photo_slots_for_groups(
        self,
        task: BlogTask,
        positions_by_group: dict[tuple[str, str, str], list[int]],
        subject_by_group: dict[tuple[str, str, str], NamedSubject | None],
        entity: Any,
        slot_count: int,
    ) -> tuple[list[WebPhoto | None], list[WebPhoto | None], dict[int, list[WebPhoto]]]:
        """독립 사진 검색은 제한적으로 병렬 실행하고, 배정은 원래 순서대로 한다.

        검색 완료 순서대로 URL을 선점시키면 같은 입력도 네트워크 타이밍에 따라 다른 카드가
        사진을 가져간다. 그래서 결과를 모두 받은 뒤 ``positions_by_group``의 삽입 순서로
        중복 제거·배정을 수행한다. 두 번째 목록은 규격 미달 사진을 생성 참고용으로 돌려준다.

        세 번째 반환값은 자리별 **예비 후보 목록**이다: 자리 수보다 여유 있게
        (+WEB_PHOTO_GATE_SPARES) 검색해 남은 규격 통과 사진들로, 픽셀 게이트가 배정
        사진을 떨어뜨렸을 때 그 자리를 잇는다. 같은 그룹의 자리들은 같은 목록 객체를
        공유한다 — 한 자리가 쓴 예비를 다른 자리가 또 쓰지 않게 하는 것은 승격 쪽이
        URL 중복 검사로 챙긴다.
        """
        group_items = list(positions_by_group.items())
        semaphore = asyncio.Semaphore(PHOTO_SEARCH_GROUP_CONCURRENCY)

        async def search(key: tuple[str, str, str], positions: list[int]):
            async with semaphore:
                return await self._photos_for_source(
                    task,
                    subject_by_group[key],
                    len(positions) + WEB_PHOTO_GATE_SPARES,
                    key[1],
                    entity,
                    visual_subject=key[2],
                )

        found_by_group = await asyncio.gather(
            *(search(key, positions) for key, positions in group_items)
        )

        photo_slots: list[WebPhoto | None] = [None] * slot_count
        reference_slots: list[WebPhoto | None] = [None] * slot_count
        spare_photos: dict[int, list[WebPhoto]] = {}
        used_photo_urls: set[str] = set()
        for (_key, positions), found in zip(group_items, found_by_group, strict=True):
            fresh = [
                photo
                for photo in found
                if photo.meets_spec and photo.source_url not in used_photo_urls
            ]
            for position, photo in zip(positions, fresh, strict=False):
                photo_slots[position] = photo
                used_photo_urls.add(photo.source_url)
            # 자리를 다 채우고 남은 규격 통과 사진이 이 그룹의 예비다.
            leftovers = fresh[len(positions) :]
            if leftovers:
                for position in positions:
                    spare_photos[position] = leftovers
            # 규격 미달 사진은 직접 싣지 않는다. 대신 사진을 못 받은 카드의 **생성 참고
            # 이미지**가 된다 — 생성조차 웹 검색 결과를 참고한다(2026-08-03 사용자 결정).
            search_reference = next(
                (photo for photo in found if not photo.meets_spec), None
            )
            if search_reference is not None:
                for position in positions:
                    if photo_slots[position] is None:
                        reference_slots[position] = search_reference

        return photo_slots, reference_slots, spare_photos

    async def _promote_spare_photos(
        self,
        draft_input: DraftGenerationInput,
        rejected_slots: list[int],
        spare_photos: dict[int, list[WebPhoto]],
        in_use: set[str],
    ) -> dict[int, WebPhoto]:
        """게이트에서 떨어진 자리에 같은 그룹의 예비 후보를 승격한다.

        예비도 같은 게이트를 통과해야 한다 — 오판 방어용 여분이지 무검사 통로가 아니다.
        판정은 후보 전체를 한 번에 묶어 부른다(자리마다 따로 부르면 그 수만큼 느려진다).
        """
        candidates: list[tuple[int, WebPhoto]] = []
        seen: set[str] = set(in_use)
        for slot in rejected_slots:
            for photo in spare_photos.get(slot, []):
                if photo.source_url in seen:
                    continue
                seen.add(photo.source_url)
                candidates.append((slot, photo))
        if not candidates:
            return {}
        keeps = await self._usable_web_photos(
            draft_input, [photo for _, photo in candidates]
        )
        promoted: dict[int, WebPhoto] = {}
        for (slot, photo), keep in zip(candidates, keeps, strict=True):
            if keep and slot not in promoted:
                promoted[slot] = photo
                logger.info(
                    "웹 사진 예비 승격 | %s - %s 자리에 %s",
                    short(draft_input.post_id),
                    "대표" if slot == -1 else f"본문 {slot + 1}",
                    photo.source_url,
                )
        return promoted

    async def _with_card_images(
        self,
        result: DraftGenerationResult,
        task: BlogTask,
        draft_input: DraftGenerationInput,
        selected: SelectedCards,
        reporter: Any = None,
    ) -> DraftGenerationResult:
        """자연 사진 파이프라인.

        코드 도표 렌더링과 사진 생성을 병렬로 실행하고, 본문 사진은 텍스트·패널 없이 해당
        섹션에 배치한다. 썸네일만 이미지 어댑터가 FinalPost 문구를 중앙 반투명 제목 박스에
        합성한다. 실패한 본문 사진은 관련 없는 대체 사진으로 채우지 않는다.

        ``reporter``가 있으면 장면 하나가 끝날 때마다 몇 장이 완성됐는지 진행 카드에
        흘린다 — 실측으로 가장 긴 단계(~85초)라 여기가 제일 조용하면 안 된다.
        """
        final_post = result.final_post
        design = result.card_plan.design_system if result.card_plan else None
        reference_images = _reference_images(
            task, final_post.title, draft_input.reference_evidence
        )[: selected.reference_count]
        # image-to-image 시각 기준. 참고 이미지가 여러 장이면 카드가 자기 referenceId로
        # 필요한 장을 가리킨다 — 첫 장을 모든 생성의 기준으로 쓰지 않는다.
        reference_urls = _safe_reference_image_urls(
            task, draft_input.reference_evidence
        )
        thumbnail_named = named_subject_of(selected.thumbnail)
        # 고유 인물 썸네일은 참고 이미지를 '이 사람이 누구인가'의 근거로 함께 보낸다.
        thumbnail_person_references = _person_reference_urls(
            thumbnail_named, reference_urls, selected.thumbnail
        )
        thumbnail_reference = _reference_url_for(selected.thumbnail, reference_urls)
        if not _reuses_reference(selected.thumbnail):
            thumbnail_reference = (
                thumbnail_reference
                or (thumbnail_person_references[0] if thumbnail_person_references else None)
                or _first_reference_image_url(task, draft_input.reference_evidence)
            )
        # 새 썸네일은 제목 박스를 항상 갖는다. 모델이 thumbnailCopy를 비웠으면 제목에서
        # 안전한 두 줄을 만들며, DB 필드를 추가하지 않는다. 배치 확정은 사진을 확보한
        # 뒤로 미룬다 — 공식 영상 썸네일이 대표가 되면 문구를 얹지 않기 때문이다.
        copy_lines = thumbnail_lines(final_post.thumbnail_copy, final_post.title)

        # 소재 정체(영상 콘텐츠인지, 정식 명칭이 무엇인지)는 어떤 소스를 먼저 볼지와
        # 어떤 질의로 물을지를 함께 정한다. 판정이 없으면 None이라 예전 경로 그대로다.
        entity = (
            draft_input.reference_evidence.content_entity
            if draft_input.reference_evidence is not None
            else None
        )

        # 어떤 카드가 어떤 소스(웹 검색·유튜브 썸네일·AI 생성)를 쓸지는 카드 계획이
        # 정했다(imageSource, 2026-08-03 사용자 결정). 검색 소스가 찾은 카드는 이미지
        # 모델을 부르지 않고 그 사진을 그대로 쓰고, 못 찾은 카드만 생성으로 폴백한다.
        # 고유 대상(실존 인물·캐릭터) 카드는 그 이름으로, 나머지는 소재로 묻는다.
        # 같은 (대상, 소스, 시각 대상) 그룹은 한 번에 묶어 여러 장을 받아(호스트 분산)
        # 같은 사진이 두 번 실리지 않게 하고, 그룹이 달라도 이미 쓴 URL은 배정하지 않는다.
        #
        # 시각 대상이 키에 든 이유(2026-08-05): 같은 소재라도 문단마다 보여 줄 대상이
        # 다르다 — '레이디 디올'을 설명하는 섹션과 '북 토트'를 설명하는 섹션이 같은
        # 질의('디올')로 묶이면 두 사진이 같은 것을 보여 준다.
        card_briefs = [selected.thumbnail, *selected.body_cards]
        card_subjects = [
            thumbnail_named,
            *(named_subject_of(brief) for brief in selected.body_cards),
        ]
        positions_by_group: dict[tuple[str, str, str], list[int]] = {}
        subject_by_group: dict[tuple[str, str, str], NamedSubject | None] = {}
        for position, (brief, subject) in enumerate(
            zip(card_briefs, card_subjects, strict=True)
        ):
            # 이 카드가 실제로 쓸 수 있는 사용자 참고 이미지가 있으면 검색하지 않는다.
            # 참고와 무관한 다른 카드는 계속 검색해, 한 장을 올렸다는 이유로 글 전체의
            # 사진 검색이 꺼지던 동작만 없앤다.
            if _reuses_reference(brief) or _reference_url_for(
                brief, reference_urls
            ) or _person_reference_urls(
                subject, reference_urls, brief
            ):
                continue
            key = (
                subject.identity if subject is not None else "",
                brief.image_source or "",
                (brief.visual_subject or "").strip(),
            )
            positions_by_group.setdefault(key, []).append(position)
            subject_by_group.setdefault(key, subject)
        if positions_by_group:
            await _progress_detail(
                reporter, "네이버·유튜브에서 소재의 실제 사진을 찾는 중이에요…"
            )
        photo_slots, reference_slots, spare_photos = await self._photo_slots_for_groups(
            task,
            positions_by_group,
            subject_by_group,
            entity,
            len(card_briefs),
        )
        thumbnail_photo = photo_slots[0]
        thumbnail_search_reference = reference_slots[0]
        body_photos = {
            index: photo
            for index, photo in enumerate(photo_slots[1:])
            if photo is not None
        }
        body_search_references = {
            index: photo
            for index, photo in enumerate(reference_slots[1:])
            if photo is not None
        }

        # 웹 검색 사진은 싣기 전에 그림을 실제로 보고 거른다(_usable_web_photos 주석 참고).
        # 유튜브 공식 썸네일은 그 콘텐츠의 공식 이미지라 걸지 않는다. 걸러진 자리는 아래
        # 생성 폴백이 web_photo=None과 같은 경로로 메운다 — 잘못된 사진을 생성의 참고
        # 이미지로도 쓰지 않는다(엉뚱한 그림이 생성까지 끌고 간다).
        gated_slots: list[tuple[int, WebPhoto]] = []
        if (
            thumbnail_photo is not None
            and thumbnail_photo.source_type != WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
        ):
            gated_slots.append((-1, thumbnail_photo))
        gated_slots.extend(sorted(body_photos.items()))
        if gated_slots:
            keeps = await self._usable_web_photos(
                draft_input, [photo for _, photo in gated_slots]
            )
            rejected_slots: list[int] = []
            for (slot, _photo), keep in zip(gated_slots, keeps, strict=True):
                if keep:
                    continue
                if slot == -1:
                    thumbnail_photo = None
                else:
                    body_photos.pop(slot, None)
                rejected_slots.append(slot)
            # 떨어진 자리는 같은 그룹의 예비 후보로 잇는다 — 예전에는 자리당 한 장뿐이라
            # 게이트 오판 한 번에 그 자리가 곧장 생성 폴백으로 갔다(2026-08-10).
            if rejected_slots:
                in_use = {
                    photo.source_url
                    for photo in (thumbnail_photo, *body_photos.values())
                    if photo is not None
                }
                # 예비 목록은 카드 위치(0=썸네일, 1부터 본문) 기준이고, 게이트 슬롯은
                # -1=썸네일, 0부터 본문 기준이다 — 여기서 게이트 슬롯 기준으로 맞춘다.
                spares_by_slot = {
                    (-1 if position == 0 else position - 1): photos
                    for position, photos in spare_photos.items()
                }
                promoted = await self._promote_spare_photos(
                    draft_input, rejected_slots, spares_by_slot, in_use
                )
                for slot, photo in promoted.items():
                    if slot == -1:
                        thumbnail_photo = photo
                    else:
                        body_photos[slot] = photo

        secured = (1 if thumbnail_photo is not None else 0) + len(body_photos)
        if secured:
            await _progress_detail(
                reporter,
                f"실제 사진 {secured}장을 확보했어요 — 그 사진은 그대로 싣고 나머지만 그려요…",
            )

        # 공식 영상 썸네일에는 이미 인물·로고·영상 제목 문구가 들어 있다. 그 위에 Blog-it의
        # 반투명 제목 박스를 다시 얹으면 원본이 담고 있던 정보를 우리가 가리는 셈이다.
        # 생성 썸네일·일반 웹 사진으로 내려가면 예전 규격 그대로 문구가 얹힌다.
        official_thumbnail = (
            thumbnail_photo is not None
            and thumbnail_photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
        )
        if official_thumbnail:
            copy_lines = []
        # 인물·캐릭터가 주요 피사체면 제목 박스를 아래 띠로 내린다. 중앙 박스는 정확히
        # 얼굴 위에 앉아 "누구인지 보여 준다"는 썸네일의 목적을 지운다.
        #
        # 실존 인물이 소재인 글에서 찾아온 사진도 마찬가지다. 카드 계획이 그 카드를
        # 인물 카드로 표시하지 않았어도(thumbnail_named이 None이어도) 소재가 사람이면
        # 실린 사진은 그 사람의 얼굴이다 — 판정 근거가 계획이 아니라 소재에 있다.
        real_person_photo = (
            thumbnail_photo is not None
            and entity is not None
            and entity.is_real_person_or_group
        )
        thumbnail_layout = thumbnail_layout_plan_for(
            draft_input.editorial_style,
            copy_lines,
            face_safe=thumbnail_named is not None or official_thumbnail or real_person_photo,
        )

        # 표·그래프 렌더링과 자연 사진 생성을 함께 시작한다. 렌더링은 CPU(PIL),
        # 장면 생성은 provider 응답 대기라 서로를 기다릴 이유가 없다 — 예전에는 렌더링이
        # 이벤트 루프 위에서 순차로 끝난 뒤에야 이미지 호출이 나갔다. PIL은 스레드로
        # 내려 루프를 막지 않는다. 순번은 양쪽 결과가 모두 나온 뒤 생존자 기준으로 다시
        # 배치는 양쪽 결과가 모두 나온 뒤 섹션 기준으로 하므로 시작 순서는 결과에 영향이 없다.
        provisional_total = selected.total

        # 장면 하나가 끝날 때마다 진행 카드에 몇 장째인지 알린다. gather는 완료 순서와
        # 무관하게 결과 순서를 보존하므로, 이 래퍼는 표시만 더하고 결과는 바꾸지 않는다.
        scene_total = 1 + len(selected.body_cards)
        scene_progress = {"done": 0}

        async def _counted_scene(scene_coro):
            image = await scene_coro
            scene_progress["done"] += 1
            # 만들 장수는 시작할 때 정해져 있고 한 장 끝날 때마다 사실을 안다 —
            # 그 사실을 진행률로 그대로 쓰게 함께 싣는다(2026-08-11).
            await _progress_detail(
                reporter,
                f"이미지를 그리는 중이에요… ({scene_progress['done']}/{scene_total}장 완성)",
                units_done=scene_progress["done"],
                units_total=scene_total,
            )
            return image

        await _progress_detail(reporter, f"이미지 {scene_total}장을 그리기 시작했어요…")
        with perf.span("image_generation_total") as image_meta:
            render_results, scene_results = await asyncio.gather(
                asyncio.gather(
                    *(asyncio.to_thread(render_planned_visual, visual) for visual in selected.visuals)
                ),
                asyncio.gather(
                    _counted_scene(self._generate_card_scene(
                        task, draft_input, final_post, selected.thumbnail, design,
                        0, provisional_total, is_thumbnail=True,
                        # 사용자 참고 이미지가 없으면 웹 검색에서 건진 규격 미달 사진을
                        # 시각 기준으로 삼는다 — 생성조차 검색 결과를 참고한다.
                        reference_image=(
                            thumbnail_reference
                            if _reuses_reference(selected.thumbnail)
                            else (
                                thumbnail_reference
                                or (
                                    thumbnail_search_reference.data_url
                                    if thumbnail_search_reference
                                    else None
                                )
                            )
                        ),
                        thumbnail_layout=thumbnail_layout,
                        person_references=thumbnail_person_references,
                        web_photo=thumbnail_photo,
                    )),
                    *(
                        _counted_scene(self._generate_card_scene(
                            task, draft_input, final_post, brief, design,
                            index + 1, provisional_total, is_thumbnail=False,
                            reference_image=(
                                _reference_url_for(brief, reference_urls)
                                if _reuses_reference(brief)
                                else (
                                    _reference_url_for(brief, reference_urls)
                                    or _first_person_reference(brief, reference_urls)
                                    or (
                                        body_search_references[index].data_url
                                        if index in body_search_references
                                        else None
                                    )
                                )
                            ),
                            person_references=_person_reference_urls(
                                named_subject_of(brief), reference_urls, brief
                            ),
                            web_photo=body_photos.get(index),
                        ))
                        for index, brief in enumerate(selected.body_cards)
                    ),
                ),
            )
            image_meta["scenes"] = len(scene_results)
            image_meta["rendered_visuals"] = len(selected.visuals)

        rendered_pairs = []
        for visual, image in zip(selected.visuals, render_results, strict=True):
            if image is None:
                logger.warning("시각자료 %s 렌더링 실패 - 카드 구성에서 제외", visual.visual_id)
                continue
            rendered_pairs.append((visual, image))
        selected.visuals = [visual for visual, _ in rendered_pairs]

        thumbnail_image, *body_images = scene_results
        if thumbnail_image is None and not _reuses_reference(selected.thumbnail):
            # 사진 계획의 세부 장면이 provider에서 거절된 경우에도 글 전체를 버리지 않는다.
            # 일반 썸네일 프롬프트로 한 번만 폴백하며, 이것도 실패하면 기존 예외가 전파된다.
            # 폴백해도 실패한 카드의 고유 대상(캐릭터명·인물명)은 그대로 들고 간다 —
            # 여기서 이름을 잃으면 스파이더맨 글의 대표 이미지가 도시 야경이 된다.
            # 이름을 실을 수 있는 것은 실물 근거(인물 참고 사진·웹 검색 참고 이미지)가 있을
            # 때뿐이다(2026-08-10) — 이름만 실으면 안전 시스템이 반드시 거절하고, 카드별
            # 이름 표기가 달라 차단 기억(문자열 일치)도 비껴간다. 근거가 없으면 처음부터
            # 이름 없이 만든다.
            anchored_named = (
                thumbnail_named
                if thumbnail_person_references or thumbnail_reference
                else None
            )
            logger.warning(
                "계획 썸네일 생성 실패 - 일반 자연 사진 썸네일로 폴백%s | %s",
                (
                    f" (고유 대상 '{anchored_named.identity}' 유지)"
                    if anchored_named is not None
                    else " (고유 대상 없이)"
                ),
                short(task.post_id),
            )
            # 폴백 생성은 장면 카운터(n/m장 완성) 밖에서 돈다 — 화면이 '3/3장 완성'인데
            # 단계가 계속 도는 것처럼 보이지 않게, 지금 무엇을 하는지 알린다(2026-08-10
            # 실화면: 4분 30초째 그대로예요).
            await _progress_detail(reporter, "대표 이미지를 다시 만드는 중이에요…")
            thumbnail_image = await self._generate_thumbnail_with_subject_fallback(
                anchored_named,
                lambda subject: self._generate_image(
                    task,
                    draft_input,
                    final_post,
                    0,
                    (
                        colour_direction_for(draft_input.editorial_style)
                        if draft_input.editorial_style
                        else visual_style_for(task.post_id)
                    ),
                    max(1, selected.total),
                    is_thumbnail=True,
                    thumbnail_copy=copy_lines,
                    reference_image=(
                        thumbnail_reference if subject is not None else None
                    ),
                    thumbnail_layout=thumbnail_layout,
                    named_subject=subject,
                    # 안전 차단으로 이름을 내려놓는 재시도에서는 인물 사진도 함께 뺀다.
                    # 이름 때문에 거절된 요청에 그 사람의 얼굴 사진을 실어 보내면
                    # 같은 이유로 다시 거절된다.
                    person_references=(
                        thumbnail_person_references if subject is not None else []
                    ),
                    # 웹 사진은 이름을 내려놓는 재시도에서도 그대로 쓴다. 안전 차단은
                    # '그려 달라'는 요청이 막힌 것이고, 이 사진은 그리는 것이 아니다.
                    web_photo=thumbnail_photo,
                    # 이름을 내려놓은 호출에서는 anchor·확인된 특징도 함께 내려놓는다 —
                    # 그러지 않으면 프롬프트에 같은 이름이 도로 실려 반드시 또 차단된다.
                    suppress_subject_identity=(
                        subject is None and thumbnail_named is not None
                    ),
                ),
            )
            if thumbnail_image is None:
                logger.warning(
                    "대표 이미지 폴백까지 실패 - 대표 이미지 없이 계속합니다 | %s",
                    short(task.post_id),
                )
        elif thumbnail_image is None:
            # REUSED는 "이 정확한 사용자 사진을 생성 없이 쓴다"는 계약이다. 검증된 원본이
            # 없거나 로컬 크롭·문구 렌더링이 실패했다고 다른 사진/AI 생성으로 바꾸면 출처가
            # 거짓이 된다. 대표 이미지만 비우고 원고와 나머지 안전한 이미지는 살린다.
            logger.warning(
                "REUSED 썸네일을 만들지 못해 대표 이미지만 제외합니다 | %s - %s",
                short(task.post_id),
                selected.thumbnail.card_id,
            )

        # 계획이 약속한 장수를 남은 웹 사진으로 메운다(2026-08-10 사용자: "총 3장이라
        # 해놨으면서 이미지를 하나만 만들었어"). 고유 대상 카드가 근거 없이 접혔거나
        # 그 카드의 검색이 빈 자리에, 같은 소재의 남은 규격 통과 사진(다른 포스터·스틸)을
        # 같은 게이트에 통과시켜 싣는다. 그 사람이라고 속이는 생성이 아니라 소재의
        # 실사진을 더 싣는 것이므로, alt는 카드의 주장 대신 사진 자신의 제목으로 적는다.
        missing_slots = [
            position
            for position, (brief, image) in enumerate(
                zip(selected.body_cards, body_images, strict=True)
            )
            if image is None and not _reuses_reference(brief)
        ]
        if missing_slots:
            pool = _leftover_spare_photos(spare_photos, thumbnail_photo, body_photos)
            passers: list[WebPhoto] = []
            if pool:
                keeps = await self._usable_web_photos(draft_input, pool)
                passers = [
                    photo for photo, keep in zip(pool, keeps, strict=True) if keep
                ]
            await _progress_detail(reporter, "본문에 넣을 사진을 더 채우는 중이에요…")
            for position in missing_slots:
                if passers:
                    photo = passers.pop(0)
                    filled = await self._generate_card_scene(
                        task,
                        draft_input,
                        final_post,
                        selected.body_cards[position],
                        design,
                        position + 1,
                        provisional_total,
                        is_thumbnail=False,
                        web_photo=photo,
                    )
                    if filled is not None:
                        body_images[position] = filled.model_copy(
                            update={"alt_text": photo.title or task.input.topic}
                        )
                        logger.info(
                            "빈 본문 자리를 웹 예비 사진으로 채움 | %s - %s ← %s",
                            short(task.post_id),
                            selected.body_cards[position].card_id,
                            photo.source_url[:80],
                        )
                        continue
                # 마지막 수단: 대상 없는 분위기 사진(정체성·소재명 억제). 실사진도
                # 예비도 없을 때 계획 장수를 지킨다 — 이 채움이 실패한다고 원고를
                # 해치지는 않는다(그 자리만 빈다).
                try:
                    body_images[position] = await self._generate_image(
                        task,
                        draft_input,
                        final_post,
                        position + 1,
                        (
                            colour_direction_for(draft_input.editorial_style)
                            if draft_input.editorial_style
                            else visual_style_for(task.post_id)
                        ),
                        provisional_total,
                        is_thumbnail=False,
                        suppress_subject_identity=True,
                    )
                    logger.info(
                        "빈 본문 자리를 대상 없는 분위기 사진으로 채움 | %s - %s",
                        short(task.post_id),
                        selected.body_cards[position].card_id,
                    )
                except Exception as error:
                    logger.warning(
                        "본문 자리 채움 생성 실패 - 그 자리만 비웁니다 | %s - %s",
                        short(task.post_id),
                        error,
                    )
        survivors = [
            (brief, image)
            for brief, image in zip(selected.body_cards, body_images, strict=True)
            if image is not None
        ]
        if len(survivors) < len(selected.body_cards):
            # 실패한 본문 사진은 그 자리만 비운다. 특히 고유 인물 카드를 다른 사람 사진으로
            # 메우지 않는다 — 이름을 남긴 채 빠지는 편이 잘못된 얼굴보다 낫다.
            for brief, image in zip(selected.body_cards, body_images, strict=True):
                if image is None:
                    named = named_subject_of(brief)
                    logger.warning(
                        "본문 사진 %s 제외%s | %s",
                        brief.card_id,
                        f" - 고유 대상 '{named.identity}' 생성 실패" if named else "",
                        short(task.post_id),
                    )
            selected.body_cards = [brief for brief, _ in survivors]

        # 대표 썸네일은 본문 사진과 표시 규칙이 다르다(글 맨 위 한 장, 별도 여백).
        thumbnail = (
            thumbnail_image.model_copy(update={"media_kind": "cover"})
            if thumbnail_image is not None
            else None
        )
        body_cards = survivors
        html_by_id: dict[str, str] = {}
        markdown_by_id: dict[str, str] = {}
        visual_images: list[GeneratedPostImage] = []
        for visual, image in rendered_pairs:
            visual_images.append(image)
            html_by_id[visual.visual_id] = visual_html(image)
            markdown_by_id[visual.visual_id] = visual_markdown(image)

        # 배치: 썸네일·첨부는 앞, 자연 사진은 자기 섹션 아래, 표·그래프는 마커 자리.
        markdown = strip_image_tags(
            final_post.markdown_content or f"# {final_post.title}\n\n{final_post.body}"
        )
        html = strip_image_tags(final_post.html_content)
        markdown = replace_visual_markers(markdown, markdown_by_id)
        html = replace_visual_markers(html, html_by_id)

        spares: list[GeneratedPostImage] = []
        for brief, image in body_cards:
            section = section_number(brief.section_id)
            if section is None:
                spares.append(image)
                continue
            markdown = insert_after_heading_markdown(markdown, section, image_markdown(image))
            html = insert_after_heading_html(html, section, image_html(image))
        if spares:
            markdown = insert_markdown_images(markdown, spares)
            html = insert_html_images(html, spares)

        lead = ([thumbnail] if thumbnail is not None else []) + reference_images
        lead_markdown = "\n\n".join(image_markdown(image) for image in lead)
        lead_html = "\n".join(image_html(image) for image in lead)

        all_images = dedupe_images(
            [*lead, *[image for _, image in body_cards], *visual_images]
        )[:MAX_POST_IMAGES]

        placed = final_post.model_copy(
            update={
                "body": replace_visual_markers(strip_image_tags(final_post.body), {}),
                "images": all_images,
                "featured_image": thumbnail,
                # 실제로 얹힌 순수 문자열만 저장한다. 핵심어 색은 렌더 시점에만 적용하므로
                # DB에는 색상 마크업이나 별도 필드가 생기지 않는다.
                "thumbnail_copy": thumbnail_layout.copy_lines if thumbnail else [],
                "html_content": f"{lead_html}\n{html}" if lead else html,
                "markdown_content": f"{lead_markdown}\n\n{markdown}" if lead else markdown,
            }
        )

        # 실물 이미지를 써야 하는 글인데 생성 이미지가 대표로 실렸으면 규격 위반이다.
        # 다 만든 글을 여기서 버리지는 않고(사용자에게는 결과가 나가야 한다) 신호로 남긴다.
        # 두 검사는 대상이 겹치지 않는다 — 유튜브 공식 썸네일 대상이면 앞쪽만, 그 밖의
        # 실존 대상(상품·인물·장소·작품)이면 뒤쪽만 돈다.
        for check in (
            validate_official_thumbnail_used(placed, entity, thumbnail_photo),
            validate_real_entity_image_used(placed, entity, thumbnail_photo),
        ):
            if check.rejected:
                logger.warning(
                    "[content-validation] %s | %s - %s",
                    check.check,
                    short(task.post_id),
                    check.message,
                )

        return result.model_copy(
            update={"final_post": placed, "thumbnail_layout_plan": thumbnail_layout}
        )

    async def _body_image_or_none(
        self,
        task: BlogTask,
        draft_input: DraftGenerationInput,
        final_post: FinalPost,
        index: int,
        visual_style: str,
        total_images: int,
        scene: str | None,
        alt: str | None,
    ) -> GeneratedPostImage | None:
        """(계획 없음 경로) 본문 사진 한 장. 안전 차단은 None — 그 자리만 비운다.

        같은 프롬프트의 재시도는 반드시 같은 이유로 차단되므로 글을 죽일 이유가 없다.
        일시 실패(혼잡·타임아웃)는 그대로 전파한다 — 저장점 재개가 이미지 단계부터
        다시 시도한다(카드 경로의 장면 생성과 달리 이 경로에는 자체 재시도가 없다)."""
        try:
            return await self._generate_image(
                task,
                draft_input,
                final_post,
                index,
                visual_style,
                total_images,
                content_prompt=scene,
                content_alt=alt,
            )
        except Exception as error:
            if not _is_image_safety_block(error):
                raise
            logger.warning(
                "본문 사진 %d 프롬프트가 안전 시스템에 차단됨 - 그 자리만 비웁니다 | %s - %s",
                index,
                short(task.post_id),
                error,
            )
            return None

    async def _with_post_images(
        self,
        result: DraftGenerationResult,
        task: BlogTask,
        draft_input: DraftGenerationInput,
        named_subject: NamedSubject | None = None,
    ) -> DraftGenerationResult:
        final_post = result.final_post
        reference_images = _reference_images(
            task, final_post.title, draft_input.reference_evidence
        )

        if self._post_image_generator is None:
            if not reference_images:
                return result
            return result.model_copy(
                update={"final_post": _with_inserted_images(final_post, reference_images)}
            )

        # 사진 계획이 실패했거나 어댑터가 지원하지 않는 글은 대표 썸네일만 만든다.
        # 저장된 구형 원고에 명시적인 [[IMAGE:]] 장면 태그가 있을 때만 최대 2장의 본문
        # 사진을 계속 생성한다. 계획 실패를 관련 없는 장식 사진으로 채우지 않는다.
        tag_specs = extract_image_tags(final_post.markdown_content or "")[
            :LEGACY_BODY_IMAGE_LIMIT
        ]
        body_specs: list[tuple[str | None, str | None]] = list(tag_specs)

        visual_style = (
            colour_direction_for(draft_input.editorial_style)
            if draft_input.editorial_style
            else visual_style_for(task.post_id)
        )
        copy_lines = thumbnail_lines(final_post.thumbnail_copy, final_post.title)
        person_references = _person_reference_urls(
            named_subject,
            _safe_reference_image_urls(task, draft_input.reference_evidence),
        )
        # 계획이 없는 글에서도 대표 썸네일의 실제 사진을 먼저 찾는다(고유 대상이 있으면
        # 그 이름으로, 없으면 소재로; 소스 판단이 없으므로 기본 사다리 = 네이버→유튜브) —
        # 대표 썸네일 한 장이 이 글의 얼굴이므로, 여기서 생성물로 때우면 소재와 무관한
        # 대표 이미지가 남는다.
        #
        # 소재 정체(entity)는 카드 경로와 똑같이 넘긴다. 이것이 빠지면 정식 명칭·채널·
        # 출연자를 붙인 정밀 질의('김부장 SBS')를 못 쓰고 소재 단어만으로 묻게 되는데,
        # 실측(2026-08-03) 결과 소재 단어 단독 질의는 동명의 다른 콘텐츠(원작 웹툰)를
        # 가져왔다.
        entity = (
            draft_input.reference_evidence.content_entity
            if draft_input.reference_evidence is not None
            else None
        )
        # 대표 한 자리지만 후보는 여유 있게 받는다 — 게이트가 첫 후보를 떨어뜨려도
        # 다음 후보가 잇는다(카드 경로의 예비 승격과 같은 이유, 2026-08-10).
        web_photos = await self._photos_for_source(
            task, named_subject, 1 + WEB_PHOTO_GATE_SPARES, "", entity
        )
        spec_candidates = [p for p in web_photos if p.meets_spec]
        # 규격 미달 사진은 직접 싣지 않고 생성의 참고 이미지로만 쓴다.
        thumbnail_search_reference = next(
            (p for p in web_photos if not p.meets_spec), None
        )
        # 웹 검색 사진 그림 판정 — 카드 경로와 같은 관문(_usable_web_photos 주석 참고).
        # 유튜브 공식 썸네일은 걸지 않고, 나머지는 한 번에 묶어 판정한 뒤 후보 순서대로
        # 첫 통과자를 쓴다.
        gated = [
            p
            for p in spec_candidates
            if p.source_type != WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
        ]
        keeps = (
            dict(
                zip(
                    (p.source_url for p in gated),
                    await self._usable_web_photos(draft_input, gated),
                    strict=True,
                )
            )
            if gated
            else {}
        )
        thumbnail_photo = next(
            (
                p
                for p in spec_candidates
                if p.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
                or keeps.get(p.source_url, True)
            ),
            None,
        )
        # 공식 영상 썸네일에는 이미 제목 문구가 들어 있다 — 카드 경로와 같은 규칙으로
        # 우리 제목 박스를 덧씌우지 않는다.
        official_thumbnail = (
            thumbnail_photo is not None
            and thumbnail_photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL
        )
        if official_thumbnail:
            copy_lines = []
        # 계획이 버려진 경우에도 고유 인물이면 얼굴을 가리지 않는 배치를 쓴다.
        thumbnail_layout = thumbnail_layout_plan_for(
            draft_input.editorial_style,
            copy_lines,
            face_safe=named_subject is not None or official_thumbnail,
        )
        # 썸네일 1장 + 본문 사진. 고정값(3)이 아니라 이 글에서 실제로 만드는 수를 넘긴다.
        total_images = 1 + len(body_specs)

        # 폴백 경로에도 대표 썸네일은 참고 이미지를 닮게 생성한다(카드 계획이 없어
        # 본문 이미지는 관련 판단이 불가하므로 썸네일만). 업로드가 없으면 웹
        # 검색의 규격 미달 사진이 시각 기준이 된다.
        fallback_reference = _first_reference_image_url(
            task, draft_input.reference_evidence
        ) or (
            thumbnail_search_reference.data_url if thumbnail_search_reference else None
        )
        # 카드 경로와 같은 규칙(2026-08-10): 이름을 실을 수 있는 것은 실물 근거(인물
        # 참고 사진·참고 이미지)가 있을 때뿐이다. 근거 없이 이름만 실으면 안전 시스템이
        # 거절하고(호출당 수십~백여 초), 그 시간만 쓰고 결국 이름 없는 폴백으로 온다.
        anchored_named = (
            named_subject if (person_references or fallback_reference) else None
        )

        # 이미지 하나하나가 이미지 모델을 부르는 독립 호출이고, 각각 수십 초 걸린다. 하나씩
        # 기다리면 대기 시간이 그 전부의 합이 된다. 이미지들은 완성된 원고에만 의존할 뿐
        # 서로에게는 의존하지 않는다.
        thumbnail, *body_images = await asyncio.gather(
            self._generate_thumbnail_with_subject_fallback(
                # 계획이 규격에 걸려 버려졌더라도 그 계획이 알아낸 고유 대상은 남긴다.
                # 단, 그 이름이 안전 시스템에 차단되면(실존 인물) 이름 없이 한 번 더 간다.
                anchored_named,
                lambda subject: self._generate_image(
                    task,
                    draft_input,
                    final_post,
                    0,
                    visual_style,
                    total_images,
                    is_thumbnail=True,
                    thumbnail_copy=list(thumbnail_layout.copy_lines),
                    # 이름을 내려놓은 호출에는 참고 이미지도 싣지 않는다 — 그 사진이 바로
                    # 그 인물이라, 이름 때문에 거절된 요청에 얼굴을 실으면 또 거절된다.
                    # (고유 대상이 아예 없던 글은 원래대로 참고 이미지를 유지한다.)
                    reference_image=(
                        None
                        if subject is None and named_subject is not None
                        else fallback_reference
                    ),
                    thumbnail_layout=thumbnail_layout,
                    named_subject=subject,
                    # 이름을 내려놓는 재시도에는 인물 사진도 싣지 않는다(같은 이유로 다시
                    # 거절된다).
                    person_references=(
                        person_references if subject is not None else []
                    ),
                    web_photo=thumbnail_photo,
                    # 이름을 내려놓은 호출에서는 anchor·확인된 특징도 함께 내려놓는다 —
                    # 그러지 않으면 subject_identity가 근거 anchor로 도로 채워진다.
                    suppress_subject_identity=(
                        subject is None and named_subject is not None
                    ),
                ),
            ),
            *(
                self._body_image_or_none(
                    task,
                    draft_input,
                    final_post,
                    index + 1,
                    visual_style,
                    total_images,
                    scene,
                    alt,
                )
                for index, (scene, alt) in enumerate(body_specs)
            ),
        )
        # 실패한 본문 사진은 그 자리만 비운다 — 사진 한 장의 예외가 gather를 타고 올라와
        # 완성된 원고를 통째로 버리지 않게 한다(대표 썸네일과 같은 원칙, 2026-08-10).
        body_images = [image for image in body_images if image is not None]

        # 썸네일이 먼저고 그 다음이 업로드 이미지다. 본문 이미지는 태그가 있던 자리로
        # 돌아가고, 태그가 없으면 문단 사이에 고루 흩뿌린다.
        # 썸네일이 None이면(폴백까지 실패) 대표 이미지 없이 완성한다 — 카드 경로와 같다.
        lead = [
            *(
                [thumbnail.model_copy(update={"media_kind": "cover"})]
                if thumbnail is not None
                else []
            ),
            *reference_images,
        ]
        final_post = final_post.model_copy(
            update={
                "thumbnail_copy": (
                    thumbnail_layout.copy_lines if thumbnail is not None else []
                )
            }
        )

        if tag_specs:
            placed = _with_tagged_images(final_post, body_images, lead, len(tag_specs))
        else:
            placed = _with_inserted_images(final_post, [*lead, *body_images])

        return result.model_copy(
            update={"final_post": placed, "thumbnail_layout_plan": thumbnail_layout}
        )
