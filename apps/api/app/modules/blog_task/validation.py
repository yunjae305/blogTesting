"""blogTask 요청 본문 검증."""

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from pydantic import ValidationError

from app.errors import BlogTaskError
from app.shared import (
    BRAND_FIT_GRADES,
    BrandClosing,
    BRAND_MATERIAL_ORIGIN,
    BRAND_MODES,
    SUBJECT_CATEGORIES,
    BlogTaskInput,
    ReferenceMaterial,
    ReferenceMaterialType,
)
from app.shared.intent import IntentCandidate
from app.shared.image_bytes import shrink, to_data_url
from app.shared.reference_url import is_public_reference_url

MAX_KEYWORDS = 10
#: 작성 화면에서 **사용자가 직접 넣는** 참고자료의 상한.
#:
#: 서버가 조립해 넣는 자료(브랜드 글의 브랜드 자료)는 이 수를 넘을 수 있어서, 호출하는
#: 쪽이 상한을 따로 줄 수 있게 해 두었다. 브랜드 자료는 자기 화면에서 이미 개수가
#: 묶여 있다(주소 20 · 문서 5 · 이미지 10) — 여기서 10으로 다시 자르면 저장해 둔
#: 자료가 통째로 거절된다.
# 개수 제한을 두지 않는다(2026-08-11 사용자 요청). 실제 한계는 용량이고 그것은 파일
# 상한(20MB)이 지킨다. 화면의 constants.MAX_REFERENCE_MATERIALS와 같은 값이어야 한다.
MAX_REFERENCE_MATERIALS = 1000
MAX_TEXT_CHARS = 16_000
# 참고 파일 상한(2026-08-11 5/10MB → 20/20MB로 올렸다가 **10/10MB로 되돌렸다**).
#
# 되돌린 이유 — 20MB는 지킬 수 없는 약속이었다. 실측(2026-08-11):
#
#     파일 10MB → 문서 13.6MB  통과
#     파일 11MB → 문서 15.0MB  통과
#     파일 12MB → 문서 16.3MB  **Mongo가 거부**
#
# 이미지·PDF는 글 문서 안에 base64 data URL로 들어가고 base64는 원본의 약 4/3배다.
# Mongo 문서 한계가 16MB이므로 **12MB짜리 파일 하나로 글 전체가 저장되지 않는다.**
# 그때 사용자가 보는 것은 "20MB까지 됩니다"라고 안내받고 한참 업로드한 뒤의 저장 실패다.
#
# 앞선 주석은 "PDF는 텍스트만 뽑아 담으니 원본이 커도 된다"고 적었는데 **사실이 아니었다.**
# 텍스트 추출(prompts.py의 material_text)은 프롬프트를 만들 때 일어나고, 문서에는 원본
# base64가 그대로 남는다. 저장 전에 줄이는 것은 `shrink`뿐이고 그건 **이미지만** 본다.
#
# 10MB로 잡은 이유: 11MB도 통과하지만 남는 여유가 1MB뿐이다. 같은 문서에 원고·자료·
# 이미지가 함께 살기 때문에 그 1MB는 글이 길어지면 사라진다. 10MB면 2.4MB가 남는다.
#
# 이미지는 사실 더 커도 된다(저장 전에 900px로 줄인다). 그래도 한 숫자로 두는 이유는
# 화면에서 "이미지는 20MB, PDF는 10MB"를 설명하는 비용이 얻는 것보다 크기 때문이다.
#
# **이 상한을 다시 올리려면** 파일을 글 문서에서 分離해야 한다(GridFS 등). 그 전에는
# 12MB가 물리적 천장이다.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 10 * 1024 * 1024
#: 한 소재로 한 번에 만들 수 있는 원고 수(2026-08-12). 화면의 MAX_DRAFT_COUNT와
#: 같은 값이어야 한다 — 어긋나면 화면은 통과시키고 서버가 거부한다.
MAX_DRAFT_COUNT = 3
MAX_TOPIC_CHARS = 300
MAX_OPTIONAL_TEXT_CHARS = 1_000
MAX_URL_CHARS = 2_048
MAX_LIST_ITEM_CHARS = 200
MAX_USER_ID_CHARS = 200
VALID_REFERENCE_TYPES = [t.value for t in ReferenceMaterialType]
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/=]+)$", re.IGNORECASE)


def _is_allowed_file_mime(material_type: str, mime: str) -> bool:
    """참고자료 파일의 mime 허용 여부. 이미지는 형식을 가리지 않고 모두 받는다 — Anthropic이
    못 받는 형식은 전송 직전에 PNG로 변환하므로(live_adapters/imaging.prepare_anthropic_image),
    여기서는 image/* 이기만 하면 통과시킨다. PDF는 application/pdf만."""
    mime = mime.lower()
    if material_type == ReferenceMaterialType.IMAGE.value:
        return mime.startswith("image/")
    if material_type == ReferenceMaterialType.PDF.value:
        return mime == "application/pdf"
    return False

# JavaScript의 `new URL()`도 호스트를 요구하는 스킴. 그래서 "https://"만 있으면
# 원본과 똑같이 거부된다.
@dataclass
class CreateBlogTaskRequest:
    user_id: str
    input: BlogTaskInput


def _is_valid_url(value: str) -> bool:
    return is_public_reference_url(value)


def validate_string_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{field} must be a non-empty array of non-empty strings"
        )
    normalized = [item.strip() for item in value]
    if any(len(item) > MAX_LIST_ITEM_CHARS for item in normalized):
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{field} items must be at most {MAX_LIST_ITEM_CHARS} characters"
        )
    return normalized


def validate_optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{field} must be a non-empty string when provided"
        )
    normalized = value.strip()
    if len(normalized) > MAX_OPTIONAL_TEXT_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{field} must be at most {MAX_OPTIONAL_TEXT_CHARS} characters"
        )
    return normalized


def _decode_file_data_url(value: str, material_type: str, index: int) -> int:
    match = _DATA_URL.fullmatch(value.strip())
    if match is None or not _is_allowed_file_mime(material_type, match.group(1)):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"referenceMaterials[{index}].value must be a supported base64 data URL",
        )
    encoded = match.group(2)
    if len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4 + 4:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"referenceMaterials[{index}] must be at most {MAX_FILE_BYTES // (1024 * 1024)}MB",
        )
    try:
        size = len(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError):
        raise BlogTaskError(
            "VALIDATION_FAILED", f"referenceMaterials[{index}].value has invalid base64 data"
        ) from None
    if size > MAX_FILE_BYTES:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"referenceMaterials[{index}] must be at most {MAX_FILE_BYTES // (1024 * 1024)}MB",
        )
    return size


def _shrunk_image(value: str) -> str:
    """올린 참고 이미지를 저장할 크기로 줄인다.

    참고 이미지는 글 문서 안에 base64로 들어간다. 실측(2026-08-06)에서 이미지 9장이
    **2.11MB**였고, 그 글은 회선 0.09MB/s에서 20초 제한을 넘겨 **다시 열리지 않았다.**
    원고 이미지에 한 것과 같은 처리다(`app/shared/image_bytes.py`).

    모델에 보내는 값도 함께 작아진다 — 첨부물로 그대로 넘어가기 때문이다. 900px면
    사진에서 읽어낼 것은 그대로 남는다.

    **줄이지 못하면 있는 그대로 둔다.** 못 여는 형식이거나 이미 작으면 손대지 않는다.
    """
    raw, mime = shrink(value)
    return to_data_url(raw, mime) if raw else value


def _validate_reference_material(item: Any, index: int) -> ReferenceMaterial:
    if not isinstance(item, dict):
        raise BlogTaskError("VALIDATION_FAILED", f"referenceMaterials[{index}] must be an object")

    material_type = item.get("type")
    value = item.get("value")
    name = item.get("name")

    if not isinstance(material_type, str) or material_type not in VALID_REFERENCE_TYPES:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"referenceMaterials[{index}].type must be one of {', '.join(VALID_REFERENCE_TYPES)}",
        )
    if not isinstance(value, str) or not value.strip():
        raise BlogTaskError("VALIDATION_FAILED", f"referenceMaterials[{index}].value is required")
    normalized = value.strip()
    if material_type == ReferenceMaterialType.URL.value:
        if len(normalized) > MAX_URL_CHARS or not _is_valid_url(normalized):
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"referenceMaterials[{index}].value must be a public HTTP(S) URL without "
                "credentials or secret query parameters",
            )
    elif material_type == ReferenceMaterialType.TEXT.value:
        if len(normalized) > MAX_TEXT_CHARS:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"referenceMaterials[{index}].value must be at most {MAX_TEXT_CHARS} characters",
            )
    else:
        _decode_file_data_url(normalized, material_type, index)
        if material_type == ReferenceMaterialType.IMAGE.value:
            normalized = _shrunk_image(normalized)

    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 255):
        raise BlogTaskError(
            "VALIDATION_FAILED", f"referenceMaterials[{index}].name is invalid"
        )

    # origin은 **서버가 붙이는 표시**다(브랜드 자료). 아는 값 하나만 통과시키고 나머지는
    # 버린다 — 임의의 문자열이 저장 문서와 프롬프트로 새어 나가지 않게.
    #
    # 화면이 보낸 origin을 여기서 걸러 내지는 못한다(누가 보냈는지 알 수 없다). 그 일은
    # 라우트의 merge_brand_materials가 이미 한다: 들어온 목록에서 brand 표시를 **전부**
    # 걷어낸 뒤 실제 브랜드 자료에만 다시 붙인다. 그래서 여기 도달한 brand 표시는
    # 서버가 붙인 것뿐이다. 이 순서가 깨지면 표시가 신뢰를 잃으므로, 글을 만드는 경로를
    # 새로 낼 때는 반드시 merge_brand_materials를 거쳐야 한다.
    origin = item.get("origin")
    return ReferenceMaterial(
        type=ReferenceMaterialType(material_type),
        value=normalized,
        name=name.strip() if isinstance(name, str) else None,
        origin=BRAND_MATERIAL_ORIGIN if origin == BRAND_MATERIAL_ORIGIN else None,
    )


def _validate_reference_materials(
    value: Any, max_items: int = MAX_REFERENCE_MATERIALS
) -> list[ReferenceMaterial]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BlogTaskError("VALIDATION_FAILED", "referenceMaterials must be an array")
    if len(value) > max_items:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"referenceMaterials must have at most {max_items} items",
        )
    materials = [_validate_reference_material(item, index) for index, item in enumerate(value)]
    total_file_bytes = sum(
        _decode_file_data_url(material.value, material.type.value, index)
        for index, material in enumerate(materials)
        if material.type in {ReferenceMaterialType.IMAGE, ReferenceMaterialType.PDF}
    )
    if total_file_bytes > MAX_TOTAL_FILE_BYTES:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"referenceMaterials files must total at most {MAX_TOTAL_FILE_BYTES // (1024 * 1024)}MB",
        )
    return materials


#: 예약을 걸 수 있는 가장 먼 미래(일). 이보다 뒤는 사용자가 날짜를 잘못 고른 것으로 본다 —
#: 두 달 뒤 글은 지금 방향을 정해 둘 값어치가 없다.
MAX_SCHEDULE_AHEAD_DAYS = 60
#: 지금과 이만큼 안이면 '지금 만들기'와 다를 것이 없어 예약으로 받지 않는다(분).
MIN_SCHEDULE_AHEAD_MINUTES = 1


def validate_draft_count(value: Any) -> int:
    """만들 원고 수. 보내지 않으면 1이다 — 옛 화면과 옛 문서가 그대로 동작한다.

    2 이상이면 같은 소재로 서로 다른 방향의 원고를 여러 편 만든다. 상한을 두는 이유는
    한 소재에서 진짜로 다른 각도가 무한히 나오지 않기 때문이다(방향 후보가 4개다).
    """
    if value is None:
        return 1
    # ``True``는 int의 부분형이라 그냥 두면 1로 통과한다 — 화면이 실수로 보낸 값이다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise BlogTaskError("VALIDATION_FAILED", "draftCount must be an integer")
    if value < 1 or value > MAX_DRAFT_COUNT:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"draftCount must be between 1 and {MAX_DRAFT_COUNT}",
        )
    return value


def validate_scheduled_run_at(value: Any, *, now: datetime | None = None) -> str | None:
    """원고 작업 예약 시각. 없으면 None(예전 동작 — 지금 만든다).

    받는 형식은 UTC ISO 문자열이다. 시간대 표기가 없으면 UTC로 읽는다 — 서버가 임의로
    로컬 시간대를 붙이면 배포 환경에 따라 몇 시간씩 밀린다.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BlogTaskError(
            "VALIDATION_FAILED", "scheduledRunAt must be a non-empty string when provided"
        )
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise BlogTaskError(
            "VALIDATION_FAILED", "scheduledRunAt must be an ISO-8601 datetime"
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if parsed <= moment + timedelta(minutes=MIN_SCHEDULE_AHEAD_MINUTES):
        # 이 메시지는 **화면의 알림창에 그대로 뜬다.** 고르는 동안 그 시각이 지나 버리는
        # 일이 흔해서(2026-08-12 사용자 신고), 무엇을 해야 하는지까지 적는다.
        raise BlogTaskError(
            "VALIDATION_FAILED",
            "원고 작업 예정 시각이 이미 지났습니다. 지금보다 뒤의 시각으로 다시 골라 주세요.",
        )
    if parsed > moment + timedelta(days=MAX_SCHEDULE_AHEAD_DAYS):
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"원고 작업 예정 시각은 지금부터 {MAX_SCHEDULE_AHEAD_DAYS}일 안이어야 합니다.",
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def validate_blog_task_input(
    body: Any, max_reference_materials: int = MAX_REFERENCE_MATERIALS
) -> BlogTaskInput:
    """입력값만 검증한다 — 글 생성과 수정이 함께 쓰므로, 둘이 어긋나는 필드가 생길 수 없다.

    ``max_reference_materials``는 서버가 자료를 조립해 넣는 경로(브랜드 글)만 올려 잡는다.
    화면이 보내는 평범한 입력은 기본값 그대로다.
    """
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    topic = body.get("topic")
    tone = body.get("tone")

    if not isinstance(topic, str) or not topic.strip():
        raise BlogTaskError("VALIDATION_FAILED", "topic is required")
    if len(topic.strip()) > MAX_TOPIC_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"topic must be at most {MAX_TOPIC_CHARS} characters"
        )

    # `keywords`는 지금의 `purpose`에 해당하는 옛 이름이다. 둘 다 두 필드에 함께 들어가
    # 옛 클라이언트와 저장된 태스크가 계속 동작하도록 한다.
    purpose = validate_string_list(body.get("purpose"), "purpose")
    if purpose is None:
        purpose = validate_string_list(body.get("keywords"), "keywords")

    if not purpose:
        raise BlogTaskError(
            "VALIDATION_FAILED", "purpose must be a non-empty array of non-empty strings"
        )
    if len(purpose) > MAX_KEYWORDS:
        raise BlogTaskError("VALIDATION_FAILED", f"purpose must have at most {MAX_KEYWORDS} items")
    if tone is not None and (not isinstance(tone, str) or not tone.strip()):
        raise BlogTaskError("VALIDATION_FAILED", "tone must be a non-empty string when provided")

    # 소재 분야는 목록 안의 값만 받는다(2026-08-11). 자유 문자열을 받으면 프롬프트에
    # 그대로 실려 나가 모델이 뜻을 지어내고, 화면의 12개 버튼과도 어긋난다.
    # **보내지 않아도 된다** — 옛 클라이언트와 저장된 글은 없이 돌아간다.
    subject_category = validate_optional_string(
        body.get("subjectCategory"), "subjectCategory"
    )
    if subject_category is not None and subject_category not in SUBJECT_CATEGORIES:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"subjectCategory must be one of: {', '.join(SUBJECT_CATEGORIES)}",
        )

    # 원고 작업 예약 시각(2026-08-11). 보내지 않으면 예전 그대로 — 방향을 고르는 즉시
    # 원고를 만든다. 보냈다면 **미래의 시각이어야** 한다: 지난 시각을 받으면 예약이
    # 걸리자마자 도는 셈이라 '지금 만들기'와 구분이 사라진다.
    scheduled_run_at = validate_scheduled_run_at(body.get("scheduledRunAt"))
    draft_count = validate_draft_count(body.get("draftCount"))
    # 자동 발행. 플랫폼마다 따로 받는다(2026-08-12). 보내지 않으면 네이버만 켠다 —
    # 예전 그대로다. 옛 화면이 보내던 통짜 `autoPublish`도 그대로 받아 준다.
    def _flag(name: str, *, default: bool) -> bool:
        raw = body.get(name)
        if raw is None:
            return default
        if not isinstance(raw, bool):
            raise BlogTaskError("VALIDATION_FAILED", f"{name} must be a boolean")
        return raw

    legacy = _flag("autoPublish", default=True)
    auto_publish_naver = _flag("autoPublishNaver", default=legacy)
    auto_publish_threads = _flag("autoPublishThreads", default=False)
    # **올릴 곳은 하나 이상이어야 한다**(2026-08-13 사용자 지시). 예약 화면과 재예약은
    # 이미 같은 규칙이었고(scheduled_posting/validation.py), 새 글 작성만 비어 있었다 —
    # 그래서 아무 데도 올라가지 않는 글이 작업 큐에 서는 길이 열려 있었다.
    if not auto_publish_naver and not auto_publish_threads:
        raise BlogTaskError(
            "VALIDATION_FAILED", "발행할 플랫폼을 하나 이상 선택해 주세요."
        )

    return BlogTaskInput(
        topic=topic.strip(),
        subject=validate_optional_string(body.get("subject"), "subject"),
        subject_category=subject_category,
        # 브랜드 존재 확인은 라우트가 이미 했다(없으면 404로 끝난다). 여기서는 어느
        # 브랜드였는지 글에 남기기만 한다 — 소재 단계로 돌아왔을 때 화면이 읽는 값이다.
        brand_id=validate_optional_string(body.get("brandId"), "brandId"),
        # 이름은 **라우트가 확인한 브랜드에서 덮어써 넣은 값**이다. 화면이 보낸 것을
        # 그대로 믿으면 없는 브랜드 이름이 프롬프트에 실린다.
        brand_name=validate_optional_string(body.get("brandName"), "brandName"),
        # 브랜드가 이 글에서 맡는 역할(FOCUS·UTILITY). brand_name과 같이 **라우트가 정해
        # 넣은 값**이다 — 화면이 보낸 것을 그대로 믿으면 글의 성격이 뒤집힌다. 모르는
        # 값은 거부한다: 조용히 None으로 두면 프롬프트가 옛 규칙(브랜드=주인공)으로
        # 되돌아가, 트렌드 글이 통째로 브랜드 홍보글이 된다.
        brand_mode=_validate_brand_mode(body.get("brandMode")),
        # 결합 가능성(A·B·C)과 닿은 기준표 줄. 둘 다 라우트가 재어 넣은 값이다.
        brand_fit_grade=_validate_brand_fit_grade(body.get("brandFitGrade")),
        brand_use_cases=_validate_use_case_lines(body.get("brandUseCases")),
        brand_closing=_validate_brand_closing(body.get("brandClosing")),
        # 라우트가 브랜드 자료에서 베껴 넣은 값이다. 문자열 목록이 아니면 비운다.
        brand_hashtags=_validate_use_case_lines(body.get("brandHashtags")),
        purpose=purpose,
        keywords=purpose,
        tone=tone,
        target_reader=validate_optional_string(body.get("targetReader"), "targetReader"),
        reader_age_range=validate_optional_string(body.get("readerAgeRange"), "readerAgeRange"),
        reader_knowledge_level=validate_optional_string(
            body.get("readerKnowledgeLevel"), "readerKnowledgeLevel"
        ),
        reference_materials=_validate_reference_materials(
            body.get("referenceMaterials"), max_reference_materials
        ),
        draft_count=draft_count,
        auto_publish_naver=auto_publish_naver,
        auto_publish_threads=auto_publish_threads,
        scheduled_run_at=scheduled_run_at,
        scheduled_timezone=(
            validate_optional_string(body.get("scheduledTimezone"), "scheduledTimezone")
            if scheduled_run_at
            else None
        ),
    )


def _validate_brand_closing(value: Any):
    """글 맨 끝에 붙일 마무리. 라우트가 브랜드 자료에서 베껴 넣은 값이다.

    모양이 어긋나면 **조용히 버린다** — 마무리가 없어도 글은 온전하고, 반쪽짜리
    블록이 모든 글의 맨 끝에 붙는 쪽이 나쁘다. 실제 검증은 브랜드 저장에서 이미 했다.
    """
    if not isinstance(value, dict):
        return None
    try:
        return BrandClosing.model_validate(value)
    except ValidationError:
        return None


def _validate_brand_fit_grade(value: Any) -> str | None:
    """``brandFitGrade``를 확인한다(A·B·C). 없으면 None — 브랜드를 안 쓴 글이다."""
    grade = validate_optional_string(value, "brandFitGrade")
    if grade is None:
        return None
    if grade not in BRAND_FIT_GRADES:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"brandFitGrade must be one of {', '.join(BRAND_FIT_GRADES)}",
        )
    return grade


def _validate_use_case_lines(value: Any) -> list[str]:
    """닿은 기준표 줄들. 문자열 목록이 아니면 비운다 — 프롬프트에 실릴 글자다."""
    if not isinstance(value, list):
        return []
    return [line.strip() for line in value if isinstance(line, str) and line.strip()]


def _validate_brand_mode(value: Any) -> str | None:
    """``brandMode``를 확인한다. 없으면 None(브랜드를 안 쓴 글).

    옛 글에는 이 값이 없다. 읽는 쪽(프롬프트)이 "브랜드는 있는데 모드가 없다"를 FOCUS로
    다루므로, 여기서 기본값을 만들어 넣지 않는다 — 저장된 값이 곧 서버가 판단한 것이다.
    """
    mode = validate_optional_string(value, "brandMode")
    if mode is None:
        return None
    if mode not in BRAND_MODES:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"brandMode must be one of {', '.join(BRAND_MODES)}",
        )
    return mode


def validate_create_blog_task_request(
    body: Any, max_reference_materials: int = MAX_REFERENCE_MATERIALS
) -> CreateBlogTaskRequest:
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")

    user_id = body.get("userId")
    if not isinstance(user_id, str) or not user_id.strip():
        raise BlogTaskError("VALIDATION_FAILED", "userId is required")
    if len(user_id.strip()) > MAX_USER_ID_CHARS:
        raise BlogTaskError(
            "VALIDATION_FAILED", f"userId must be at most {MAX_USER_ID_CHARS} characters"
        )

    return CreateBlogTaskRequest(
        user_id=user_id.strip(),
        input=validate_blog_task_input(body, max_reference_materials),
    )


# 사용자가 검증 화면에서 체크 해제한 자료의 URL. 최종 원고에서 이 자료들은 빠진다.
# 클라이언트가 정하므로 상한을 둔다(선택 intent의 소스가 이보다 많을 일은 없다).
MAX_EXCLUDED_SOURCES = 30


@dataclass
class SelectIntentRequest:
    intent_id: str
    # 원고에서 제외할 자료 URL. 비어 있으면 검색된 자료를 모두 사용한다.
    excluded_source_urls: list[str]
    #: 2편 이상을 만들 때 **뒤이어 만들 글**의 방향(2026-08-12). 고른 차례가 만들어지는
    #: 차례다. 한 편이면 비어 있고, 그때는 예전과 완전히 같은 요청이다.
    additional_intent_ids: list[str] = field(default_factory=list)


def validate_chosen_intent(value: Any, *, where: str) -> IntentCandidate | None:
    """그 편에서 **실제로 고른 방향**(2026-08-12 사용자 신고로 추가).

    왜 방향 전체를 받는가: ``intentId``는 자리번호다(``{postId}_intent_{n}``). 여러 편을
    만들 때는 편마다 제목을 다시 골라 검증(M3)을 다시 돌리는데, 새 제목을 저장하는
    ``save_trend_selection``이 **옛 검증 결과를 지운다.** 그래서 마지막에 예약을 걸 시점의
    글에는 마지막 편의 후보만 남아 있고, 1·2편째의 자리번호는 **다른 방향**을 가리킨다.

    자리번호만 받던 동안 두 가지가 났다:

    1. 두 편이 같은 자리의 방향을 고르면 문자열이 같아 "must not repeat a direction"으로
       400이 났다(2026-08-12 사용자 화면: ``POST .../schedule 400``). 실제로는 서로 다른
       방향인데도 그랬다.
    2. 400이 나지 않는 경우가 더 나쁘다 — 그 자리번호가 **마지막 편의 후보**로 조용히
       해석돼, 고르지도 않은 방향으로 원고가 만들어진다.

    그래서 화면이 고른 방향을 통째로 보낸다. 없으면 ``None``이다 — 옛 화면이 보낸 요청과
    한 편짜리 흐름은 예전 그대로 자리번호로 찾는다.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BlogTaskError("VALIDATION_FAILED", f"{where} must be an object")
    try:
        candidate = IntentCandidate.model_validate(value)
    except ValidationError as error:
        raise BlogTaskError("VALIDATION_FAILED", f"{where} is not a valid direction") from error
    if not candidate.intent_id.strip() or not candidate.title.strip():
        raise BlogTaskError(
            "VALIDATION_FAILED", f"{where} must have an intentId and a title"
        )
    return candidate


def validate_additional_drafts(value: Any) -> list[dict[str, Any]]:
    """뒤이어 만들 글의 **(방향, 제목) 짝**(2026-08-12). 없으면 빈 목록 — 한 편이다.

    한 소재로 여러 편을 만들 때 편마다 제목과 방향을 함께 고른다. 고른 차례가 만들어지는
    차례라 **정렬하지 않는다.** 같은 방향이 두 번 들어오는 것은 막는다 — 말만 바꾼 중복
    글이 되기 때문이다.

    제목은 없어도 된다(그 편은 원본 제목을 쓴다). 방향은 반드시 있어야 한다 — 방향이
    없으면 무엇으로 다른 글을 만들지 알 수 없다.

    **중복은 자리번호가 아니라 방향의 제목으로 가린다**(2026-08-12). ``intentId``는 편마다
    다시 매겨지는 자리번호라(``validate_chosen_intent`` 참고) 서로 다른 방향이 같은 번호를
    달고 오는 일이 정상이다. 방향을 함께 보낸 요청은 그 제목으로 비교하고, 보내지 않은
    옛 요청만 예전처럼 번호로 비교한다.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise BlogTaskError("VALIDATION_FAILED", "additionalDrafts must be an array")
    if len(value) > MAX_DRAFT_COUNT - 1:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"additionalDrafts must have at most {MAX_DRAFT_COUNT - 1} items",
        )
    drafts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise BlogTaskError(
                "VALIDATION_FAILED", f"additionalDrafts[{index}] must be an object"
            )
        intent_id = item.get("intentId")
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise BlogTaskError(
                "VALIDATION_FAILED", f"additionalDrafts[{index}].intentId is required"
            )
        chosen = intent_id.strip()
        intent = validate_chosen_intent(
            item.get("intent"), where=f"additionalDrafts[{index}].intent"
        )
        # 방향을 함께 받았으면 그 제목이 정체성이다. 없으면 자리번호로 돌아간다.
        fingerprint = intent.title.strip() if intent is not None else chosen
        if fingerprint in seen:
            raise BlogTaskError(
                "VALIDATION_FAILED", "additionalDrafts must not repeat a direction"
            )
        seen.add(fingerprint)
        draft: dict[str, Any] = {"intentId": chosen}
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            draft["title"] = title.strip()
        if intent is not None:
            draft["intent"] = intent
        drafts.append(draft)
    return drafts


def validate_additional_intent_ids(value: Any, *, exclude: str = "") -> list[str]:
    """뒤이어 만들 글의 방향(2026-08-12). 없으면 빈 목록 — 예전 그대로 한 편이다.

    고른 차례가 만들어지는 차례라 **정렬하지 않는다.** 같은 방향이 두 번 들어오는 것은
    막는다 — 말만 바꾼 중복 글이 되기 때문이다.

    ``exclude``는 이미 첫 편으로 고른 방향이다. 함께 검사해 첫 편과 겹치는 것도 막는다.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise BlogTaskError("VALIDATION_FAILED", "additionalIntentIds must be an array")
    # 첫 편을 포함해 MAX_DRAFT_COUNT를 넘지 않는다.
    if len(value) > MAX_DRAFT_COUNT - 1:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            f"additionalIntentIds must have at most {MAX_DRAFT_COUNT - 1} items",
        )
    ids: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"additionalIntentIds[{index}] must be a non-empty string",
            )
        chosen = item.strip()
        if chosen == exclude or chosen in ids:
            raise BlogTaskError(
                "VALIDATION_FAILED", "additionalIntentIds must not repeat a direction"
            )
        ids.append(chosen)
    return ids


def validate_select_intent_request(body: Any) -> SelectIntentRequest:
    """intentId와, 사용자가 제외한 자료 URL 목록을 반환한다."""
    if not isinstance(body, dict):
        raise BlogTaskError("VALIDATION_FAILED", "request body must be a JSON object")
    intent_id = body.get("intentId")
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise BlogTaskError("VALIDATION_FAILED", "intentId is required")

    raw_excluded = body.get("excludedSourceUrls")
    excluded: list[str] = []
    if raw_excluded is not None:
        if not isinstance(raw_excluded, list):
            raise BlogTaskError("VALIDATION_FAILED", "excludedSourceUrls must be an array")
        if len(raw_excluded) > MAX_EXCLUDED_SOURCES:
            raise BlogTaskError(
                "VALIDATION_FAILED",
                f"excludedSourceUrls must have at most {MAX_EXCLUDED_SOURCES} items",
            )
        for index, item in enumerate(raw_excluded):
            if not isinstance(item, str) or not item.strip():
                raise BlogTaskError(
                    "VALIDATION_FAILED", f"excludedSourceUrls[{index}] must be a non-empty string"
                )
            excluded.append(item.strip())

    additional = validate_additional_intent_ids(
        body.get("additionalIntentIds"), exclude=intent_id.strip()
    )

    return SelectIntentRequest(
        intent_id=intent_id.strip(),
        excluded_source_urls=excluded,
        additional_intent_ids=additional,
    )
