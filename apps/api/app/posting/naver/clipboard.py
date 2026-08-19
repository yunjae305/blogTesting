"""제목·본문을 클립보드에 써넣는 함수들.

브라우저 ``navigator.clipboard`` 쓰기는 '문서 포커스'를 요구해 자동화 창에서 실패하거나
멈춘다. 그래서 Windows에서는 OS 클립보드에 직접(CF_HTML+평문) 써넣어 창 포커스 없이도
Ctrl+V로 서식·이미지 태그를 그대로 붙여넣을 수 있게 한다. 비-Windows는 브라우저 API로 폴백.

OS 클립보드는 **기기 전체에 하나뿐**이다. 같은 서버에서 여러 발행이 동시에 돌면
쓰기→Ctrl+V 사이에 서로의 내용을 덮어써 **다른 사람의 글이 붙는다**. 그래서:

- ``use_os_clipboard()``: 쓰기→붙여넣기→확인 구간을 프로세스 안에서 직렬화하는 잠금.
- ``clipboard_still_holds()``: 붙여넣기 직전, 클립보드가 **우리가 넣은 그대로인지**
  바이트를 다시 읽어 SHA-256으로 대조한다. 다르면 붙여넣지 않는다(fail-closed) —
  잠금이 막지 못하는 다른 프로세스(사용자의 복사·화면 캡처, 원격 데스크톱 동기화)를
  여기서 잡는다.
"""

import hashlib
import logging
import platform
import re
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class ClipboardOverwrittenError(RuntimeError):
    """붙여넣기 직전 대조 실패 — 클립보드가 우리가 넣은 내용이 아니다.

    이 예외가 났다는 것은 **아직 아무것도 붙여넣지 않았다**는 뜻이다(fail-closed).
    부르는 쪽은 클립보드에 다시 써넣고 재시도하면 된다.
    """


# 쓰기→붙여넣기→확인을 한 덩어리로 지키는 프로세스 전역 잠금. 발행은 스레드별로 돌므로
# 이 잠금이 프로세스 안의 겹침은 전부 막는다. 재진입(RLock)인 이유: 잠금 구간 안에서
# 부르는 쓰기 함수도 스스로 잠금을 잡기 때문이다(구간 없이 단독으로 불려도 안전하도록).
_CLIPBOARD_LOCK = threading.RLock()

# 마지막으로 OS 클립보드에 써넣은 내용의 지문: {format_id: (sha256, 길이)}.
# 바이트 자체를 들고 있지 않는 이유는 이미지(DIB)가 수 MB이기 때문이다.
_last_os_write: dict[int, tuple[str, int]] | None = None


@contextmanager
def use_os_clipboard():
    """클립보드 쓰기→붙여넣기→반영 확인 구간을 감싸는 잠금.

    붙여넣기(Ctrl+V)는 키를 보낸 순간이 아니라 **에디터가 paste를 처리하는 순간**에
    클립보드를 읽는다. 그래서 키를 보낸 직후에 잠금을 풀면, 다른 발행 스레드가 그 틈에
    클립보드를 갈아치워 엉뚱한 내용이 붙는다. 반영이 DOM으로 확인될 때까지 잠금을
    쥐고 있어야 한다.
    """
    with _CLIPBOARD_LOCK:
        yield


def clipboard_still_holds() -> bool:
    """클립보드가 마지막에 우리가 써넣은 그대로인가 — 붙여넣기 직전의 마지막 관문.

    잠금(use_os_clipboard)은 이 프로세스 안의 겹침만 막는다. 사용자가 서버 화면에서
    복사하거나(특히 Win+Shift+S 캡처), 원격 데스크톱이 클립보드를 동기화하면 밖에서
    바뀐다. 그래서 붙여넣기 직전에 실제 바이트를 다시 읽어 지문을 대조한다.

    OS 클립보드에 쓴 적이 없으면(비-Windows의 브라우저 폴백 경로) 대조할 대상이 없어
    True다 — 그 경로의 보호는 여기 소관이 아니다.
    """
    with _CLIPBOARD_LOCK:
        expected = _last_os_write
        if expected is None:
            return True
        for fmt, (digest, length) in expected.items():
            data = _windows_clipboard_read(fmt)
            if data is None or len(data) < length:
                return False
            if hashlib.sha256(data[:length]).hexdigest() != digest:
                return False
        return True


def _write_browser_clipboard(driver, value: str) -> bool:
    """현재 페이지의 Clipboard API에 한 글자만 기록한다."""
    driver.set_script_timeout(5)
    result = driver.execute_async_script(
        """
        const value = arguments[0], done = arguments[arguments.length - 1];
        navigator.clipboard.writeText(value)
          .then(() => done({ok: true}))
          .catch(error => done({ok: false, error: String(error)}));
        """,
        value,
    )
    return bool(result and result.get("ok"))


_CF_UNICODETEXT = 13
_CF_DIB = 8
_GMEM_MOVEABLE = 0x0002


def _os_clipboard_image(image_bytes: bytes) -> bool:
    """이미지(PNG 등) 바이트를 Windows 클립보드에 CF_DIB로 올린다. 비-Windows는 False.

    네이버는 붙여넣은 이미지의 실제 바이트를 자기 서버에 업로드한다(외부 URL은 그대로
    두어 발행 후 깨진다). PIL로 DIB(파일 헤더 없는 비트맵)로 바꿔 클립보드에 넣으면,
    Ctrl+V 한 번으로 파일 대화상자 없이 인라인 삽입되고 네이버가 이미지를 호스팅한다.
    """
    if platform.system() != "Windows":
        return False
    try:
        from io import BytesIO

        from PIL import Image
    except Exception as error:
        logger.warning("이미지 클립보드: Pillow를 불러오지 못했습니다 (%s)", error)
        return False
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            rgb = image.convert("RGB")
            buffer = BytesIO()
            rgb.save(buffer, format="BMP")
        # BMP 파일 = 14바이트 BITMAPFILEHEADER + DIB. CF_DIB는 그 헤더를 뺀 것.
        dib = buffer.getvalue()[14:]
    except Exception as error:
        logger.warning("이미지 클립보드 변환 실패: %s", error)
        return False
    return _windows_clipboard_write({_CF_DIB: dib})


def _cf_html_payload(fragment: str) -> bytes:
    """CF_HTML 클립보드 포맷 바이트. 헤더의 오프셋은 UTF-8 바이트 기준이다."""
    head = (
        "Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
        "StartFragment:{:010d}\r\nEndFragment:{:010d}\r\n"
    )
    pre, post = "<html><body><!--StartFragment-->", "<!--EndFragment--></body></html>"
    start_html = len(head.format(0, 0, 0, 0).encode("utf-8"))
    start_fragment = start_html + len(pre.encode("utf-8"))
    end_fragment = start_html + len((pre + fragment).encode("utf-8"))
    end_html = start_html + len((pre + fragment + post).encode("utf-8"))
    header = head.format(start_html, end_html, start_fragment, end_fragment)
    return (header + pre + fragment + post).encode("utf-8") + b"\x00"


def _windows_clipboard_write(pairs: dict) -> bool:
    """{format_id: bytes}를 Windows 클립보드에 원자적으로 써넣는다.

    브라우저 navigator.clipboard 쓰기는 '문서 포커스'를 요구해 자동화 창에서 실패하거나
    (NotAllowed / script timeout) 멈춘다. OS 클립보드 쓰기는 창 포커스가 필요 없고, 이후
    Ctrl+V로 붙여넣으면 서식과 <img> 태그가 그대로 에디터의 paste 이벤트로 전달된다.

    성공하면 써넣은 내용의 지문을 기억해 둔다 — ``clipboard_still_holds()``가 붙여넣기
    직전에 이 지문과 실제 클립보드를 대조한다. 지문은 **넘겨받은 모든 포맷**으로 만든다.
    일부 포맷이 조용히 안 들어갔어도(SetClipboardData 실패) 대조가 잡아낸다 — 서식(CF_HTML)
    없이 평문만 붙는 것도 잘못된 발행이다.
    """
    global _last_os_write
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    with _CLIPBOARD_LOCK:
        if not u32.OpenClipboard(None):
            # 쓰기에 실패하면 지문도 지운다. 남겨 두면 부르는 쪽이 브라우저 클립보드로
            # 폴백해 성공한 뒤에도 옛 지문과 대조해 영원히 어긋난다.
            _last_os_write = None
            return False
        try:
            u32.EmptyClipboard()
            for fmt, data in pairs.items():
                if not data:
                    continue
                handle = k32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
                if not handle:
                    continue
                pointer = k32.GlobalLock(handle)
                ctypes.memmove(pointer, data, len(data))
                k32.GlobalUnlock(handle)
                u32.SetClipboardData(fmt, handle)
            _last_os_write = {
                fmt: (hashlib.sha256(data).hexdigest(), len(data))
                for fmt, data in pairs.items()
                if data
            }
            return True
        except Exception as error:
            logger.warning("Windows 클립보드 쓰기 실패: %s", error)
            # 어디까지 들어갔는지 알 수 없다 — 지문을 지워, 이전 쓰기의 지문과 우연히
            # 맞아 통과하는 일이 없게 한다(대조는 실패하고 붙여넣기는 막힌다).
            _last_os_write = None
            return False
        finally:
            u32.CloseClipboard()


def _windows_clipboard_read(fmt: int) -> bytes | None:
    """Windows 클립보드에서 해당 포맷의 바이트를 읽는다. 없거나 실패하면 None.

    ``GlobalSize``는 요청보다 큰 할당을 돌려줄 수 있다 — 그래서 대조하는 쪽이
    기억해 둔 길이만큼 앞부분을 잘라 비교한다.
    """
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalSize.restype = ctypes.c_size_t
    k32.GlobalSize.argtypes = [wintypes.HGLOBAL]

    with _CLIPBOARD_LOCK:
        # 다른 앱이 클립보드를 잠깐 열어 둔 순간이면 OpenClipboard가 실패한다. 그
        # 한 번으로 '내용이 바뀌었다'가 되면 멀쩡한 붙여넣기 시도 하나를 버리는
        # 셈이라, 짧게 몇 번 더 두드린다(그래도 안 되면 대조 실패 = 붙여넣기 보류).
        opened = False
        for _ in range(5):
            if u32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            return None
        try:
            handle = u32.GetClipboardData(fmt)
            if not handle:
                return None
            size = k32.GlobalSize(handle)
            pointer = k32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return ctypes.string_at(pointer, size)
            finally:
                k32.GlobalUnlock(handle)
        except Exception as error:
            logger.warning("Windows 클립보드 읽기 실패: %s", error)
            return None
        finally:
            u32.CloseClipboard()


def _register_html_clipboard_format() -> int:
    if platform.system() != "Windows":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        u32.RegisterClipboardFormatW.restype = wintypes.UINT
        u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        return int(u32.RegisterClipboardFormatW("HTML Format"))
    except Exception:
        return 0


def _html_to_text(html: str) -> str:
    """CF_HTML과 함께 넣을 평문 폴백. 태그만 걷어낸 대략적인 텍스트."""
    import html as _html_module

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _html_module.unescape(text).strip()


def _os_clipboard_text(text: str) -> bool:
    """제목처럼 평문만 OS 클립보드에 넣는다 (Windows). 비-Windows는 False."""
    return _windows_clipboard_write(
        {_CF_UNICODETEXT: text.encode("utf-16-le") + b"\x00\x00"}
    )


def _os_clipboard_html(html: str, plain_text: str | None = None) -> bool:
    """본문 rich HTML을 OS 클립보드에 CF_HTML+평문으로 넣는다 (Windows). 비-Windows는 False.

    plain_text를 주면 그것을 평문 폴백으로 쓴다 — 발행 계획의 scaffold_plain_text처럼
    앵커 토큰까지 정확히 담은 평문이 있을 때 태그 걷어내기 추정보다 낫다.
    """
    global _last_os_write
    fmt_html = _register_html_clipboard_format()
    if not fmt_html:
        # 여기서 실패하면 부르는 쪽이 브라우저 클립보드로 폴백한다. 직전 쓰기(제목)의
        # 지문을 남겨 두면 폴백이 성공해도 대조가 영원히 어긋난다 — 지운다
        # (지문 없음 = 대조 통과, _windows_clipboard_write의 실패 경로와 같은 규칙).
        with _CLIPBOARD_LOCK:
            _last_os_write = None
        return False
    text = plain_text if plain_text is not None else _html_to_text(html)
    return _windows_clipboard_write(
        {
            _CF_UNICODETEXT: text.encode("utf-16-le") + b"\x00\x00",
            fmt_html: _cf_html_payload(html),
        }
    )
