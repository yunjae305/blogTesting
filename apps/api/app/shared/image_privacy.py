"""사진에 찍힌 개인정보를 검은 사각형으로 덮는다.

왜 있나
-------
2026-08-07, 야간 드라이브 글에 올린 주차장 사진에 **차량 번호판이 그대로 읽혔다**.
사용자가 올린 사진은 `_reference_images`가 원본 그대로 글에 다시 싣기 때문에, 찍힌
것이 그대로 발행된다. 번호판·전화번호·생년월일은 한 번 올라가면 회수할 수 없다.

무엇을 덮나
-----------
어디를 덮을지는 **모델이 정한다**(근거 프로필을 만들 때 이미 이미지를 보고 있어서,
호출을 새로 만들지 않고 그 판정에 얹었다 — `llm/schemas.py`의 `privateRegions`).
여기서는 받은 좌표를 그림 위에 실제로 칠하는 일만 한다. 판단과 칠하기를 나눠 둔
이유는, 칠하기는 모델 없이 테스트할 수 있어야 하기 때문이다.

왜 검은 사각형인가
------------------
블러는 **되돌릴 수 있다**. 약한 블러에서 번호판 글자를 복원하는 것은 알려진 기법이고,
모자이크도 마찬가지다. 픽셀을 지우는 것만이 되돌릴 수 없다. 사용자가 요청한 것도
검은 사각형이다.

무엇을 안 하나
--------------
**얼굴은 덮지 않는다.** 요청받은 것은 번호판·전화번호·생년월일 같은 '신상 정보'이고,
얼굴까지 덮으면 인물 사진을 올린 글이 통째로 망가진다.

**원본을 고치지 않는다.** 덮은 것은 글에 싣는 사본이다. 참고자료로 저장된 원본은
그대로 두어, 사용자가 자기가 올린 것을 다시 볼 수 있게 한다.
"""

import io
import logging

from pydantic import BaseModel, Field

from .image_bytes import data_url_parts, load_safe_image, to_data_url

logger = logging.getLogger(__name__)

#: 모델이 준 상자를 사방으로 이만큼 넓힌다(짧은 변 기준 비율).
#:
#: 모델의 좌표는 **정확하지 않다.** 번호판 상자가 한두 글자만큼 짧게 잡히면 남은 글자가
#: 그대로 읽히는데, 그러면 덮은 의미가 없다. 넓게 잡아 글자를 더 지우는 쪽이, 좁게 잡아
#: 번호를 남기는 쪽보다 언제나 낫다.
REGION_MARGIN = 0.04

#: 이보다 큰 상자는 버린다(넓이 비율). 모델이 가끔 "이미지 전체"를 돌려주는데, 그대로
#: 칠하면 사진이 검은 사각형 하나가 된다. 그건 가린 것이 아니라 잃은 것이다.
MAX_REGION_AREA = 0.6

#: 덮은 자리의 색. 완전한 검정이다 — 이 픽셀에는 원본 정보가 남지 않는다.
MASK_COLOR = (0, 0, 0)


class PrivateRegion(BaseModel):
    """사진에서 개인정보가 보이는 자리. 좌표는 **이미지 크기 대비 0~1 비율**이다.

    픽셀이 아니라 비율인 이유: 모델에 보내는 이미지는 긴 변 1568px로 줄여서 보내므로
    (`imaging.prepare_anthropic_image`), 픽셀 좌표를 받으면 원본에 맞지 않는다.
    """

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    #: 무엇을 덮는지(번호판·전화번호 등). 로그와 사용자 안내에만 쓴다.
    kind: str = ""


def _usable(region: PrivateRegion) -> bool:
    """칠해도 되는 상자인가. 이미지를 통째로 덮는 상자는 버린다."""
    if region.x >= 1.0 or region.y >= 1.0:
        return False
    return region.width * region.height <= MAX_REGION_AREA


def _box(region: PrivateRegion, width: int, height: int) -> tuple[int, int, int, int]:
    """비율 상자를 픽셀 상자로. 사방으로 여유를 주고 이미지 밖으로 나가지 않게 자른다."""
    margin = REGION_MARGIN * min(width, height)
    left = region.x * width - margin
    top = region.y * height - margin
    right = (region.x + region.width) * width + margin
    bottom = (region.y + region.height) * height + margin
    return (
        max(0, round(left)),
        max(0, round(top)),
        min(width, round(right)),
        min(height, round(bottom)),
    )


def mask_regions(raw: bytes, regions: list[PrivateRegion]) -> bytes | None:
    """사진 위 해당 자리를 검게 칠한 바이트를 돌려준다.

    덮을 영역이 없으면 원본을 그대로 돌려준다. 영역이 있는데 열기·좌표 검증·저장 중 하나라도
    실패하면 ``None``이다. 원본과 실패를 같은 값으로 돌려주면 호출부가 개인정보가 남은
    사진을 정상 결과로 오인해 게시하므로 fail-closed 결과를 명시적으로 구분한다.

    투명도는 잃는다(JPEG으로 저장한다). 덮을 자리가 있는 사진은 사람이 찍은 사진이고,
    투명한 PNG에 번호판이 찍혀 있는 경우는 실제로 없다.
    """
    if not regions:
        return raw
    if not raw:
        return None

    usable = [region for region in regions if _usable(region)]
    if not usable:
        logger.warning("개인정보 가리기 실패 - 안전하게 칠할 수 있는 영역이 없습니다")
        return None

    try:
        from PIL import ImageDraw

        source, _format_name = load_safe_image(raw)
        image = source.convert("RGB")
        draw = ImageDraw.Draw(image)
        for region in usable:
            draw.rectangle(_box(region, image.width, image.height), fill=MASK_COLOR)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88, optimize=True)
    except Exception as error:  # noqa: BLE001 - 실패 결과는 None이다
        logger.warning("개인정보 가리기 실패(사진 제외 필요) | %s", error)
        return None

    return buffer.getvalue()


def mask_data_url(data_url: str, regions: list[PrivateRegion]) -> str | None:
    """data URL을 받아 덮은 data URL로. 영역이 있는데 덮지 못하면 ``None``이다."""
    if not regions:
        return data_url
    raw, _mime = data_url_parts(data_url)
    if not raw:
        return None
    masked = mask_regions(raw, regions)
    if masked is None:
        return None
    # 칠한 결과는 항상 새 JPEG이다. 선언한 mime을 그대로 두면 받는 쪽이 PNG인 줄 알고 연다.
    return to_data_url(masked, "image/jpeg")
