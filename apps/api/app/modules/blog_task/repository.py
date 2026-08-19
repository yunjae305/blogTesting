"""블로그 태스크 저장소.repository.ts.

원본에서 구조를 두 군데 바꿨고, 둘 다 동작은 그대로다:

- 전이 확인 / 버전 증가 / 히스토리 추가 순서가 원본에선 네 메서드에 흩어져 있었고 이미
  어긋나 있었다(updateFinalPost는 다른 상태 검사를 썼다). 여기서는 한곳에 모았다.
- 쓰기마다 왕복이 세 번(읽기, 갱신, 다시 읽기)이었다. find_one_and_update는 버전을
  지키는 쓰기와 같은 호출에서 갱신된 문서를 돌려주므로, 이제 두 번이다.
"""

import re

from app.shared.format import now_iso as _now
from collections.abc import Sequence
from typing import Any, Protocol

from bson import Binary
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.mongo import strip_id
from app.errors import BlogTaskError
from app.shared.base import CamelModel
from app.shared.image_bytes import shrink, to_data_url
# 순수 함수만 있는 모듈이다(외부 호출·DB 없음). draft 패키지의 __init__은 비어 있어
# 이 import로 순환이 생기지 않는다.
from app.modules.draft.duplication import PostDigest, extract_headings, first_paragraph
from app.shared import (
    ActivityEntry,
    BlogTask,
    BlogTaskInput,
    BlogTaskListItem,
    BlogTaskStatus,
    DraftCheckpoint,
    DraftGenerationResult,
    IntentValidationResult,
    PostSummary,
    PostingLog,
    SearchSource,
    SelectedIntent,
    SeoKeywordPlan,
    StatusHistoryEntry,
    TitlePlan,
    TaskProgress,
    TrendSelection,
    can_transition,
)


# 입력값에서 파생된 모든 것. 입력이 수정되면 지운다: 트렌드는 옛 주제로 추천됐고 의도는
# 그 주제에 맞춰 검증됐다. 남겨 두면 여전히 이 글을 설명하는 양 보이게 된다.
DERIVED_FIELDS = (
    "trendSelection",
    "intentValidationResult",
    "selectedIntent",
    "titlePlan",
    "seoKeywordPlan",
    "draftGenerationResult",
)

# 목록 카드가 실제로 읽는 필드만 Mongo에서 가져온다. 특히 finalPost의 본문·HTML·마크다운·
# base64 이미지와, 같은 원고를 다시 품고 있는 draftGenerationResult는 projection 단계에서
# 제외돼 API 프로세스까지 전송되지 않는다.
BLOG_TASK_LIST_ITEM_PROJECTION = {
    "_id": 0,
    "postId": 1,
    "userId": 1,
    "status": 1,
    "version": 1,
    "createdAt": 1,
    "updatedAt": 1,
    "input.topic": 1,
    "input.subject": 1,
    "input.purpose": 1,
    "input.keywords": 1,
    "trendSelection.finalTopic": 1,
    "finalPost.title": 1,
    "postingLogs.postUrl": 1,
}

# Polling must not read the multi-megabyte post document.  In particular, keep
# reference material data URLs and generated images out of this projection.
# ``intentValidationResult.analyzedAt`` is the smallest durable field that lets
# the M3 client distinguish "still searching" from "result is ready" while the
# status is still SEARCH_ANALYZING.
BLOG_TASK_STATUS_PROJECTION = {
    "_id": 0,
    "postId": 1,
    "status": 1,
    "version": 1,
    "progress": 1,
    "intentValidationResult.analyzedAt": 1,
}


class BlogTaskStatusSnapshot(CamelModel):
    """Small owner-scoped polling contract; never contains post content or images."""

    post_id: str
    status: BlogTaskStatus
    version: int
    progress: TaskProgress | None = None
    has_intent_validation_result: bool = False
    # 생성 중 '작업 현황' 로그(2026-08-10). DB가 아니라 프로세스 메모리(jobs 모듈)에서
    # 오므로 repository는 채우지 않는다 — 서비스가 응답 직전에 붙인다.
    activity_log: list[ActivityEntry] = []


def _status_snapshot_from_document(document: dict[str, Any]) -> BlogTaskStatusSnapshot:
    progress = document.get("progress")
    return BlogTaskStatusSnapshot(
        post_id=document["postId"],
        status=document["status"],
        version=document["version"],
        progress=TaskProgress.model_validate(progress) if isinstance(progress, dict) else None,
        has_intent_validation_result=bool(document.get("intentValidationResult")),
    )


def _status_snapshot_from_task(task: BlogTask) -> BlogTaskStatusSnapshot:
    return BlogTaskStatusSnapshot(
        post_id=task.post_id,
        status=task.status,
        version=task.version,
        progress=task.progress,
        has_intent_validation_result=task.intent_validation_result is not None,
    )


def _first_post_url(logs: Sequence[Any]) -> str | None:
    for log in logs:
        value = log.get("postUrl") if isinstance(log, dict) else log.post_url
        if value:
            return value
    return None


def _list_item_from_document(document: dict[str, Any]) -> BlogTaskListItem:
    blog_input = document.get("input") or {}
    final_post = document.get("finalPost")
    trend_selection = document.get("trendSelection") or {}
    purpose = blog_input.get("purpose")
    purposes = (blog_input.get("keywords") or []) if purpose is None else purpose
    title = (
        (final_post or {}).get("title")
        or trend_selection.get("finalTopic")
        or blog_input.get("topic")
        or ""
    )
    return BlogTaskListItem(
        post_id=document["postId"],
        user_id=document["userId"],
        status=document["status"],
        version=document["version"],
        created_at=document["createdAt"],
        updated_at=document["updatedAt"],
        title=title,
        topic=blog_input.get("topic") or "",
        subject=blog_input.get("subject"),
        purposes=list(purposes),
        post_url=_first_post_url(document.get("postingLogs") or []),
        has_final_post=final_post is not None,
    )


def _digest_from_document(document: dict) -> PostDigest:
    """중복 검사용 요약. 투영된 문서에서 바로 만든다(모델 검증을 거치지 않는다 —
    제목·소제목·도입부만 있으면 되고, 옛 문서에 빠진 필드가 있어도 검사는 돌아야 한다)."""
    final = document.get("finalPost") or {}
    markdown = final.get("markdownContent") or final.get("body") or ""
    return PostDigest(
        post_id=str(document.get("postId") or ""),
        title=str(final.get("title") or ""),
        headings=extract_headings(markdown),
        opening=first_paragraph(markdown),
    )


def _summary_from_document(document: dict[str, Any]) -> PostSummary:
    """투영된 문서에서 바로 만든다(모델 검증을 거치지 않는다).

    옛 문서에 빠진 필드가 있어도 예약 목록은 그려져야 한다 — 여기서 검증에 걸려
    터지면 목록 전체가 상태를 잃는다.
    """
    logs = document.get("postingLogs") or []
    progress = document.get("progress")
    return PostSummary(
        post_id=document["postId"],
        status=document["status"],
        title=((document.get("finalPost") or {}).get("title") or "").strip() or None,
        published_url=_first_post_url(logs),
        progress=TaskProgress.model_validate(progress) if isinstance(progress, dict) else None,
    )


def _list_item_from_task(task: BlogTask) -> BlogTaskListItem:
    purposes = task.input.keywords if task.input.purpose is None else task.input.purpose
    title = (
        (task.final_post.title if task.final_post else None)
        or (task.trend_selection.final_topic if task.trend_selection else None)
        or task.input.topic
    )
    return BlogTaskListItem(
        post_id=task.post_id,
        user_id=task.user_id,
        status=task.status,
        version=task.version,
        created_at=task.created_at,
        updated_at=task.updated_at,
        title=title,
        topic=task.input.topic,
        subject=task.input.subject,
        purposes=list(purposes),
        post_url=_first_post_url(task.posting_logs),
        has_final_post=task.final_post is not None,
    )


class BlogTaskRepository(Protocol):
    async def create(self, task: BlogTask) -> BlogTask: ...
    async def find_by_post_id(self, post_id: str) -> BlogTask | None: ...
    async def summaries_by_post_ids(self, post_ids: list[str]) -> dict[str, PostSummary]: ...
    async def owns_post(self, user_id: str, post_id: str) -> bool: ...
    async def find_by_user_and_post_id(
        self, user_id: str, post_id: str
    ) -> BlogTask | None: ...
    async def find_status_by_user_and_post_id(
        self, user_id: str, post_id: str
    ) -> BlogTaskStatusSnapshot | None: ...
    async def list_by_user_id(self, user_id: str) -> list[BlogTask]: ...
    async def list_items_by_user_id(self, user_id: str) -> list[BlogTaskListItem]: ...
    async def list_published_digests(
        self, user_id: str, limit: int = 30, exclude_post_id: str | None = None
    ) -> list[PostDigest]: ...
    async def list_by_status(self, statuses: list[BlogTaskStatus]) -> list[BlogTask]: ...
    async def delete_by_post_id(self, post_id: str) -> None: ...
    async def delete_by_user_and_post_id(self, user_id: str, post_id: str) -> None: ...
    async def transition_status(
        self, post_id: str, next_status: BlogTaskStatus, actor: str
    ) -> BlogTask: ...
    async def append_posting_log(self, post_id: str, log: PostingLog) -> BlogTask: ...
    async def replace_input(
        self, post_id: str, blog_input: BlogTaskInput, actor: str
    ) -> BlogTask: ...
    async def update_progress(self, post_id: str, progress: TaskProgress | None) -> None: ...
    async def save_title_plan(self, post_id: str, plan: TitlePlan) -> None: ...
    async def save_seo_keyword_plan(self, post_id: str, plan: SeoKeywordPlan) -> None: ...
    async def save_draft_checkpoint(
        self, post_id: str, checkpoint: DraftCheckpoint
    ) -> None: ...
    async def load_draft_checkpoint(self, post_id: str) -> DraftCheckpoint | None: ...
    async def clear_draft_checkpoint(self, post_id: str) -> None: ...
    async def rewind_status(
        self, post_id: str, to_status: BlogTaskStatus, actor: str
    ) -> BlogTask: ...
    async def save_intent_validation_result(
        self, post_id: str, result: IntentValidationResult
    ) -> BlogTask: ...
    async def select_intent(
        self, post_id: str, intent_id: str, actor: str, excluded_source_urls: Sequence[str] = ()
    ) -> BlogTask: ...
    async def replace_selected_intent_sources(
        self, post_id: str, sources: Sequence[SearchSource], actor: str
    ) -> BlogTask: ...
    async def save_trend_selection(
        self, post_id: str, selection: TrendSelection, actor: str
    ) -> BlogTask: ...
    async def save_draft_generation_result(
        self, post_id: str, result: DraftGenerationResult, actor: str
    ) -> BlogTask: ...
    async def update_final_post(
        self, post_id: str, result: DraftGenerationResult, actor: str
    ) -> BlogTask: ...


def _require_transition(current: BlogTask, next_status: BlogTaskStatus) -> None:
    if not can_transition(current.status, next_status):
        raise BlogTaskError(
            "INVALID_STATUS_TRANSITION",
            f"cannot transition blogTask {current.post_id} from {current.status.value} to {next_status.value}",
        )


def _selected_intent_from(
    current: BlogTask, intent_id: str, excluded_source_urls: Sequence[str] = ()
) -> SelectedIntent:
    candidates = (
        current.intent_validation_result.intent_candidates
        if current.intent_validation_result
        else []
    )
    candidate = next((c for c in candidates if c.intent_id == intent_id), None)
    if candidate is None:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"intent candidate {intent_id} does not exist on blogTask {current.post_id}",
        )
    # 사용자가 검증 화면에서 체크 해제한 자료는 여기서 걸러 낸다. 이 SelectedIntent가 곧
    # 원고 프롬프트의 자료 목록이 되므로(draft: selected_intent.sources), 제외한 자료는
    # 실제로 글에 쓰이지 않는다.
    excluded = {url for url in excluded_source_urls if url}
    sources = [source for source in candidate.sources if source.url not in excluded]
    return SelectedIntent(
        intent_id=candidate.intent_id,
        title=candidate.title,
        target_reader=candidate.target_reader,
        rationale=candidate.rationale,
        keywords=candidate.keywords,
        sources=sources,
    )


#: 이미지 바이트가 사는 컬렉션. 글 문서에는 번호와 설명만 남는다.
POST_IMAGES = "post_images"

#: 이미지가 옆 컬렉션에 있다는 표시. 글 문서의 ``dataUrl`` 자리에 들어간다.
#:
#: 빈 문자열이 아니라 표시를 쓰는 이유: 빈 문자열은 "이미지가 없다"와 구분되지 않아서,
#: 못 붙인 채로 발행되면 **이미지 없는 글이 조용히 올라간다.** 이 표시가 남아 있으면
#: 무엇이 잘못됐는지 알 수 있다.
IMAGE_ELSEWHERE = "stored:post_images"

#: 글을 읽을 때 **가져오지 않는** 필드.
#:
#: `draftCheckpoint`는 원고 생성 중간 저장점이라 `BlogTask` 모델에 없다 — 가져와도
#: `_to_task`가 그냥 버린다. 그런데 실측에서 한 건이 **2.2MB**였다(2026-08-06). 회선이
#: 0.09MB/s라 버릴 값을 받는 데만 24초가 걸린 셈이고, 그래서 그 글이 안 열렸다.
#:
#: 필요한 곳은 `load_draft_checkpoint`가 따로 읽는다.
WITHOUT_UNUSED_FIELDS = {"draftCheckpoint": 0}

#: 한 번의 왕복에 이미지 몇 장을 받을지. 한 장씩 받는다(`_images_of` 참고).
#:
#: 한꺼번에 받으면 소켓 읽기 한 번이 그만큼 커진다. 5장이면 약 2MB인데 실측 회선이
#: 0.09MB/s여서 22초 — 20초 제한을 넘겨 글이 안 열렸다. 한 장이면 5초쯤이라 들어온다.
IMAGES_PER_ROUND_TRIP = 1


def _split_images(final_post: dict, post_id: str) -> tuple[dict, list[dict]]:
    """원고에서 이미지 바이트를 떼어 낸다.

    글 문서 한 건이 base64 이미지 때문에 1.6MB까지 커졌고, **그 글을 여는 것이 20초
    타임아웃으로 실패했다**(2026-08-06 실측: 가벼운 필드만 읽으면 20ms, 이미지까지 읽으면
    실패). 최적화가 아니라 오류 수정이다.

    바이트는 ``post_images``로 가고, 글 문서에는 몇 번째 이미지인지와 설명만 남는다.
    발행할 때는 ``_attach_images``가 다시 붙여 **지금과 똑같은 data URL**을 만든다 —
    네이버·스레드에 넘기는 값은 달라지지 않는다.

    **본문 글에서도 뺀다.** 같은 이미지가 ``htmlContent``와 ``markdownContent`` 안에도
    인라인 base64로 한 벌씩 더 들어 있었다(2026-08-06 실측: 글 하나에서 html 1.4MB +
    markdown 1.4MB, 51건이 그랬다). ``images[]``만 빼서는 글 문서가 여전히 2~3MB라
    여는 것이 20초 제한을 넘겼다. 자리에는 ``stored:post_images#3`` 같은 자리표를 남기고,
    읽을 때 되돌린다.
    """
    images = final_post.get("images") or []
    featured = final_post.get("featuredImage")

    rows: list[dict] = []
    # 본문 글에서 바꿔야 할 (있던 값, 몇 번째). 저장하는 행은 이진이라 그 값이 남지
    # 않으므로 여기 따로 들고 있어야 한다.
    was_in_text: list[tuple[str, int]] = []
    light_images = []
    for index, image in enumerate(images):
        data_url = image.get("dataUrl") or ""
        light_image = {**image, "dataUrl": IMAGE_ELSEWHERE}
        if data_url and data_url != IMAGE_ELSEWHERE:
            row = _image_row(post_id, index, data_url)
            rows.append(row)
            was_in_text.append((data_url, index))
            light_image["mimeType"] = row["mimeType"]
        light_images.append(light_image)

    light = {**final_post, "images": light_images}
    if isinstance(featured, dict):
        data_url = featured.get("dataUrl") or ""
        light["featuredImage"] = {**featured, "dataUrl": IMAGE_ELSEWHERE}
        if data_url and data_url != IMAGE_ELSEWHERE:
            # 대표 이미지는 -1번으로 둔다. 본문 이미지와 번호가 겹치지 않는다.
            row = _image_row(post_id, -1, data_url)
            rows.append(row)
            was_in_text.append((data_url, -1))
            light["featuredImage"]["mimeType"] = row["mimeType"]

    for field in IMAGE_BEARING_TEXT:
        text = light.get(field)
        if not isinstance(text, str) or not text:
            continue
        for data_url, index in was_in_text:
            text = text.replace(data_url, _placeholder(index))
        light[field] = text
    return light, rows


#: 본문 글에도 같은 이미지가 인라인 base64로 들어간다. 여기서도 빼야 한다.
IMAGE_BEARING_TEXT = ("htmlContent", "markdownContent")


def _image_row(post_id: str, index: int, data_url: str) -> dict:
    """이미지 한 장을 저장할 모양으로 만든다.

    **base64 글자가 아니라 이진으로 담는다.** base64는 원본보다 33% 크다 — 글 하나를
    여는 데 오가는 것의 85%가 이미지였으므로(2026-08-06 실측) 그 33%가 그대로 대기
    시간이었다. 함께 900px로 줄인다(`app/shared/image_bytes.py` 참고).

    읽을 때 `_data_url_of`가 되돌려 **발행 경로에는 지금과 같은 data URL**이 간다.
    """
    raw, mime = shrink(data_url)
    if not raw:
        # 줄이지도 못하고 열지도 못한 것(data URL이 아닌 값 등). 있는 그대로 둔다.
        return {"postId": post_id, "index": index, "dataUrl": data_url, "mimeType": mime}
    return {"postId": post_id, "index": index, "bytes": Binary(raw), "mimeType": mime}


def _data_url_of(row: dict) -> str:
    """저장된 한 행을 발행 경로가 받는 data URL로 되돌린다.

    옛 행(``dataUrl`` 글자)도 그대로 읽는다 — 이관 전에 저장된 것들이다.
    """
    stored = row.get("dataUrl")
    if isinstance(stored, str) and stored:
        return stored
    raw = row.get("bytes")
    if not raw:
        return ""
    return to_data_url(bytes(raw), row.get("mimeType") or "")


def _placeholder(index: int) -> str:
    """본문 글 안에 남기는 자리표. 몇 번째 이미지인지가 붙는다."""
    return f"{IMAGE_ELSEWHERE}#{index}"


#: 본문 글에서 자리표를 찾는다. ``stored:post_images#-1``은 대표 이미지다.
PLACEHOLDER_IN_TEXT = re.compile(re.escape(IMAGE_ELSEWHERE) + r"#(-?\d+)")


def _needs_images(final_post: dict | None) -> bool:
    if not isinstance(final_post, dict):
        return False
    if (final_post.get("featuredImage") or {}).get("dataUrl") == IMAGE_ELSEWHERE:
        return True
    if any(
        (image or {}).get("dataUrl") == IMAGE_ELSEWHERE for image in final_post.get("images") or []
    ):
        return True
    # 본문 글에만 자리표가 남은 글도 있다. 놓치면 "stored:post_images#0"이라는 글자가
    # 이미지 자리에 그대로 발행된다.
    return any(
        isinstance(final_post.get(field), str) and IMAGE_ELSEWHERE in final_post[field]
        for field in IMAGE_BEARING_TEXT
    )


def _without_duplicate_final_post(draft_result: dict) -> dict:
    """저장 직전에 ``draftGenerationResult`` 안의 원고를 뺀다.

    ``DraftGenerationResult.final_post``는 문서 맨 위의 ``finalPost``와 **항상 같은 값**이다
    (둘 다 같은 객체에서 나온다). 그런데 그 안에는 base64 이미지가 들어 있어, 두 벌을
    쓰면 문서가 정확히 두 배가 된다 — 실측한 글은 1.11MB 중 0.54MB씩 똑같은 것이 두 벌
    이었다.

    한 벌만 쓰고, 읽을 때 ``_with_restored_final_post``가 되돌린다.
    """
    trimmed = dict(draft_result)
    trimmed.pop("finalPost", None)
    return trimmed


def _with_restored_final_post(document: dict) -> dict:
    """저장할 때 뺀 원고를 읽으면서 되돌린다.

    옛 문서에는 두 벌 다 들어 있다 — 그때는 손대지 않는다(호환).

    되돌릴 수 없는 경우, 즉 ``draftGenerationResult``는 있는데 맨 위 ``finalPost``가 없는
    문서는 **조용히 넘기지 않는다.** 그대로 두면 모델 검증이 "finalPost 필드가 없다"는
    말만 남기고 어느 글인지 알려 주지 않는다.
    """
    draft_result = document.get("draftGenerationResult")
    if not isinstance(draft_result, dict) or "finalPost" in draft_result:
        return document

    final_post = document.get("finalPost")
    if not isinstance(final_post, dict):
        raise BlogTaskError(
            "CORRUPT_DOCUMENT",
            f"글 {document.get('postId')}의 원고를 복원하지 못했습니다: "
            "draftGenerationResult는 있는데 finalPost가 없습니다.",
        )
    return {**document, "draftGenerationResult": {**draft_result, "finalPost": final_post}}


#: 형광펜 치환에 먹혀 `**`로 바뀐 base64 패딩(`==`). markdown 이미지의 닫는 괄호 앞만
#: 잡는다 — `![alt](data:…base64,…2Q**)` 꼴로 저장된 글이 실재한다(2026-08-07 실측).
_BROKEN_IMAGE_PADDING = re.compile(r"(base64,[A-Za-z0-9+/]+)\*\*\)")


def _repair_markdown_image_padding(document: dict) -> dict:
    """오염돼 저장된 markdown 이미지의 base64 패딩을 읽으면서 되돌린다.

    markdown_for_storage의 형광펜 치환(`==`→`**`)이 이미지 data URL의 패딩까지 바꿔
    놓은 글들이 있다(생성 쪽은 2026-08-07에 고쳤다). 그 글은 미리보기 이미지가 깨지고,
    이미지 외부화도 그 사본을 건너뛴다. 저장분을 고치는 이관 대신 읽기에서 되돌린다 —
    다음 저장 때 자연히 바로잡힌 값이 저장된다.
    """
    final_post = document.get("finalPost")
    if not isinstance(final_post, dict):
        return document
    markdown = final_post.get("markdownContent")
    if not isinstance(markdown, str) or "base64," not in markdown:
        return document
    repaired = _BROKEN_IMAGE_PADDING.sub(r"\1==)", markdown)
    if repaired == markdown:
        return document
    return {**document, "finalPost": {**final_post, "markdownContent": repaired}}


def _to_task(document: dict) -> BlogTask:
    return BlogTask.model_validate(
        _with_restored_final_post(_repair_markdown_image_padding(document))
    )


class MongoBlogTaskRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self._collection = db["blogTask"]
        # 이미지 바이트는 여기 산다(_split_images 참고).
        self._images = db[POST_IMAGES]

    async def _store_images(self, post_id: str, rows: list[dict]) -> None:
        """이 글의 이미지를 갈아 끼운다. 옛것을 지우고 새것을 넣는다.

        원고를 다시 만들면 이미지도 바뀐다. 지우지 않으면 옛 이미지가 남아, 번호가 줄어든
        경우 없는 이미지를 가리키게 된다.
        """
        await self._images.delete_many({"postId": post_id})
        if rows:
            await self._images.insert_many(rows)

    async def _attach_images(self, document: dict | None) -> dict | None:
        """글 문서에 이미지 바이트를 다시 붙인다.

        옛 문서(바이트가 안에 그대로 있는)는 손대지 않는다 — 표시가 없으면 그냥 지나간다.

        **못 붙이면 조용히 넘기지 않는다.** 어느 글의 몇 번째가 없는지 말하고 멈춘다.
        그대로 두면 이미지 없는 글이 그대로 발행된다.
        """
        if document is None:
            return None
        final_post = document.get("finalPost")
        if not _needs_images(final_post):
            return document

        post_id = document.get("postId")
        stored = {
            row["index"]: _data_url_of(row)
            async for row in self._images_of({"postId": post_id})
        }
        return self._restore(document, stored)

    def _images_of(self, query: dict):
        """이미지를 **한 장씩** 받아 온다.

        한꺼번에 받으면 한 번의 소켓 읽기가 그만큼 커진다. 글 하나에 이미지가 5장이면
        약 2MB인데, 실측한 회선이 0.09MB/s여서 22초가 걸린다 — `SOCKET_TIMEOUT_MS`(20초)를
        넘겨 **글이 아예 안 열렸다**(2026-08-06: 3건 중 2건이 NetworkTimeout).

        한 장은 0.4MB 남짓이라 한 번의 읽기가 5초쯤이다. 오가는 총 바이트는 같으니 전체
        시간은 그대로지만, **제한에 걸려 실패하지는 않는다.** 제한을 늘리지 않는 이유는
        그 20초가 '응답 없는 소켓이 프로세스를 붙잡는 것'을 막는 값이기 때문이다
        (`app/db/mongo.py` 참고).
        """
        return self._images.find(query, {"_id": 0}).batch_size(IMAGES_PER_ROUND_TRIP)

    async def _attach_many(self, documents: list[dict]) -> list[dict]:
        """여러 글에 이미지를 붙인다. **글마다 따로 묻지 않는다.**

        따로 물으면 목록 한 번에 쿼리가 글 수만큼 나간다. 필요한 글이 없으면 이미지
        컬렉션을 아예 부르지 않는다 — 옛 문서만 있을 때 부를 이유가 없다.
        """
        wanted = [doc["postId"] for doc in documents if _needs_images(doc.get("finalPost"))]
        if not wanted:
            return documents

        by_post: dict[str, dict[int, str]] = {}
        async for row in self._images_of({"postId": {"$in": wanted}}):
            by_post.setdefault(row["postId"], {})[row["index"]] = _data_url_of(row)
        return [self._restore(doc, by_post.get(doc["postId"], {})) for doc in documents]

    def _restore(self, document: dict, stored: dict[int, str]) -> dict:
        """이미 읽어 둔 이미지로 글 문서를 채운다. 묻는 것과 채우는 것을 나눠, 목록은
        글마다 따로 묻지 않고 한 번에 모아 올 수 있다."""
        final_post = document.get("finalPost")
        if not _needs_images(final_post):
            return document
        post_id = document.get("postId")

        missing: list[str] = []
        images = []
        for index, image in enumerate(final_post.get("images") or []):
            if (image or {}).get("dataUrl") != IMAGE_ELSEWHERE:
                images.append(image)
                continue
            data_url = stored.get(index)
            if not data_url:
                missing.append(f"{index + 1}번째")
                continue
            images.append({**image, "dataUrl": data_url})

        featured = final_post.get("featuredImage")
        if isinstance(featured, dict) and featured.get("dataUrl") == IMAGE_ELSEWHERE:
            data_url = stored.get(-1)
            if not data_url:
                missing.append("대표 이미지")
            else:
                featured = {**featured, "dataUrl": data_url}

        restored = {**final_post, "images": images}
        if isinstance(featured, dict):
            restored["featuredImage"] = featured

        # 본문 글 안의 자리표도 되돌린다. 안 되돌리면 발행된 글에 이미지 대신
        # "stored:post_images#0"이라는 글자가 그대로 들어간다.
        for field in IMAGE_BEARING_TEXT:
            text = restored.get(field)
            if isinstance(text, str) and text:
                restored[field] = self._text_with_images(text, stored, missing)

        if missing:
            raise BlogTaskError(
                "MISSING_POST_IMAGES",
                f"글 {post_id}의 이미지를 찾지 못했습니다({', '.join(dict.fromkeys(missing))}). "
                f"{POST_IMAGES} 컬렉션에 해당 이미지가 없습니다 — 이미지 없이 발행하지 "
                "않으려고 여기서 멈춥니다.",
            )
        return {**document, "finalPost": restored}

    @staticmethod
    def _text_with_images(text: str, stored: dict[int, str], missing: list[str]) -> str:
        """본문 글의 자리표를 실제 바이트로 되돌린다."""

        def swap(match: re.Match) -> str:
            index = int(match.group(1))
            data_url = stored.get(index)
            if not data_url:
                missing.append("대표 이미지" if index == -1 else f"{index + 1}번째")
                return match.group(0)
            return data_url

        return PLACEHOLDER_IN_TEXT.sub(swap, text)

    async def create(self, task: BlogTask) -> BlogTask:
        try:
            await self._collection.insert_one(task.to_wire())
        except DuplicateKeyError:
            raise BlogTaskError(
                "DUPLICATE_POST_ID", f"blogTask {task.post_id} already exists"
            ) from None
        return task

    async def find_by_post_id(self, post_id: str) -> BlogTask | None:
        document = await self._attach_images(
            strip_id(await self._collection.find_one({"postId": post_id}, WITHOUT_UNUSED_FIELDS))
        )
        return _to_task(document) if document else None

    async def summaries_by_post_ids(self, post_ids: list[str]) -> dict[str, PostSummary]:
        """여러 글의 상태·제목·발행 주소·진행 칸을 한 번에.

        예약 목록이 쓴다. 그 화면은 작업(ScheduledJob)의 상태만 알고 있었는데, 그것은
        **작업이 끝났을 때의 마지막 기억**일 뿐 글이 지금 어떤 상태인지가 아니다
        (`PostSummary` 참고).

        **원고 본문과 이미지는 가져오지 않는다.** 예약 목록은 2초마다 폴링되므로,
        여기서 글을 통째로 읽으면 그 비용이 그대로 서버를 누른다.
        """
        if not post_ids:
            return {}
        cursor = self._collection.find(
            {"postId": {"$in": post_ids}},
            {
                "_id": 0,
                "postId": 1,
                "status": 1,
                "finalPost.title": 1,
                "postingLogs.postUrl": 1,
                "progress": 1,
            },
        )
        summaries: dict[str, PostSummary] = {}
        async for document in cursor:
            summaries[document["postId"]] = _summary_from_document(document)
        return summaries

    async def owns_post(self, user_id: str, post_id: str) -> bool:
        """이 사용자의 글인가. **문서를 끌어오지 않는다.**

        `/posts` 아래 모든 동작 앞에 붙는 검사다(10개 라우트). 예전에는 여기서 글을
        통째로 읽고 버렸는데, 한 편이 몇 MB라 '선택 삭제'로 24편을 지우면 수십 MB가
        오가느라 끝나지 않았다. 있는지만 묻고 `_id` 하나만 받는다.
        """
        found = await self._collection.find_one(
            {"userId": user_id, "postId": post_id}, {"_id": 1}
        )
        return found is not None

    async def find_by_user_and_post_id(
        self, user_id: str, post_id: str
    ) -> BlogTask | None:
        document = await self._attach_images(
            strip_id(await self._collection.find_one({"userId": user_id, "postId": post_id}, WITHOUT_UNUSED_FIELDS))
        )
        return _to_task(document) if document else None

    async def find_status_by_user_and_post_id(
        self, user_id: str, post_id: str
    ) -> BlogTaskStatusSnapshot | None:
        document = strip_id(
            await self._collection.find_one(
                {"userId": user_id, "postId": post_id}, BLOG_TASK_STATUS_PROJECTION
            )
        )
        return _status_snapshot_from_document(document) if document else None

    async def list_by_user_id(self, user_id: str) -> list[BlogTask]:
        """글 전체를 그대로 돌려준다(``GET /posts?view=full``).

        **무겁다.** 화면은 이 경로를 쓰지 않고 ``list_items_by_user_id``(프로젝션)를 쓴다.
        남겨 둔 이유는 옛 호출과의 계약 때문이다.

        이미지를 붙이는 것은 선택이 아니다. 안 붙이면 ``dataUrl`` 자리에 표시 문자열이
        그대로 나가, 받는 쪽이 그것을 이미지 주소로 알고 쓴다 — 화면에는 깨진 이미지가
        뜨고 아무도 이유를 모른다. 글마다 따로 묻지 않고 **한 번에 모아 온다.**
        """
        cursor = self._collection.find({"userId": user_id}, WITHOUT_UNUSED_FIELDS).sort("createdAt", -1)
        documents = [strip_id(doc) async for doc in cursor]
        return [_to_task(doc) for doc in await self._attach_many(documents)]

    async def list_published_digests(
        self, user_id: str, limit: int = 30, exclude_post_id: str | None = None
    ) -> list[PostDigest]:
        """중복 검사용 요약 — 제목·소제목·도입부만. 본문 전체는 들고 오지 않는다.

        원고가 있는 글만 본다(finalPost). 발행까지 갔는지는 따지지 않는다 — 만들어 둔
        글끼리 닮는 것도 같은 문제이기 때문이다.
        """
        query: dict = {"userId": user_id, "finalPost": {"$ne": None}}
        if exclude_post_id:
            query["postId"] = {"$ne": exclude_post_id}
        cursor = (
            self._collection.find(
                query,
                {
                    "postId": 1,
                    "finalPost.title": 1,
                    "finalPost.markdownContent": 1,
                    "finalPost.body": 1,
                },
            )
            .sort("createdAt", -1)
            .limit(limit)
        )
        return [_digest_from_document(document) async for document in cursor]

    async def list_items_by_user_id(self, user_id: str) -> list[BlogTaskListItem]:
        cursor = self._collection.find(
            {"userId": user_id}, BLOG_TASK_LIST_ITEM_PROJECTION
        ).sort("createdAt", -1)
        return [_list_item_from_document(doc) async for doc in cursor]

    async def list_by_status(self, statuses: list[BlogTaskStatus]) -> list[BlogTask]:
        """상태로 훑는다. 시작 시 '진행 중인 채로 멈춘' 글을 찾는 복구 스위퍼가 쓴다.

        여기서도 이미지를 붙인다. 스위퍼가 찾아낸 글은 **이어서 발행되는** 글이다 —
        안 붙이면 표시 문자열이 이미지 주소로 발행에 넘어간다.
        """
        cursor = self._collection.find(
            {"status": {"$in": [s.value for s in statuses]}}, WITHOUT_UNUSED_FIELDS
        )
        documents = [strip_id(doc) async for doc in cursor]
        return [_to_task(doc) for doc in await self._attach_many(documents)]

    async def delete_by_post_id(self, post_id: str) -> None:
        # 이미지가 옆 컬렉션으로 나갔으므로 함께 지운다. 안 지우면 주인 없는 이미지가
        # 쌓여, 지운 글의 이미지가 컬렉션 크기를 계속 차지한다.
        await self._images.delete_many({"postId": post_id})
        result = await self._collection.delete_one({"postId": post_id})
        if result.deleted_count == 0:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")

    async def delete_by_user_and_post_id(self, user_id: str, post_id: str) -> None:
        result = await self._collection.delete_one({"userId": user_id, "postId": post_id})
        if result.deleted_count == 0:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        # 글이 지워진 뒤에 이미지를 지운다. 순서가 반대면, 글 주인이 아니어서 지우지
        # 못하는 경우에도 남의 글 이미지를 먼저 지워 버린다.
        await self._images.delete_many({"postId": post_id})

    async def _require_task(self, post_id: str) -> BlogTask:
        task = await self.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        return task

    async def _apply(self, current: BlogTask, update: dict[str, Any]) -> BlogTask:
        """버전을 지키는 쓰기. 동시 갱신이 버전을 올리면 필터가 빗나가고, 호출자는 잃어버린
        쓰기 대신 충돌을 보게 된다.

        돌려주는 글에는 이미지를 붙인다. 저장된 문서에는 표시만 있으므로, 붙이지 않으면
        **상태를 바꾼 뒤 받은 글로 발행하는 곳**에서 표시 문자열이 이미지 주소로 넘어간다
        (`transition_status`가 돌려준 글을 그대로 쓰는 호출자가 있다).
        """
        document = await self._collection.find_one_and_update(
            {"postId": current.post_id, "version": current.version},
            update,
            projection=WITHOUT_UNUSED_FIELDS,
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"blogTask {current.post_id} was updated concurrently",
            )
        attached = await self._attach_images(strip_id(document))
        return _to_task(attached)  # type: ignore[arg-type]  # 위에서 None이 아님이 보장된다

    async def _transition(
        self,
        post_id: str,
        next_status: BlogTaskStatus,
        actor: str,
        extra_set: dict[str, Any] | None = None,
        extra_unset: Sequence[str] = (),
    ) -> BlogTask:
        current = await self._require_task(post_id)
        _require_transition(current, next_status)

        now = _now()
        history = StatusHistoryEntry(
            **{"from": current.status, "to": next_status, "at": now, "by": actor}
        )
        update: dict[str, Any] = {
            "$set": {"status": next_status.value, "updatedAt": now, **(extra_set or {})},
            "$inc": {"version": 1},
            "$push": {"statusHistory": history.to_wire()},
        }
        if extra_unset:
            update["$unset"] = {field: "" for field in extra_unset}
        return await self._apply(current, update)

    async def transition_status(
        self, post_id: str, next_status: BlogTaskStatus, actor: str
    ) -> BlogTask:
        return await self._transition(post_id, next_status, actor)

    async def append_posting_log(self, post_id: str, log: PostingLog) -> BlogTask:
        current = await self._require_task(post_id)
        return await self._apply(
            current,
            {
                "$set": {"updatedAt": _now()},
                "$inc": {"version": 1},
                "$push": {"postingLogs": log.to_wire()},
            },
        )

    async def replace_input(
        self, post_id: str, blog_input: BlogTaskInput, actor: str
    ) -> BlogTask:
        current = await self._require_task(post_id)

        now = _now()
        history = StatusHistoryEntry(
            **{"from": current.status, "to": BlogTaskStatus.INPUT, "at": now, "by": actor}
        )
        return await self._apply(
            current,
            {
                "$set": {
                    "status": BlogTaskStatus.INPUT.value,
                    "input": blog_input.to_wire(),
                    "updatedAt": now,
                },
                "$unset": {field: "" for field in DERIVED_FIELDS},
                "$inc": {"version": 1},
                "$push": {"statusHistory": history.to_wire()},
            },
        )

    async def update_progress(self, post_id: str, progress: TaskProgress | None) -> None:
        # 일부러 버전 가드 밖에 둔다: 진행 상황은 백그라운드 잡이 실제 쓰기를 하는 동안
        # 같은 잡에서 기록하는데, 여기서 버전을 올리면 그 쓰기들이 자기 자신과 충돌한다.
        await self._collection.update_one(
            {"postId": post_id},
            {"$set": {"progress": progress.to_wire()}}
            if progress
            else {"$unset": {"progress": ""}},
        )

    async def save_title_plan(self, post_id: str, plan: TitlePlan) -> None:
        """확정된 제목 계획을 기록한다.

        update_progress와 같은 이유로 버전 가드 밖에 둔다: 이 쓰기는 의도 선택 직후의
        백그라운드 선행 생성이 하는데, 버전을 올리면 같은 시점에 도는 다른 쓰기와 충돌한다.
        상태 전이도 히스토리도 없다 — 사용자 상태가 아니라 입력에서 파생된 값이다.
        """
        await self._collection.update_one(
            {"postId": post_id}, {"$set": {"titlePlan": plan.to_wire()}}
        )

    async def save_seo_keyword_plan(self, post_id: str, plan: SeoKeywordPlan) -> None:
        """확정된 SEO 키워드 계획을 기록한다.

        save_title_plan과 같은 이유로 버전 가드 밖에 둔다: 입력에서 파생된 값이라 상태 전이도
        히스토리도 없고, 원고 생성 흐름 안에서 추가 필드로만 저장한다(기존 데이터 호환)."""
        await self._collection.update_one(
            {"postId": post_id}, {"$set": {"seoKeywordPlan": plan.to_wire()}}
        )

    async def save_draft_checkpoint(
        self, post_id: str, checkpoint: DraftCheckpoint
    ) -> None:
        """원고 중간 저장점을 기록한다.

        save_title_plan과 같은 이유로 버전 가드 밖이다 — 생성 잡 자신이 도중에 쓰는 파생
        값이라 상태 전이도 히스토리도 없다. BlogTask 모델에는 없는 문서 전용 필드라서
        조회 응답에 실리지 않고, 글이 삭제되면 문서와 함께 사라진다.
        """
        await self._collection.update_one(
            {"postId": post_id}, {"$set": {"draftCheckpoint": checkpoint.to_wire()}}
        )

    async def load_draft_checkpoint(self, post_id: str) -> DraftCheckpoint | None:
        document = await self._collection.find_one(
            {"postId": post_id}, {"draftCheckpoint": 1}
        )
        raw = (document or {}).get("draftCheckpoint")
        if not raw:
            return None
        try:
            return DraftCheckpoint.model_validate(raw)
        except Exception:
            # 스키마가 바뀐 옛 저장점은 재개 근거가 못 된다 — 없는 것으로 취급한다.
            return None

    async def clear_draft_checkpoint(self, post_id: str) -> None:
        await self._collection.update_one(
            {"postId": post_id}, {"$unset": {"draftCheckpoint": ""}}
        )

    async def rewind_status(
        self, post_id: str, to_status: BlogTaskStatus, actor: str
    ) -> BlogTask:
        """태스크를 뒤로 되감는다. 상태 머신에는 이런 간선이 없다.

        실패한 원고가 죽은 글은 아니다 — 모델이 타임아웃했을 뿐, 같은 입력이면 두 번째
        시도에서 잘 만들어 낸다. FAILED는 설계상 종착 상태라 거기서 빠져나오는 건 전이일
        수 없다. 이것은 개정이고, 상태 히스토리에도 그렇게 기록한다.
        """
        current = await self._require_task(post_id)

        now = _now()
        history = StatusHistoryEntry(
            **{"from": current.status, "to": to_status, "at": now, "by": actor}
        )
        return await self._apply(
            current,
            {
                "$set": {"status": to_status.value, "updatedAt": now},
                "$unset": {"progress": ""},
                "$inc": {"version": 1},
                "$push": {"statusHistory": history.to_wire()},
            },
        )

    async def save_intent_validation_result(
        self, post_id: str, result: IntentValidationResult
    ) -> BlogTask:
        current = await self._require_task(post_id)
        return await self._apply(
            current,
            {
                "$set": {"intentValidationResult": result.to_wire(), "updatedAt": _now()},
                "$inc": {"version": 1},
            },
        )

    async def select_intent(
        self, post_id: str, intent_id: str, actor: str, excluded_source_urls: Sequence[str] = ()
    ) -> BlogTask:
        current = await self._require_task(post_id)
        selected = _selected_intent_from(current, intent_id, excluded_source_urls)
        return await self._transition(
            post_id,
            BlogTaskStatus.INTENT_SELECTED,
            actor,
            {"selectedIntent": selected.to_wire()},
        )

    async def replace_selected_intent_sources(
        self, post_id: str, sources: Sequence[SearchSource], actor: str
    ) -> BlogTask:
        """고른 방향은 그대로 두고 **자료만** 갈아끼운다(2026-08-11 예약 경로).

        상태를 옮기지 않는다 — 글은 이미 INTENT_SELECTED이고, 여기서 바뀌는 것은 그
        방향이 들고 있던 근거뿐이다. 방향(제목·독자·논지·키워드)은 사람이 고른 판단이라
        며칠 뒤에도 유효하지만 자료는 낡는다.
        """
        current = await self._require_task(post_id)
        if current.selected_intent is None:
            raise BlogTaskError(
                "VALIDATION_FAILED", f"blogTask {post_id} has no selected intent"
            )
        updated = current.selected_intent.model_copy(update={"sources": list(sources)})
        return await self._apply(
            current,
            {
                "$set": {"selectedIntent": updated.to_wire(), "updatedAt": _now()},
                "$inc": {"version": 1},
            },
        )

    async def save_trend_selection(
        self, post_id: str, selection: TrendSelection, actor: str
    ) -> BlogTask:
        return await self._transition(
            post_id,
            BlogTaskStatus.SEARCH_ANALYZING,
            actor,
            {"trendSelection": selection.to_wire()},
            # 제목을 다시 고르면 여기로 다시 들어온다(자기 간선). 옛 제목으로 만든 검증
            # 결과와 그 진행 표시는 새 제목에 대해 아무것도 말해 주지 않으므로 함께
            # 버린다 — 남겨 두면 검증 팝업이 옛 제목의 방향·자료를 새 제목의 것인 양
            # 보여주고, 폴링은 그 옛 결과를 보고 검증이 끝났다고 판단한다.
            extra_unset=("intentValidationResult", "progress"),
        )

    async def save_draft_generation_result(
        self, post_id: str, result: DraftGenerationResult, actor: str
    ) -> BlogTask:
        light, rows = _split_images(result.final_post.to_wire(), post_id)
        # 이미지를 먼저 넣는다. 글 문서가 가리키는 이미지가 아직 없는 순간을 만들지 않는다.
        await self._store_images(post_id, rows)
        return await self._transition(
            post_id,
            BlogTaskStatus.READY_TO_PUBLISH,
            actor,
            {
                # 원고는 한 벌만 쓴다(_without_duplicate_final_post 참고).
                "draftGenerationResult": _without_duplicate_final_post(result.to_wire()),
                "finalPost": light,
            },
        )

    async def update_final_post(
        self, post_id: str, result: DraftGenerationResult, actor: str
    ) -> BlogTask:
        # 기존 원고의 스타일만 다시 입힌다. 상태 변화가 없으니 히스토리 항목도 없다 —
        # 하지만 버전은 여전히 올라가므로 동시 쓰기는 그대로 잡힌다.
        current = await self._require_task(post_id)
        if current.status != BlogTaskStatus.READY_TO_PUBLISH:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"cannot update finalPost for blogTask {post_id} in {current.status.value}",
            )
        light, rows = _split_images(result.final_post.to_wire(), post_id)
        await self._store_images(post_id, rows)
        return await self._apply(
            current,
            {
                "$set": {
                    "draftGenerationResult": _without_duplicate_final_post(result.to_wire()),
                    "finalPost": light,
                    "updatedAt": _now(),
                },
                "$inc": {"version": 1},
            },
        )


class InMemoryBlogTaskRepository:
    """동시성 가드 없음 — 단일 이벤트 루프에 기댔던 원본을 그대로 따른다. 테스트가
    검증할 수 있도록 버전은 여전히 증가시킨다."""

    def __init__(self) -> None:
        self._by_post_id: dict[str, BlogTask] = {}
        # 원고 중간 저장점. Mongo에서는 blogTask 문서의 필드지만 BlogTask 모델에는 없으므로
        # 여기서도 태스크 옆에 따로 둔다(글 삭제 시 함께 지운다).
        self._draft_checkpoints: dict[str, DraftCheckpoint] = {}

    async def create(self, task: BlogTask) -> BlogTask:
        if task.post_id in self._by_post_id:
            raise BlogTaskError("DUPLICATE_POST_ID", f"blogTask {task.post_id} already exists")
        self._by_post_id[task.post_id] = task
        return task

    async def find_by_post_id(self, post_id: str) -> BlogTask | None:
        return self._by_post_id.get(post_id)

    async def summaries_by_post_ids(self, post_ids: list[str]) -> dict[str, PostSummary]:
        summaries: dict[str, PostSummary] = {}
        for post_id in post_ids:
            task = self._by_post_id.get(post_id)
            if task is None:
                continue
            summaries[post_id] = PostSummary(
                post_id=task.post_id,
                status=task.status,
                title=((task.final_post.title if task.final_post else "") or "").strip() or None,
                published_url=_first_post_url(task.posting_logs),
                progress=task.progress,
            )
        return summaries

    async def owns_post(self, user_id: str, post_id: str) -> bool:
        task = self._by_post_id.get(post_id)
        return task is not None and task.user_id == user_id

    async def find_by_user_and_post_id(
        self, user_id: str, post_id: str
    ) -> BlogTask | None:
        task = self._by_post_id.get(post_id)
        return task if task and task.user_id == user_id else None

    async def find_status_by_user_and_post_id(
        self, user_id: str, post_id: str
    ) -> BlogTaskStatusSnapshot | None:
        task = self._by_post_id.get(post_id)
        if task is None or task.user_id != user_id:
            return None
        return _status_snapshot_from_task(task)

    async def list_by_user_id(self, user_id: str) -> list[BlogTask]:
        tasks = [t for t in self._by_post_id.values() if t.user_id == user_id]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    async def list_items_by_user_id(self, user_id: str) -> list[BlogTaskListItem]:
        tasks = await self.list_by_user_id(user_id)
        return [_list_item_from_task(task) for task in tasks]

    async def list_published_digests(
        self, user_id: str, limit: int = 30, exclude_post_id: str | None = None
    ) -> list[PostDigest]:
        digests = []
        for task in await self.list_by_user_id(user_id):
            if task.final_post is None or task.post_id == exclude_post_id:
                continue
            markdown = task.final_post.markdown_content or task.final_post.body or ""
            digests.append(
                PostDigest(
                    post_id=task.post_id,
                    title=task.final_post.title or "",
                    headings=extract_headings(markdown),
                    opening=first_paragraph(markdown),
                )
            )
        return digests[:limit]

    async def list_by_status(self, statuses: list[BlogTaskStatus]) -> list[BlogTask]:
        wanted = set(statuses)
        return [t for t in self._by_post_id.values() if t.status in wanted]

    async def delete_by_post_id(self, post_id: str) -> None:
        if self._by_post_id.pop(post_id, None) is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        self._draft_checkpoints.pop(post_id, None)

    async def delete_by_user_and_post_id(self, user_id: str, post_id: str) -> None:
        task = await self.find_by_user_and_post_id(user_id, post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        del self._by_post_id[post_id]
        self._draft_checkpoints.pop(post_id, None)

    async def _require_task(self, post_id: str) -> BlogTask:
        task = self._by_post_id.get(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        return task

    def _store(self, task: BlogTask) -> BlogTask:
        self._by_post_id[task.post_id] = task
        return task

    async def _transition(
        self,
        post_id: str,
        next_status: BlogTaskStatus,
        actor: str,
        updates: dict[str, Any] | None = None,
    ) -> BlogTask:
        current = await self._require_task(post_id)
        _require_transition(current, next_status)

        now = _now()
        history = StatusHistoryEntry(
            **{"from": current.status, "to": next_status, "at": now, "by": actor}
        )
        return self._store(
            current.model_copy(
                update={
                    "status": next_status,
                    "updated_at": now,
                    "version": current.version + 1,
                    "status_history": [*current.status_history, history],
                    **(updates or {}),
                }
            )
        )

    async def transition_status(
        self, post_id: str, next_status: BlogTaskStatus, actor: str
    ) -> BlogTask:
        return await self._transition(post_id, next_status, actor)

    async def append_posting_log(self, post_id: str, log: PostingLog) -> BlogTask:
        current = await self._require_task(post_id)
        return self._store(
            current.model_copy(
                update={
                    "updated_at": _now(),
                    "version": current.version + 1,
                    "posting_logs": [*current.posting_logs, log],
                }
            )
        )

    async def replace_input(
        self, post_id: str, blog_input: BlogTaskInput, actor: str
    ) -> BlogTask:
        current = await self._require_task(post_id)

        now = _now()
        history = StatusHistoryEntry(
            **{"from": current.status, "to": BlogTaskStatus.INPUT, "at": now, "by": actor}
        )
        return self._store(
            current.model_copy(
                update={
                    "status": BlogTaskStatus.INPUT,
                    "input": blog_input,
                    "updated_at": now,
                    "version": current.version + 1,
                    "status_history": [*current.status_history, history],
                    "trend_selection": None,
                    "intent_validation_result": None,
                    "selected_intent": None,
                    "title_plan": None,
                    "seo_keyword_plan": None,
                    "draft_generation_result": None,
                    "progress": None,
                }
            )
        )

    async def update_progress(self, post_id: str, progress: TaskProgress | None) -> None:
        current = await self._require_task(post_id)
        # Mongo와 맞춰 버전은 올리지 않는다: 진행 상황은 참고용이다.
        self._store(current.model_copy(update={"progress": progress}))

    async def save_title_plan(self, post_id: str, plan: TitlePlan) -> None:
        current = await self._require_task(post_id)
        # Mongo와 맞춰 버전은 올리지 않는다(선행 생성이 백그라운드에서 쓴다).
        self._store(current.model_copy(update={"title_plan": plan}))

    async def save_seo_keyword_plan(self, post_id: str, plan: SeoKeywordPlan) -> None:
        current = await self._require_task(post_id)
        # Mongo와 맞춰 버전은 올리지 않는다(입력에서 파생된 값이다).
        self._store(current.model_copy(update={"seo_keyword_plan": plan}))

    async def save_draft_checkpoint(
        self, post_id: str, checkpoint: DraftCheckpoint
    ) -> None:
        await self._require_task(post_id)
        # BlogTask 모델 밖의 문서 전용 필드 — Mongo와 맞춰 따로 보관한다.
        self._draft_checkpoints[post_id] = checkpoint

    async def load_draft_checkpoint(self, post_id: str) -> DraftCheckpoint | None:
        return self._draft_checkpoints.get(post_id)

    async def clear_draft_checkpoint(self, post_id: str) -> None:
        self._draft_checkpoints.pop(post_id, None)

    async def rewind_status(
        self, post_id: str, to_status: BlogTaskStatus, actor: str
    ) -> BlogTask:
        current = await self._require_task(post_id)

        now = _now()
        history = StatusHistoryEntry(
            **{"from": current.status, "to": to_status, "at": now, "by": actor}
        )
        return self._store(
            current.model_copy(
                update={
                    "status": to_status,
                    "updated_at": now,
                    "version": current.version + 1,
                    "status_history": [*current.status_history, history],
                    "progress": None,
                }
            )
        )

    async def save_intent_validation_result(
        self, post_id: str, result: IntentValidationResult
    ) -> BlogTask:
        current = await self._require_task(post_id)
        return self._store(
            current.model_copy(
                update={
                    "updated_at": _now(),
                    "version": current.version + 1,
                    "intent_validation_result": result,
                }
            )
        )

    async def select_intent(
        self, post_id: str, intent_id: str, actor: str, excluded_source_urls: Sequence[str] = ()
    ) -> BlogTask:
        current = await self._require_task(post_id)
        selected = _selected_intent_from(current, intent_id, excluded_source_urls)
        return await self._transition(
            post_id, BlogTaskStatus.INTENT_SELECTED, actor, {"selected_intent": selected}
        )

    async def replace_selected_intent_sources(
        self, post_id: str, sources: Sequence[SearchSource], actor: str
    ) -> BlogTask:
        """Mongo 구현과 같은 계약 — 방향은 그대로, 자료만 갈아끼운다."""
        current = await self._require_task(post_id)
        if current.selected_intent is None:
            raise BlogTaskError(
                "VALIDATION_FAILED", f"blogTask {post_id} has no selected intent"
            )
        return self._store(
            current.model_copy(
                update={
                    "selected_intent": current.selected_intent.model_copy(
                        update={"sources": list(sources)}
                    ),
                    "updated_at": _now(),
                    "version": current.version + 1,
                }
            )
        )

    async def save_trend_selection(
        self, post_id: str, selection: TrendSelection, actor: str
    ) -> BlogTask:
        # Mongo 구현과 같은 이유로 옛 검증 결과·진행 표시를 함께 지운다.
        return await self._transition(
            post_id,
            BlogTaskStatus.SEARCH_ANALYZING,
            actor,
            {
                "trend_selection": selection,
                "intent_validation_result": None,
                "progress": None,
            },
        )

    async def save_draft_generation_result(
        self, post_id: str, result: DraftGenerationResult, actor: str
    ) -> BlogTask:
        return await self._transition(
            post_id,
            BlogTaskStatus.READY_TO_PUBLISH,
            actor,
            {"draft_generation_result": result, "final_post": result.final_post},
        )

    async def update_final_post(
        self, post_id: str, result: DraftGenerationResult, actor: str
    ) -> BlogTask:
        current = await self._require_task(post_id)
        if current.status != BlogTaskStatus.READY_TO_PUBLISH:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"cannot update finalPost for blogTask {post_id} in {current.status.value}",
            )
        return self._store(
            current.model_copy(
                update={
                    "updated_at": _now(),
                    "version": current.version + 1,
                    "draft_generation_result": result,
                    "final_post": result.final_post,
                }
            )
        )
