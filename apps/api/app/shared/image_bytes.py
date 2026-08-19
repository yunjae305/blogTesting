"""이미지를 저장할 모양으로 줄이고, 읽을 때 되돌린다.

왜 있나
-------
글 하나를 여는 데 오가는 것의 **85%가 이미지**였다(2026-08-06 실측: 이미지 970KB,
참고자료 144KB, 글자 104KB). 실측한 회선이 0.09MB/s여서 글 하나에 11초, 무거운 글은
20초 제한을 넘겨 아예 안 열렸다.

이미지 자체를 재 봤더니 1200x675 JPEG, 장당 base64 204KB였다. 두 가지를 바꾼다.

    base64 → 이진      204KB → 153KB   화질 손실 0 (base64는 원본보다 33% 크다)
    1200px → 900px     153KB →  78KB   블로그 본문 폭이 보통 800~900px이다

둘을 합치면 장당 204KB → 78KB, 글 하나(5장)가 970KB → 390KB다.

무엇을 안 하나
--------------
**WebP로 바꾸지 않는다.** 더 줄지만(38KB) 네이버 에디터·스레드가 WebP data URL을 받는지
확인하지 않았고, 안 받으면 발행이 깨진다. 발행 경로에 넘어가는 것은 지금과 같은
JPEG data URL이다.

**원본보다 키우지 않는다.** 900px보다 작은 이미지는 그대로 둔다. 다시 인코딩하면 화질만
잃고 용량은 안 준다.
"""

import base64
import io

from PIL import Image, ImageOps

#: 저장할 이미지의 최대 가로. 블로그 본문 폭이 보통 800~900px이라 그보다 크면 낭비다.
MAX_IMAGE_WIDTH = 900

#: JPEG 품질. 80은 눈으로 구분하기 어려우면서 1200px 원본의 절반이 된다(실측).
JPEG_QUALITY = 80

#: 외부(provider 응답·사용자 참고자료) 이미지의 압축 바이트 상한. 업로드 입력은 이보다
#: 훨씬 작은 5MB 제한을 따르지만, 이 함수는 provider 응답과 웹 사진도 함께 지키는 마지막
#: 방어선이라 여유를 둔다. 상한을 넘긴 바이트는 Pillow에 넘기기 전에 거부한다.
MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024

#: 압축 해제 뒤 허용할 최대 픽셀 수와 한 변. 8,000×6,000(48MP) 휴대폰 사진은 받되,
#: 작은 압축 파일이 수억 픽셀로 팽창하는 decompression bomb은 디코딩 전에 막는다.
MAX_SOURCE_IMAGE_PIXELS = 50_000_000
MAX_SOURCE_IMAGE_EDGE = 12_000

#: 줄이지 못했을 때 쓰는 형식. data URL에 종류가 없으면 이것으로 본다.
DEFAULT_MIME = "image/jpeg"

_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


class UnsafeImageError(ValueError):
    """열 수 없거나 운영 상한을 넘겨 provider·발행 경로에 써서는 안 되는 이미지."""


def image_mime_type(format_name: str | None) -> str | None:
    """Pillow가 실제 바이트에서 판별한 형식을 MIME으로 바꾼다."""
    return _MIME_BY_FORMAT.get((format_name or "").upper())


def load_safe_image(raw: bytes) -> tuple[Image.Image, str]:
    """외부 이미지 바이트를 검증하고 방향·메타데이터가 정리된 픽셀만 돌려준다.

    선언된 data URL MIME은 보지 않는다. Pillow가 실제 바이트에서 판별한 형식을 반환하며,
    EXIF orientation은 픽셀에 반영한다. 반환 이미지의 ``info``는 비워 EXIF·GPS·XMP·PNG
    텍스트가 뒤이은 저장에 우연히 복사되지 않게 한다.

    실패 시 원본을 돌려주지 않는다. 호출부가 참고 이미지를 생략하거나 provider 결과를
    실패 처리할 수 있도록 ``UnsafeImageError``로 명확히 구분한다.
    """
    if not raw:
        raise UnsafeImageError("이미지 바이트가 비어 있습니다")
    if len(raw) > MAX_SOURCE_IMAGE_BYTES:
        raise UnsafeImageError(
            f"이미지 압축 바이트가 상한을 넘습니다: {len(raw)} > {MAX_SOURCE_IMAGE_BYTES}"
        )

    try:
        with Image.open(io.BytesIO(raw)) as source:
            format_name = (source.format or "").upper()
            width, height = source.size
            if width < 1 or height < 1:
                raise UnsafeImageError("이미지 크기가 올바르지 않습니다")
            if max(width, height) > MAX_SOURCE_IMAGE_EDGE:
                raise UnsafeImageError(
                    f"이미지 한 변이 상한을 넘습니다: {width}x{height}"
                )
            if width * height > MAX_SOURCE_IMAGE_PIXELS:
                raise UnsafeImageError(
                    f"이미지 픽셀 수가 상한을 넘습니다: {width}x{height}"
                )

            # 헤더만 연 상태에서 끝내지 않는다. 잘린 파일·CRC 오류도 여기서 실패한다.
            source.load()
            oriented = ImageOps.exif_transpose(source)
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            clean = oriented.convert("RGBA") if has_alpha else oriented.copy()
            clean.info.clear()
    except UnsafeImageError:
        raise
    except Exception as error:  # Pillow는 형식마다 다른 예외를 던진다.
        raise UnsafeImageError(f"이미지를 안전하게 열 수 없습니다: {error}") from error

    return clean, format_name


def normalize_image_bytes(
    raw: bytes,
    *,
    max_edge: int | None = None,
    output_format: str = "canonical",
) -> tuple[bytes, str]:
    """외부 이미지를 새 파일로 재인코딩해 메타데이터 없는 실제 MIME 바이트로 만든다.

    ``shrink``와 달리 작은 이미지도 반드시 다시 저장한다. 사용자 참고자료·provider 입력은
    이 함수를 거쳐야 EXIF/GPS가 남지 않는다.

    - ``canonical``: JPEG는 JPEG, 나머지는 무손실 PNG. 발행·저장 참조용 기본값.
    - ``provider``: JPEG·PNG·WebP를 유지하고 GIF/기타는 PNG. 전송량을 아끼는 LLM 입력용.
    - ``png``: OpenAI image edit처럼 PNG가 필요한 경로.
    """
    if output_format not in {"canonical", "provider", "png"}:
        raise ValueError(f"지원하지 않는 이미지 출력 형식: {output_format}")

    image, source_format = load_safe_image(raw)
    if max_edge is not None:
        if max_edge < 1:
            raise ValueError("max_edge는 1 이상이어야 합니다")
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)

    if output_format == "png":
        target_format = "PNG"
    elif source_format == "JPEG":
        target_format = "JPEG"
    elif output_format == "provider" and source_format == "WEBP":
        target_format = "WEBP"
    else:
        target_format = "PNG"

    buffer = io.BytesIO()
    if target_format == "JPEG":
        image.convert("RGB").save(buffer, "JPEG", quality=88, optimize=True)
        mime = "image/jpeg"
    elif target_format == "WEBP":
        image.save(buffer, "WEBP", quality=88, method=4)
        mime = "image/webp"
    else:
        image.save(buffer, "PNG", optimize=True)
        mime = "image/png"
    return buffer.getvalue(), mime


def normalize_data_url(
    data_url: str,
    *,
    max_edge: int | None = None,
    output_format: str = "canonical",
) -> str | None:
    """외부 data URL의 선언 MIME을 버리고 안전한 실제 이미지 data URL로 만든다.

    base64 문법, 이미지 디코딩, 압축 바이트·픽셀·한 변 상한 중 하나라도 어기면 ``None``이다.
    호출부는 원본으로 되돌아가지 말고 해당 참조만 제외해야 한다.
    """
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    if "," not in data_url:
        return None
    head, encoded = data_url.split(",", 1)
    if ";base64" not in head.lower():
        return None
    # 압축 바이트 상한보다 큰 base64는 디코딩 자체를 하지 않는다. 그렇지 않으면 거부할
    # 입력 때문에 encoded 문자열 외에 수십 MB의 별도 bytes를 먼저 할당하게 된다.
    max_encoded_length = ((MAX_SOURCE_IMAGE_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded_length:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
        normalized, mime = normalize_image_bytes(
            raw, max_edge=max_edge, output_format=output_format
        )
    except (ValueError, OSError):
        return None
    return to_data_url(normalized, mime)


def data_url_parts(data_url: str) -> tuple[bytes, str]:
    """``data:image/jpeg;base64,...``를 (바이트, 종류)로 나눈다.

    data URL이 아니면 빈 바이트를 돌려준다 — 부르는 쪽이 '줄일 수 없는 것'으로 다룬다.
    """
    if not data_url.startswith("data:") or "," not in data_url:
        return b"", DEFAULT_MIME
    head, encoded = data_url.split(",", 1)
    mime = head[len("data:") :].split(";", 1)[0] or DEFAULT_MIME
    try:
        return base64.b64decode(encoded), mime
    except Exception:  # noqa: BLE001 - 망가진 base64는 '줄일 수 없는 것'이다
        return b"", mime


def to_data_url(raw: bytes, mime: str) -> str:
    """이진 바이트를 발행 경로가 받는 data URL로 되돌린다.

    네이버·스레드에 넘어가는 값의 모양은 지금과 같다 — 발행 코드는 바뀌지 않는다.
    """
    return f"data:{mime or DEFAULT_MIME};base64,{base64.b64encode(raw).decode('ascii')}"


def shrink(data_url: str) -> tuple[bytes, str]:
    """저장할 모양으로 줄인다. (바이트, 종류)를 돌려준다.

    **줄일 수 없으면 있는 그대로 돌려준다.** 열지 못하는 형식이거나 이미 작으면 손대지
    않는다 — 다시 인코딩하면 화질만 잃는다.
    """
    raw, mime = data_url_parts(data_url)
    if not raw:
        return raw, mime

    try:
        image, format_name = load_safe_image(raw)
        actual_mime = image_mime_type(format_name) or mime
        if image.width <= MAX_IMAGE_WIDTH:
            # 크기가 이미 맞으면 재인코딩하지 않는다. 다만 선언 MIME이 거짓이면 실제 바이트
            # 형식으로 바로잡아 읽는 쪽이 잘못된 디코더를 고르지 않게 한다.
            return raw, actual_mime
        height = round(image.height * MAX_IMAGE_WIDTH / image.width)
        smaller = image.resize((MAX_IMAGE_WIDTH, height), Image.LANCZOS)
        buffer = io.BytesIO()
        if format_name == "PNG":
            # 코드 렌더 표·그래프는 고대비 한글과 1px 선이 핵심이다. JPEG q80으로 바꾸면
            # 글자 가장자리와 격자에 모스키토 노이즈가 생기므로 PNG인 것은 끝까지 PNG다.
            smaller.save(buffer, "PNG", optimize=True)
            output_mime = "image/png"
        else:
            smaller.convert("RGB").save(
                buffer, "JPEG", quality=JPEG_QUALITY, optimize=True
            )
            output_mime = "image/jpeg"
        shrunk = buffer.getvalue()
    except UnsafeImageError:
        # 저장 호환 경로의 기존 계약은 유지한다. provider·참조 경로는 load_safe_image를
        # 직접 호출해 fail-closed하고, 구형 DB 행을 다시 저장할 때만 원본을 보존한다.
        return raw, mime

    # JPEG는 줄인 결과가 더 크면 원본을 쓴다. PNG는 저장 폭 계약과 무손실 텍스트 품질이
    # 우선이라 900px 결과를 그대로 쓴다.
    if output_mime == "image/jpeg" and len(shrunk) >= len(raw):
        return raw, mime
    return shrunk, output_mime
