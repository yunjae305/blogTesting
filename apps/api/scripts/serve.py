"""운영용 백엔드 실행기 — 긴 uvicorn 명령을 한 줄로 줄인다.

    python apps/api/scripts/serve.py

아래 명령과 같은 서버다. 플래그를 매번 타이핑하지 않으려고 여기 모아 둔다
(개발용 dev.py와 짝 — 그쪽은 reload가 켜져 있고, 운영은 꺼져 있어야 한다).

    python -m uvicorn app.main:app --host 127.0.0.1 --port <PORT> \
        --timeout-graceful-shutdown 5 --app-dir apps/api

포트는 **.env의 PORT 한 곳**에서 읽는다(없으면 3000). uvicorn --port와 .env의
PORT가 어긋나면 화면은 멀쩡한데 이미지 주소가 죽는 사고가 났었다
(docs/서버-설치-절차.md) — 읽는 곳을 하나로 만들면 어긋날 방법이 없다.
"""

import os
import platform
import sys
from pathlib import Path

# Windows 콘솔(cp949)에서 한글 로그가 UnicodeEncodeError로 죽지 않도록.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _disable_quick_edit() -> None:
    """콘솔의 '빠른 편집 모드'를 끈다 — 켜져 있으면 클릭 한 번에 서버가 멈춘다.

    창을 클릭하면 텍스트 선택 모드로 들어가고, 그동안 **출력이 통째로 막힌다.**
    uvicorn은 요청마다 로그를 쓰므로 로그가 막히면 모든 요청이 함께 멈춘다 —
    실사용(2026-08-18): 외부 PC가 무한 로딩이다가 서버 터미널에서 Enter를 치니
    풀렸다. 그 Enter가 선택 모드를 해제한 것이다.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return  # 콘솔이 없다(작업 스케줄러 등) — 걸릴 것도 없다.
        ENABLE_QUICK_EDIT = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        kernel32.SetConsoleMode(
            handle, (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED_FLAGS
        )
    except Exception:
        pass  # 모드를 못 꺼도 서버는 떠야 한다.


_disable_quick_edit()

API_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _env import load_env_file  # noqa: E402

load_env_file()


def _configure_logging() -> None:
    """콘솔 쓰기를 요청 처리에서 떼어낸다 — 콘솔이 막혀도 서버는 돈다.

    Windows 콘솔은 선택 모드(클릭)·느린 렌더링 동안 **쓰기를 통째로 막는다.** 로그를
    이벤트 루프에서 직접 쓰면 그 순간 모든 요청이 함께 멈춘다 — 실사용(2026-08-18):
    터미널에서 Enter를 쳐야 밀린 로그가 쏟아지며 접속이 풀렸다. 빠른 편집 모드를
    꺼도 스크롤·렌더링 지연은 남으므로, 로그는 큐에 넣고 **별도 스레드**가 쓴다.
    콘솔이 막히면 큐만 쌓이고 요청 처리는 계속된다.
    """
    import atexit
    import logging
    import logging.handlers
    import queue

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
        )
    )
    log_queue: "queue.Queue" = queue.Queue(-1)
    listener = logging.handlers.QueueListener(log_queue, console)
    listener.start()
    atexit.register(listener.stop)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(logging.handlers.QueueHandler(log_queue))


def main() -> None:
    import uvicorn

    from app.shutdown import force_exit_after

    _configure_logging()
    port = int(os.environ.get("PORT") or 3000)
    print(f"Blog-it 백엔드를 127.0.0.1:{port} 에서 시작합니다 (.env PORT 기준).")
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        timeout_graceful_shutdown=5,
        # uvicorn의 자체 로깅 설정을 끄고 위의 큐 로깅으로 흘린다(핸들러 없는 로거는
        # 루트로 전파된다).
        log_config=None,
        # 요청마다 한 줄씩 찍던 access 로그는 끈다 — 그 줄이 콘솔을 기다리며 서버를
        # 멈추게 하던 주범이고, 접속 기록은 nginx가 이미 남긴다. 요청당 콘솔 I/O가
        # 0이 되므로 그만큼 빨라진다.
        access_log=False,
    )
    # dev.py와 같은 이유: motor 워커 스레드가 종료를 붙잡는 것을 끊는다.
    force_exit_after()


if __name__ == "__main__":
    main()
