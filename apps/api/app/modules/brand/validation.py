"""브랜드 자료 입력 검증.

사용자 설정 검증과 같은 방식이다: 첫 실패에서 멈추지 않고 **문제를 모두 모아** 던진다.
자료 화면은 칸이 많아서, 하나 고칠 때마다 다시 저장해 보게 하면 지친다.
"""

from typing import Any

from app.errors import BlogTaskError
from app.shared import (
    AUDIENCE_CATALOG,
    BRAND_DOCUMENT_SECTIONS,
    AUDIENCE_OTHER,
    BrandAudience,
    BrandClosing,
    BrandDocument,
    BrandImage,
    BrandLimits,
    BrandLink,
    BrandUseCase,
    BrandValidationError,
)

LIMITS = BrandLimits
#: data URL만 받는다. 바깥 주소를 그대로 두면 발행 뒤 이미지가 깨진다(원고 이미지와 같은
#: 이유 — 네이버가 자기 서버로 가져가야 한다).
_DATA_URL_PREFIX = "data:image/"
#: PDF도 data URL로 받는다(이미지와 같은 이유 — 발행·프롬프트가 같은 형식을 읽는다).
_PDF_URL_PREFIX = "data:application/pdf"


def _decoded_size(data_url: str) -> int:
    """data URL의 원본 바이트 크기(대략). base64는 원본보다 약 4/3 크다."""
    return len(data_url) * 3 // 4


def _error(field: str, code: str, message: str) -> BrandValidationError:
    return BrandValidationError(field=field, code=code, message=message)


def _audiences(value: Any, errors: list[BrandValidationError]) -> list[BrandAudience]:
    """고른 고객을 다듬는다. **카탈로그에 없는 값은 거절한다.**

    화면이 고른 것만 보내므로 정상 경로에서는 걸릴 일이 없다. 그래도 검사하는 이유는,
    자유 입력을 없앤 목적이 "표기가 들쭉날쭉해지는 것"을 막는 데 있기 때문이다 —
    임의 문자열이 들어오면 그 목적이 무너진다.

    유형을 하나도 안 고른 대분류는 버린다. 대분류만 켜 두고 아래를 비운 상태는 화면에서
    쉽게 만들어지는데, 그대로 저장하면 프롬프트에 "기업·사업자()" 같은 빈 껍데기가 실린다.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_error("audiences", "INVALID_TYPE", "주요 고객은 목록이어야 합니다."))
        return []

    cleaned: list[BrandAudience] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append(_error("audiences", "INVALID_TYPE", "고른 고객은 객체여야 합니다."))
            return []
        category = (item.get("category") or "").strip()
        allowed = AUDIENCE_CATALOG.get(category)
        if allowed is None:
            errors.append(_error("audiences", "UNKNOWN_VALUE", f"모르는 고객 대분류입니다: {category}"))
            return []

        raw_types = item.get("types") or []
        if not isinstance(raw_types, list):
            errors.append(_error("audiences", "INVALID_TYPE", f"{category}의 유형은 목록이어야 합니다."))
            return []
        types: list[str] = []
        for one in raw_types:
            text = (one or "").strip() if isinstance(one, str) else ""
            if not text:
                continue
            if text not in allowed:
                errors.append(
                    _error("audiences", "UNKNOWN_VALUE", f"{category}에 없는 유형입니다: {text}")
                )
                return []
            if text not in types:
                types.append(text)

        other = item.get("other")
        other = other.strip() if isinstance(other, str) else ""
        if other and AUDIENCE_OTHER not in types:
            # '기타'를 고르지 않았는데 직접 입력만 남은 경우다. 그 글자는 화면에서 보이지
            # 않으므로 저장하면 사용자가 모르는 값이 프롬프트에 실린다.
            other = ""
        if other and len(other) > LIMITS.MAX_AUDIENCE_OTHER_LENGTH:
            errors.append(
                _error("audiences", "TOO_LONG", f"기타 입력은 {LIMITS.MAX_AUDIENCE_OTHER_LENGTH}자 이하입니다.")
            )
            return []

        if not types:
            continue
        cleaned.append(BrandAudience(category=category, types=types, other=other or None))
    return cleaned


def _use_cases(value: Any, errors: list[BrandValidationError]) -> list[BrandUseCase]:
    """"이런 상황이면 이 기능" 기준표를 다듬는다(2026-08-19).

    상황과 기능이 **둘 다** 있어야 한 줄이다. 하나만 적힌 줄은 조용히 버린다 — 화면에서
    한 칸만 채우고 넘어가기 쉬운데, 그대로 저장하면 프롬프트에 "상황: (없음) → 자료
    조사" 같은 반쪽짜리 지시가 실린다. 검색어는 선택이라 비어도 그대로 둔다(비면
    ``situation``의 낱말이 그 자리를 대신한다).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_error("useCases", "INVALID_TYPE", "기준표는 목록이어야 합니다."))
        return []
    if len(value) > LIMITS.MAX_USE_CASES:
        errors.append(
            _error("useCases", "TOO_MANY", f"기준표는 최대 {LIMITS.MAX_USE_CASES}줄입니다.")
        )
        return []

    cleaned: list[BrandUseCase] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append(_error("useCases", "INVALID_TYPE", "기준표의 각 줄은 객체여야 합니다."))
            return []
        situation = (item.get("situation") or "").strip() if isinstance(item.get("situation"), str) else ""
        feature = (item.get("feature") or "").strip() if isinstance(item.get("feature"), str) else ""
        if not situation or not feature:
            continue

        raw_keywords = item.get("keywords") or []
        if not isinstance(raw_keywords, list):
            errors.append(
                _error("useCases", "INVALID_TYPE", f"'{feature}'의 검색어는 목록이어야 합니다.")
            )
            return []
        keywords: list[str] = []
        for one in raw_keywords:
            text = one.strip() if isinstance(one, str) else ""
            if text and text not in keywords:
                keywords.append(text[: LIMITS.MAX_ITEM_LENGTH])
        if len(keywords) > LIMITS.MAX_USE_CASE_KEYWORDS:
            errors.append(
                _error(
                    "useCases",
                    "TOO_MANY",
                    f"한 줄의 검색어는 최대 {LIMITS.MAX_USE_CASE_KEYWORDS}개입니다: {feature}",
                )
            )
            return []

        cleaned.append(
            BrandUseCase(
                situation=situation[: LIMITS.MAX_ITEM_LENGTH],
                feature=feature[: LIMITS.MAX_ITEM_LENGTH],
                keywords=keywords,
            )
        )
    return cleaned


def _closing(value: Any, errors: list[BrandValidationError]) -> BrandClosing | None:
    """글 맨 마지막에 붙는 마무리 블록. 없으면 None(아무것도 붙지 않는다).

    **사실 한 줄과 주소가 둘 다 있어야 한 블록이다.** 하나만 채운 것은 저장하지 않는다 —
    이 글자는 검수를 거치지 않고 그대로 발행되므로, 반쪽짜리가 모든 글의 맨 끝에 붙는다.

    주소는 ``links``와 같은 규칙으로 본다(http로 시작). 여기 적힌 주소는 독자가 실제로
    누르는 것이라, 틀리면 글마다 죽은 링크가 하나씩 실린다.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(_error("closing", "INVALID_TYPE", "마무리는 객체여야 합니다."))
        return None

    def text(key: str) -> str:
        raw = value.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    note, url = text("note"), text("url")
    if not note and not url:
        # 화면에서 칸을 비워 둔 상태다. 안 쓰겠다는 뜻이므로 조용히 없앤다.
        return None
    if not note or not url:
        errors.append(
            _error("closing", "REQUIRED", "마무리에는 안내 문구와 주소가 모두 필요합니다.")
        )
        return None
    if not url.startswith(("http://", "https://")):
        errors.append(_error("closing", "INVALID_URL", f"주소는 http로 시작해야 합니다: {url}"))
        return None
    if len(note) > LIMITS.MAX_ITEM_LENGTH:
        errors.append(
            _error("closing", "TOO_LONG", f"안내 문구는 {LIMITS.MAX_ITEM_LENGTH}자 이하입니다.")
        )
        return None

    # 링크 글자를 비워 두면 주소를 그대로 보여 준다 — 빈 링크는 누를 데가 없다.
    label = text("label") or url
    image_label = text("imageLabel") or None
    return BrandClosing(
        note=note,
        label=label[: LIMITS.MAX_ITEM_LENGTH],
        url=url,
        image_label=image_label[: LIMITS.MAX_ITEM_LENGTH] if image_label else None,
    )


def _hashtags(value: Any, errors: list[BrandValidationError]) -> list[str]:
    """모든 글에 고정으로 붙일 해시태그(2026-08-20).

    '#'과 공백은 떼어 낸다 — 발행할 때 '#'이 붙으므로 여기 남아 있으면 '##AIONA'가 되고,
    해시태그 안의 공백은 네이버에서 태그를 끊는다.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_error("hashtags", "INVALID_TYPE", "해시태그는 목록이어야 합니다."))
        return []
    if len(value) > LIMITS.MAX_HASHTAGS:
        errors.append(
            _error("hashtags", "TOO_MANY", f"해시태그는 최대 {LIMITS.MAX_HASHTAGS}개입니다.")
        )
        return []

    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#").replace(" ", "")
        if not tag or tag in cleaned:
            continue
        cleaned.append(tag[: LIMITS.MAX_ITEM_LENGTH])
    return cleaned


def _links(value: Any, errors: list[BrandValidationError]) -> list[BrandLink]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_error("links", "INVALID_TYPE", "links는 목록이어야 합니다."))
        return []
    if len(value) > LIMITS.MAX_LINKS:
        errors.append(_error("links", "TOO_MANY", f"주소는 최대 {LIMITS.MAX_LINKS}개입니다."))
        return []

    cleaned: list[BrandLink] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append(_error("links", "INVALID_TYPE", "각 주소는 객체여야 합니다."))
            return []
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            errors.append(_error("links", "INVALID_URL", f"주소는 http로 시작해야 합니다: {url}"))
            return []
        label = (item.get("label") or "").strip() or url
        cleaned.append(BrandLink(label=label[: LIMITS.MAX_ITEM_LENGTH], url=url))
    return cleaned


def _images(value: Any, errors: list[BrandValidationError]) -> list[BrandImage]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_error("images", "INVALID_TYPE", "images는 목록이어야 합니다."))
        return []
    if len(value) > LIMITS.MAX_IMAGES:
        errors.append(_error("images", "TOO_MANY", f"이미지는 최대 {LIMITS.MAX_IMAGES}장입니다."))
        return []

    cleaned: list[BrandImage] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append(_error("images", "INVALID_TYPE", "각 이미지는 객체여야 합니다."))
            return []
        data_url = (item.get("dataUrl") or item.get("data_url") or "").strip()
        if not data_url:
            continue
        if not data_url.startswith(_DATA_URL_PREFIX):
            errors.append(
                _error("images", "INVALID_TYPE", "이미지는 data:image/... 형식이어야 합니다.")
            )
            return []
        # base64는 원본보다 약 4/3 크다. 대략만 재도 과한 파일을 걸러 내기에 충분하다.
        if len(data_url) * 3 // 4 > LIMITS.MAX_IMAGE_BYTES:
            errors.append(
                _error(
                    "images",
                    "TOO_LONG",
                    f"이미지 한 장은 {LIMITS.MAX_IMAGE_BYTES // 1024}KB 이하입니다"
                    f"({LIMITS.MAX_IMAGES}장을 한 번에 보내야 해서 한 장을 크게 둘 수 없습니다).",
                )
            )
            return []
        caption = (item.get("caption") or "").strip() or None
        label = (item.get("label") or "").strip() or "브랜드 이미지"
        cleaned.append(
            BrandImage(
                label=label[: LIMITS.MAX_ITEM_LENGTH],
                data_url=data_url,
                caption=caption[: LIMITS.MAX_ITEM_LENGTH] if caption else None,
            )
        )
    return cleaned


def _documents(value: Any, errors: list[BrandValidationError]) -> list[BrandDocument]:
    """올린 텍스트·PDF 문서를 다듬는다.

    텍스트는 글자 그대로 저장한다(프롬프트에 바로 실린다). PDF는 data URL로 두고,
    프롬프트를 만들 때 텍스트를 뽑아 쓴다(``llm/prompts.py``).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_error("documents", "INVALID_TYPE", "문서는 목록이어야 합니다."))
        return []
    if len(value) > LIMITS.MAX_DOCUMENTS:
        errors.append(_error("documents", "TOO_MANY", f"문서는 최대 {LIMITS.MAX_DOCUMENTS}개입니다."))
        return []

    cleaned: list[BrandDocument] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append(_error("documents", "INVALID_TYPE", "각 문서는 객체여야 합니다."))
            return []
        section = (item.get("section") or "").strip()
        if section not in BRAND_DOCUMENT_SECTIONS:
            errors.append(_error("documents", "UNKNOWN_VALUE", f"모르는 자료 위치입니다: {section}"))
            return []
        kind = (item.get("kind") or "").strip().upper()
        raw = item.get("value")
        raw = raw if isinstance(raw, str) else ""
        if not raw.strip():
            continue
        name = (item.get("name") or "").strip() or ("PDF 문서" if kind == "PDF" else "텍스트 문서")

        if kind == "TEXT":
            text = raw.strip()
            if len(text) > LIMITS.MAX_TEXT_DOCUMENT_LENGTH:
                errors.append(
                    _error(
                        "documents",
                        "TOO_LONG",
                        f"{name}이(가) {LIMITS.MAX_TEXT_DOCUMENT_LENGTH}자를 넘습니다. "
                        "필요한 부분만 남겨 주세요.",
                    )
                )
                return []
            cleaned.append(
                BrandDocument(
                    section=section, name=name[: LIMITS.MAX_ITEM_LENGTH], kind="TEXT", value=text
                )
            )
            continue

        if kind == "PDF":
            if not raw.startswith(_PDF_URL_PREFIX):
                errors.append(
                    _error("documents", "INVALID_TYPE", f"{name}은(는) PDF 형식이 아닙니다.")
                )
                return []
            if _decoded_size(raw) > LIMITS.MAX_PDF_BYTES:
                errors.append(
                    _error(
                        "documents",
                        "TOO_LONG",
                        f"PDF 한 개는 {LIMITS.MAX_PDF_BYTES // (1024 * 1024)}MB 이하입니다: {name}",
                    )
                )
                return []
            cleaned.append(
                BrandDocument(
                    section=section, name=name[: LIMITS.MAX_ITEM_LENGTH], kind="PDF", value=raw
                )
            )
            continue

        errors.append(_error("documents", "UNKNOWN_VALUE", f"모르는 문서 종류입니다: {kind}"))
        return []
    return cleaned


def _check_attachment_budget(
    images: list[BrandImage], documents: list[BrandDocument], errors: list[BrandValidationError]
) -> None:
    """이미지와 PDF의 **합계**를 본다.

    낱개 상한만 두면 합이 요청 본문 상한(16MB)을 넘어, 화면은 다 받아 놓고 저장 버튼에서
    통째로 실패한다. 어디를 줄여야 하는지 알 수 있게 현재 합계도 함께 알린다.
    """
    total = sum(_decoded_size(image.data_url) for image in images)
    total += sum(_decoded_size(doc.value) for doc in documents if doc.kind == "PDF")
    if total <= LIMITS.MAX_ATTACHMENT_TOTAL_BYTES:
        return
    errors.append(
        _error(
            "images",
            "TOO_LONG",
            f"이미지와 PDF를 합쳐 {LIMITS.MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)}MB까지"
            f" 넣을 수 있습니다(지금 {total / (1024 * 1024):.1f}MB).",
        )
    )


def validate_brand_body(raw_body: Any) -> dict:
    """저장 요청을 다듬어 돌려준다. 문제가 있으면 한꺼번에 모아 던진다."""
    body = raw_body if isinstance(raw_body, dict) else {}
    errors: list[BrandValidationError] = []

    name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
    if not name:
        errors.append(_error("name", "REQUIRED", "브랜드 이름을 입력해 주세요."))
    elif len(name) > LIMITS.MAX_NAME_LENGTH:
        errors.append(_error("name", "TOO_LONG", f"이름은 {LIMITS.MAX_NAME_LENGTH}자 이하입니다."))

    # 서술 칸 셋은 규칙이 같다 — 있으면 문자열, 비면 None, 상한을 넘으면 알린다.
    sections: dict[str, str | None] = {}
    for field, label in (
        ("description", "브랜드 소개"),
        ("features", "핵심 기능·서비스"),
    ):
        value = body.get(field)
        if value is None:
            sections[field] = None
            continue
        if not isinstance(value, str):
            errors.append(_error(field, "INVALID_TYPE", f"{label}은(는) 문자열이어야 합니다."))
            sections[field] = None
            continue
        text = value.strip() or None
        if text and len(text) > LIMITS.MAX_SECTION_LENGTH:
            errors.append(
                _error(field, "TOO_LONG", f"{label}은(는) {LIMITS.MAX_SECTION_LENGTH}자 이하입니다.")
            )
        sections[field] = text

    cleaned = {
        "name": name,
        **sections,
        "audiences": _audiences(body.get("audiences"), errors),
        "use_cases": _use_cases(body.get("useCases"), errors),
        "closing": _closing(body.get("closing"), errors),
        "hashtags": _hashtags(body.get("hashtags"), errors),
        "links": _links(body.get("links"), errors),
        "documents": _documents(body.get("documents"), errors),
        "images": _images(body.get("images"), errors),
    }
    _check_attachment_budget(cleaned["images"], cleaned["documents"], errors)

    if errors:
        raise BlogTaskError(
            "VALIDATION_FAILED",
            "; ".join(error.message for error in errors),
        )
    return cleaned
