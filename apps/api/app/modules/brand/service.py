"""브랜드 자료 서비스.

저장·조회와 함께, **모델에게 줄 문장**을 만드는 곳이기도 하다(`brand_brief`). 자료가
프롬프트에 어떤 모양으로 들어가는지는 한곳에서만 정해야, 나중에 항목을 더해도 글이
따로 놀지 않는다.
"""

import logging

from app.errors import BlogTaskError
from app.llm import SiteReadInput
from app.modules.blog_task.validation import MAX_REFERENCE_MATERIALS
from app.shared import (
    AUDIENCE_OTHER,
    BRAND_DOCUMENT_SECTIONS,
    BRAND_MATERIAL_ORIGIN,
    BRAND_MODES,
    BRAND_MODE_FOCUS,
    BRAND_MODE_UTILITY,
    BrandLimits,
    BrandProfile,
)
from app.shared.format import now_iso
from app.shared.ids import new_brand_id

from .defaults import DEFAULT_BRAND_ID, DEFAULTS_REVISION, default_brand_body
from .fit import brand_use_case_lines, evaluate_brand_fit, use_case_brief
from .repository import BrandRepository
from .validation import validate_brand_body, validate_site_read_body

logger = logging.getLogger(__name__)

#: 브랜드 글 하나가 실을 수 있는 참고자료 개수.
#:
#: 브랜드 자료가 펼쳐진 것(소개 1 + 주소 20 + 문서 5 + 이미지 10 = 36)에, 입력 단계에서
#: 사용자가 직접 넣은 것(작성 화면과 같은 10개)을 더한 값이다. 작성 화면의 상한 10개를
#: 그대로 쓰면 **저장해 둔 브랜드 자료가 많은 브랜드는 글을 아예 만들 수 없다** — 자료를
#: 다 채운 브랜드에서 실제로 그렇게 막혔다.
BRAND_POST_MAX_REFERENCE_MATERIALS = (
    1
    + BrandLimits.MAX_LINKS
    + BrandLimits.MAX_DOCUMENTS
    + BrandLimits.MAX_IMAGES
    + MAX_REFERENCE_MATERIALS
)

# BRAND_MATERIAL_ORIGIN은 app.shared에 있다 — 검증도 같은 글자를 봐야 한다.


class BrandService:
    def __init__(self, repository: BrandRepository, site_reader=None):
        self._repository = repository
        # 브랜드 사이트를 읽어 자료를 채워 주는 쪽(`llm.contracts.SiteReader`, 없으면
        # None). 자격 증명이 없는 서버에서도 나머지는 그대로 돌아야 해서 선택 항목이다.
        self._site_reader = site_reader

    async def read_feature(self, user_id: str, brand_id: str, raw_body) -> dict:
        """신기능 페이지(또는 붙여넣은 공지)를 읽어 **글의 출발점**으로 바꾼다.

        기능 이름을 사람이 알고 있어야 하는 문제가 여기서 없어진다 — 브랜드 자료가 아직
        그 기능을 모르더라도 글은 써진다. 그래서 자료를 고치는 것과 별개의 통로다.
        """
        profile = await self.get_brand_light(user_id, brand_id)
        urls, text = validate_site_read_body(raw_body, profile)
        brief = await self._read_feature(
            SiteReadInput(brand_name=profile.name, urls=urls, text=text)
        )
        return brief.model_dump(by_alias=True)

    def _reader(self):
        if self._site_reader is None:
            raise BlogTaskError(
                "SITE_READER_UNAVAILABLE",
                "사이트에서 자료를 가져오는 기능이 지금 꺼져 있습니다. 자료는 직접 채워 주세요.",
            )
        return self._site_reader

    async def _read_feature(self, site_input):
        try:
            return await self._reader().read_feature(site_input)
        except BlogTaskError:
            raise
        except Exception as error:
            raise _site_read_failed(error) from error

    async def ensure_default_brands(self, user_id: str) -> None:
        """기본 브랜드(AIONA)가 없으면 만든다(2026-08-19).

        이 저장소는 AIONA 유입용 콘텐츠 엔진이다. 그러니 AIONA 자료는 누가 어딘가에서
        한 번 등록해 주어야 하는 것이 아니라 **처음부터 거기 있어야 한다** — 서버를 새로
        세우거나 계정을 새로 만들어도 브랜드 고르기 칸에 이미 있다.

        **손으로 고쳐 둔 글자는 덮어쓰지 않는다.** 사용자가 문구를 다듬어 두었는데 조회할
        때마다 코드가 원래 값으로 되돌리면 그 편집이 통째로 사라진다.

        **다만 빈 칸은 채운다**(2026-08-20). "없으면 만든다"만 있었을 때, 한 번 만들어진
        뒤에 정의로 새 자료가 들어오면(마스코트 그림, 고정 해시태그) 이미 쓰던 사람에게는
        영영 오지 않았다 — 등록한 적도 없는 자료가 자기 것만 비어 있는데, 사용자에게는
        그것을 알 방법도 고칠 방법도 없다. 그래서 판번호(`DEFAULTS_REVISION`)가 뒤처져
        있으면 **비어 있는 칸만** 지금 정의에서 채운다. 채워져 있는 칸은 그대로 둔다.

        통째로 되돌리려면 `scripts/seed_aiona_brand.py --apply`를 쓴다 — 덮어쓰기는 사람이
        눈으로 확인하고 하는 일이다.

        브랜드 id가 사람마다 같은 값으로 고정돼 있어(`DEFAULT_BRAND_ID`), 두 요청이 동시에
        들어와도 같은 문서를 두 번 쓸 뿐 두 벌이 되지 않는다.
        """
        stamp = now_iso()
        existing = await self._repository.find(user_id, DEFAULT_BRAND_ID)
        if existing is not None:
            await self._backfill_default_brand(existing, stamp)
            return
        # **지운 것까지 센다.** 사용자가 기본 브랜드를 지웠으면 그 자리에 '지웠다'는
        # 표시가 남아 있다 — 조회(`find`)는 그것을 없는 것으로 다루므로 여기까지 온다.
        # 표시를 못 보면 다음 조회에서 되살아나고, 사용자는 지운 것이 왜 돌아왔는지 알 수
        # 없다.
        if await self._repository.exists(user_id, DEFAULT_BRAND_ID):
            return
        await self._repository.upsert(
            BrandProfile(
                brand_id=DEFAULT_BRAND_ID,
                user_id=user_id,
                created_at=stamp,
                updated_at=stamp,
                defaults_revision=DEFAULTS_REVISION,
                **validate_brand_body(default_brand_body()),
            )
        )

    async def _backfill_default_brand(self, existing: BrandProfile, stamp: str) -> None:
        """이미 있는 기본 브랜드의 **빈 칸만** 지금 정의로 채운다(2026-08-20).

        판번호가 최신이면 아무것도 하지 않는다 — 그 뒤로는 사용자가 비운 것이 곧 뜻이다.
        (마스코트를 일부러 지웠는데 조회할 때마다 되살아나면 그것도 못 고치는 것이다.)

        '빈 칸'만 보는 이유: 무엇을 고쳤는지 알 방법이 없으니, 확실히 안전한 것만 만진다.
        비어 있는 칸을 채우는 것은 어떤 편집도 지우지 않는다.
        """
        if existing.defaults_revision >= DEFAULTS_REVISION:
            return
        defaults = validate_brand_body(default_brand_body())
        # 값이 있으면 손대지 않는다. 목록 칸은 비었을 때만, 글자 칸도 비었을 때만.
        filled = {
            field: defaults[field]
            for field in (
                "images",
                "hashtags",
                "use_cases",
                "audiences",
                "links",
                "documents",
                "description",
                "features",
                "closing",
            )
            if defaults.get(field) and not getattr(existing, field)
        }
        await self._repository.upsert(
            existing.model_copy(
                update={
                    **filled,
                    # 판번호는 채운 것이 없어도 올린다. 그러지 않으면 조회할 때마다 다시
                    # 훑고, 사용자가 일부러 비워 둔 칸을 매번 채우려 든다.
                    "defaults_revision": DEFAULTS_REVISION,
                    "updated_at": stamp if filled else existing.updated_at,
                }
            )
        )

    async def list_brands(self, user_id: str) -> list[BrandProfile]:
        await self.ensure_default_brands(user_id)
        return await self._repository.list_by_user_id(user_id)

    async def list_brand_items(self, user_id: str):
        """목록 화면용 가벼운 조회 — 이미지·문서의 base64를 싣지 않는다.

        브랜드 하나가 2MB인데(실측: 이미지 9장) 고르기 화면은 이름과 한 줄 소개만
        그린다. 그걸 보여 주려고 2MB를 기다리고 있었다.
        """
        await self.ensure_default_brands(user_id)
        return await self._repository.list_items_by_user_id(user_id)

    async def get_brand(self, user_id: str, brand_id: str) -> BrandProfile:
        profile = await self._repository.find(user_id, brand_id)
        if profile is None and brand_id == DEFAULT_BRAND_ID:
            # 목록을 거치지 않고 곧바로 이 id로 들어온 경우(앱스튜디오 진입·저장된 글).
            await self.ensure_default_brands(user_id)
            profile = await self._repository.find(user_id, brand_id)
        if profile is None:
            raise BlogTaskError("NOT_FOUND", f"브랜드 자료 {brand_id}를 찾을 수 없습니다.")
        return profile

    async def get_brand_light(self, user_id: str, brand_id: str) -> BrandProfile:
        """텍스트 필드만 담은 조회 — 이미지·문서 base64를 뺀다(자료 편집 첫 화면용).

        전체 문서는 2MB라 대역폭이 제한된 Atlas에서 20초 넘게 걸린다(2026-08-07 실측).
        편집 화면은 이것으로 먼저 열고, 첨부는 전체 조회가 뒤따라 채운다.
        """
        profile = await self._repository.find_light(user_id, brand_id)
        if profile is None:
            raise BlogTaskError("NOT_FOUND", f"브랜드 자료 {brand_id}를 찾을 수 없습니다.")
        return profile

    async def create_brand(self, user_id: str, raw_body) -> BrandProfile:
        cleaned = validate_brand_body(raw_body)
        stamp = now_iso()
        return await self._repository.upsert(
            BrandProfile(
                brand_id=new_brand_id(),
                user_id=user_id,
                created_at=stamp,
                updated_at=stamp,
                **cleaned,
            )
        )

    async def update_brand(self, user_id: str, brand_id: str, raw_body) -> BrandProfile:
        existing = await self.get_brand(user_id, brand_id)
        cleaned = validate_brand_body(raw_body)
        return await self._repository.upsert(
            existing.model_copy(update={**cleaned, "updated_at": now_iso()})
        )

    async def delete_brand(self, user_id: str, brand_id: str) -> None:
        """브랜드 자료를 지운다. 기본 브랜드도 지울 수 있다(2026-08-20 사용자 요청).

        다만 지우는 **방식**이 다르다. 기본 브랜드는 없으면 다시 만들어 주는 자리가
        있어서(`ensure_default_brands`) 문서를 없애면 다음 조회에서 되살아난다 — 그래서
        문서를 지우는 대신 **지웠다는 표시를 남긴다.** 목록·조회에서는 없는 것으로 다루고,
        다시 만들어 주지도 않는다.

        되살리려면 `scripts/seed_aiona_brand.py --apply`를 쓴다.
        """
        if brand_id == DEFAULT_BRAND_ID:
            existing = await self._repository.find(user_id, brand_id)
            if existing is None:
                raise BlogTaskError(
                    "NOT_FOUND", f"브랜드 자료 {brand_id}를 찾을 수 없습니다."
                )
            stamp = now_iso()
            await self._repository.upsert(
                existing.model_copy(update={"deleted_at": stamp, "updated_at": stamp})
            )
            return
        if not await self._repository.delete(user_id, brand_id):
            raise BlogTaskError("NOT_FOUND", f"브랜드 자료 {brand_id}를 찾을 수 없습니다.")


def _site_read_failed(error: Exception) -> BlogTaskError:
    """읽기 실패를 사용자가 읽을 말로 바꾼다.

    원문을 그대로 보이지 않는다 — provider 이름·모델명·상태코드가 화면에 뜬다. 대신
    **다음에 무엇을 하면 되는지**를 말한다: 이 기능은 없어도 자료를 손으로 채울 수 있다.
    """
    logger.warning("브랜드 사이트 읽기 실패 | %s: %s", type(error).__name__, error)
    return BlogTaskError(
        "SITE_READ_FAILED",
        "사이트를 읽지 못했습니다. 주소가 맞는지 확인하거나, 내용을 직접 붙여넣어 주세요.",
    )


def brand_mode_for(
    profile: BrandProfile | None, topic: object, requested: object = None
) -> str | None:
    """브랜드가 이 글에서 맡는 역할을 정한다(2026-08-19).

    소재 칸과 브랜드 칸이 서로를 잠그던 동안에는 물을 필요가 없었다 — 브랜드가 있으면
    언제나 주인공이었다. 잠금을 없애면서 같은 ``brandId``가 두 가지 글을 뜻하게 됐고,
    그 둘은 **정반대의 글**이다:

    ``FOCUS``    브랜드가 주인공. 소재를 비우고 브랜드만 골랐을 때다. 소재는 서버가
                 브랜드 이름으로 채운다("AIONA란 무엇인가").
    ``UTILITY``  트렌드·소재가 주인공이고 브랜드는 그 상황에서 쓴 도구. 소재를 적고
                 브랜드도 골랐을 때다("빼빼로" x AIONA).

    사용자가 직접 고른 값(``requested``)이 있으면 그것을 따른다 — 어느 쪽으로 쓸지는
    **편집 판단**이고, 소재가 브랜드 이름으로 시작하는 글("AIONA 앱스튜디오 사용법")은
    소재가 있어도 FOCUS가 맞다. 다만 소재가 비어 있으면 UTILITY가 성립하지 않는다:
    소재 자리를 브랜드 이름이 채우게 되어 브랜드가 주인공이자 도구가 된다. 그래서 그
    한 가지만 되돌린다.

    브랜드가 없으면 역할도 없다(None).
    """
    if profile is None:
        return None
    text = topic.strip() if isinstance(topic, str) else ""
    if isinstance(requested, str) and requested in BRAND_MODES:
        return requested if text else BRAND_MODE_FOCUS
    # 소재가 브랜드 이름 그 자체이면 브랜드로 쓰겠다는 뜻이다(화면이 이름을 채워 보내는
    # 경로가 남아 있을 수 있다). 공백·대소문자만 다른 것도 같은 것으로 본다.
    if not text or text.replace(" ", "").lower() == profile.name.replace(" ", "").lower():
        return BRAND_MODE_FOCUS
    return BRAND_MODE_UTILITY


def brand_reference_materials(profile: BrandProfile) -> list[dict]:
    """브랜드 자료를 글 작성 입력의 **참고자료**로 바꾼다.

    새 통로를 만들지 않는 것이 핵심이다. 이 저장소의 파이프라인(M3 자료 수집 → M4 원고)은
    이미 ``referenceMaterials``를 읽는다. 브랜드 자료를 그 형식으로 넣으면 자료 수집도,
    원고도, 발행도 손댈 것이 없다.

    - 설명은 ``TEXT`` 한 덩어리로(``brand_brief``와 같은 문장이다)
    - 주소는 ``URL``로 — 자료 수집이 실제로 읽어 본다
    - 이미지는 ``IMAGE``로. data URL이라 원고 이미지와 형식이 같아 발행 경로가 그대로 받는다
    """
    materials: list[dict] = [
        {"type": "TEXT", "name": f"{profile.name} 브랜드 자료", "value": brand_brief(profile)}
    ]
    materials += [
        {"type": "URL", "name": link.label, "value": link.url} for link in profile.links
    ]
    # 올린 문서는 종류를 그대로 넘긴다 — TEXT는 글자, PDF는 data URL이고, 파이프라인이
    # 이미 둘을 다르게 읽는다(PDF는 프롬프트를 만들 때 텍스트를 뽑는다).
    # 이름에 어느 칸의 자료인지 붙인다. 파일 이름만으로는 모델이 무엇인지 짐작해야 한다.
    materials += [
        {
            "type": doc.kind,
            "name": f"{BRAND_DOCUMENT_SECTIONS.get(doc.section, '브랜드')} 자료 - {doc.name}",
            "value": doc.value,
        }
        for doc in profile.documents
    ]
    materials += [
        {"type": "IMAGE", "name": image.caption or image.label, "value": image.data_url}
        for image in profile.images
    ]
    # 누가 넣은 자료인지 표시해 둔다(2026-08-11). 다시 저장할 때 브랜드 자료만 걷어내고
    # 지금 브랜드로 다시 채우려면 이 표시가 있어야 한다 — 없으면 같은 자료가 두 벌이 되고
    # 브랜드를 바꿔도 옛 자료가 남는다.
    for material in materials:
        material["origin"] = BRAND_MATERIAL_ORIGIN
    return materials


def merge_brand_materials(
    profile: BrandProfile | None, incoming: list[dict] | None
) -> list[dict]:
    """화면이 보낸 참고자료에 브랜드 자료를 얹는다(2026-08-11 — 브랜드 글쓰기 통합).

    브랜드는 이제 별도 화면이 아니라 **소재 단계의 선택 항목**이다. 그래서 저장은 한
    곳에서 일어나고, 이 함수가 그때마다 목록을 다시 만든다:

        [지금 고른 브랜드의 자료] + [사용자가 직접 넣은 자료]

    앞서 저장된 브랜드 자료(origin="brand")는 **언제나 버리고 다시 채운다.** 화면은
    저장돼 있던 목록을 그대로 돌려보내므로, 걷어내지 않으면 저장할 때마다 브랜드 자료가
    한 벌씩 늘어난다. 브랜드 선택을 해제했으면(profile=None) 브랜드 자료는 사라지고
    사용자 자료만 남는다.
    """
    user_materials = [
        material
        for material in (incoming or [])
        if isinstance(material, dict) and material.get("origin") != BRAND_MATERIAL_ORIGIN
    ]
    if profile is None:
        return user_materials
    return brand_reference_materials(profile) + user_materials


def brand_brief(profile: BrandProfile) -> str:
    """모델에게 줄 브랜드 설명.

    **비어 있는 항목은 아예 넣지 않는다.** "서비스: (없음)" 같은 줄을 주면 모델이 그것을
    사실로 받아들여 "서비스가 없는 회사"라고 쓴다. 채운 것만 말한다.

    말투는 여기서 정하지 않는다. 사용자 설정의 페르소나가 이미 그 일을 하고, 두 군데서
    말투를 정하면 서로 어긋난다.
    """
    lines = [f"브랜드 이름: {profile.name}"]
    if profile.description:
        lines.append(f"소개:\n{profile.description}")
    if profile.features:
        lines.append(f"핵심 기능·서비스:\n{profile.features}")
    if profile.audiences:
        # 고른 값을 사람이 읽는 문장으로 편다. 모델은 결국 글로 읽으므로, 저장 구조가
        # 아니라 **문장**이 나가야 한다. '기타'라는 낱말 자체는 빼고 직접 적은 것만 싣는다
        # — 모델에게 "기타"는 아무 뜻도 아니다.
        picked = []
        for group in profile.audiences:
            names = [name for name in group.types if name != AUDIENCE_OTHER]
            if group.other:
                names.append(group.other)
            picked.append(f"{group.category}({', '.join(names)})" if names else group.category)
        lines.append("주요 고객: " + " / ".join(picked))
    # 기준표("이런 상황이면 이 기능")는 **기능 이름의 출처**다. 이것이 자료에 없으면
    # 모델은 줄글에서 기능을 짐작하거나 없는 이름을 지어낸다(2026-08-19).
    if brief := use_case_brief(profile.use_cases):
        lines.append(brief)
    if profile.links:
        lines.append(
            "관련 주소:\n" + "\n".join(f"- {link.label}: {link.url}" for link in profile.links)
        )
    return "\n".join(lines)


def fit_context_of(body: dict) -> list[str]:
    """결합 가능성을 잴 때 **소재 바깥에서** 볼 것들.

    소재에서 닿으면 A, 여기서만 닿으면 B다(``fit.evaluate_brand_fit``). 트렌드 키워드는
    여기 없다 — 아직 고르기 전이다. 소재를 저장하는 시점이 사용자가 조합을 바꿀 수 있는
    마지막 자리이므로, 그때 아는 것으로 잰다.
    """
    parts: list[str] = []
    category = body.get("subjectCategory")
    if isinstance(category, str):
        parts.append(category)
    for key in ("purpose", "keywords"):
        values = body.get(key)
        if isinstance(values, list):
            parts += [value for value in values if isinstance(value, str)]
    return parts


async def with_brand_materials(
    brand_service: "BrandService", user_id: str, body: dict
) -> tuple[dict, int]:
    """``brandId``가 있으면 그 브랜드 자료를 참고자료에 얹는다(2026-08-11).

    브랜드 글쓰기를 새 글 작성에 통합하면서 생긴 자리다. 브랜드 화면이 하던 일(자료를
    참고자료로 펼쳐 넣기)을 여기서 하고, 나머지는 평범한 글과 완전히 같은 길을 간다 —
    새 파이프라인을 만들지 않는다.

    **글을 만드는 통로가 하나가 아니라서** 라우트가 아니라 여기 있다(2026-08-19).
    사용자가 화면에서 만드는 글(`POST /posts`)과 자동 포스팅의 예약 작업이 둘 다 이것을
    거친다. 두 벌로 두면 화면으로 만든 글과 예약으로 만든 글의 브랜드 처리가 조용히
    갈라진다 — 같은 소재인데 한쪽만 브랜드가 주인공이 되는 식이다.

    돌려주는 상한이 둘로 갈리는 이유: 브랜드 자료는 **서버가 펼쳐 넣는 것**이라 작성
    화면의 10개 상한으로 재면 자료를 다 채운 브랜드는 글을 아예 만들 수 없다.

    브랜드를 못 찾으면 그대로 오류다 — 조용히 브랜드 없이 만들면 사용자는 브랜드가
    반영된 줄 알고 원고를 받는다.
    """
    brand_id = body.get("brandId")
    brand_id = brand_id.strip() if isinstance(brand_id, str) else ""
    profile = await brand_service.get_brand(user_id, brand_id) if brand_id else None
    merged = merge_brand_materials(profile, body.get("referenceMaterials"))
    limit = (
        BRAND_POST_MAX_REFERENCE_MATERIALS if profile is not None else MAX_REFERENCE_MATERIALS
    )

    # 역할은 **사용자가 보낸 소재로** 판정한다. 아래에서 빈 소재를 브랜드 이름으로
    # 채우므로, 채우기 **전에** 재야 한다 — 채운 뒤에 재면 모든 글이 UTILITY가 된다.
    topic = body.get("topic") if isinstance(body.get("topic"), str) else ""
    mode = brand_mode_for(profile, topic, body.get("brandMode"))
    # 소재 × 브랜드가 자연스럽게 닿는지(A·B·C)도 여기서 잰다. 브랜드가 주인공인 글
    # (FOCUS)에는 물을 것이 없다 — 소재가 곧 브랜드다.
    fit = (
        evaluate_brand_fit(profile, topic, context=fit_context_of(body))
        if mode == BRAND_MODE_UTILITY
        else None
    )

    # 이름은 확인한 브랜드에서 가져와 **덮어쓴다**. 화면이 보낸 값을 그대로 두면 없는
    # 브랜드 이름이 프롬프트에 실린다. 브랜드를 풀었으면(None) 이름도 함께 지운다.
    payload = {
        **body,
        "referenceMaterials": merged,
        "brandName": profile.name if profile is not None else None,
        "brandMode": mode,
        "brandFitGrade": fit.grade if fit is not None else None,
        # 닿은 줄만 싣는다. 표 전체는 이미 참고자료에 있고, 거기서 모델이 고르게 두면
        # 소재와 무관한 기능이 붙는다.
        "brandUseCases": brand_use_case_lines(profile, fit) if fit and profile else [],
        # 모든 글에 고정으로 붙는 해시태그. 아래 마무리와 같은 이유로 베껴 둔다.
        "brandHashtags": list(profile.hashtags) if profile is not None else [],
        # 글 맨 끝에 붙일 마무리. **지금 정해져 있는 문구를 베껴 둔다** — 나중에 브랜드
        # 자료를 고쳐도 이미 나간 글의 마무리는 바뀌지 않아야 한다.
        "brandClosing": (
            profile.closing.model_dump(by_alias=True)
            if profile is not None and profile.closing is not None
            else None
        ),
    }

    # 브랜드만 고른 글(FOCUS)은 소재 칸이 비어서 온다 — 등록된 소개·핵심 기능이 소재를
    # 대신한다. 소재는 필수 값이므로 여기서 브랜드 이름으로 채운다. 화면이 채워 보내게
    # 하지 않는 이유는, 그러면 브랜드를 지운 뒤에도 그 이름이 소재로 남아 있을 수
    # 있어서다 — 채우는 쪽과 지우는 쪽이 같아야 한다.
    #
    # 소재를 적고 브랜드도 고른 글(UTILITY)은 소재가 이미 있으므로 손대지 않는다.
    if profile is not None and not topic.strip():
        payload["topic"] = profile.name
    return payload, limit
