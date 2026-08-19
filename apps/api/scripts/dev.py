"""개발용 백엔드 서버를 띄운다. 저장소 어디에서 실행해도 동작이 같다.

    python apps/api/scripts/dev.py

아래 명령과 완전히 같은 서버다 — 플래그를 매번 타이핑하지 않으려고 여기 모아 둔다.

    python -m uvicorn app.main:app --reload --reload-dir apps/api \
        --timeout-graceful-shutdown 5 --port 3000 --app-dir apps/api

플래그를 하나씩 지우면 안 되는 이유가 있다. 셋 다 실제로 겪은 문제를 막는 것이다
(2026-08-06 변경내역 참고).

- ``reload_dirs``: ``reload``만 켜면 uvicorn이 저장소 루트 전체를 감시한다. 그 안에는
  ``node_modules``와 네이버 자동화용 크롬 프로필(``.naver-profile-users``)이 있고,
  크롬은 열려 있는 동안 프로필 파일에 계속 쓴다 — 그 파일 하나가 바뀔 때마다 예약
  발행이 도는 서버가 재시작된다. 실제로 감시 프로세스가 CPU 279초를 쓰고 있었다.
- ``timeout_graceful_shutdown``: 없으면 응답을 못 받은 요청 하나가 Ctrl+C를 붙잡고
  서버가 안 꺼진다.
- ``sys.path`` 삽입: CLI의 ``--app-dir apps/api``와 같은 일이다. ``app`` 패키지를
  import 경로에 올려서 저장소 루트에서 실행할 수 있게 한다. ``apps/api``로 들어가서
  실행하면 그 폴더가 곧 import 경로가 되므로 원래 필요 없는 플래그다.

``.env``는 여기서 읽지 않는다. 서버가 ``app.config``에서 저장소 루트를 찾아 직접
읽으며, 그것도 실행 위치와 무관하다.
"""

import sys
from pathlib import Path

import uvicorn

# apps/api — CLI의 --app-dir 자리. 절대 경로로 넣는 이유는 이 스크립트를 저장소
# 루트가 아닌 곳에서 실행해도 같아야 하기 때문이다.
API_DIR = Path(__file__).resolve().parent.parent

# uvicorn CLI의 기본 포트는 8000이라 명령에 --port 3000이 늘 붙어 있었다.
# app.config.DEFAULT_PORT와 같은 값이며, 프런트엔드(Vite) 프록시가 이 포트를 본다.
PORT = 3000


def main() -> None:
    sys.path.insert(0, str(API_DIR))
    # sys.path를 세운 뒤라야 app 패키지를 부를 수 있다(저장소 루트에서 실행하는 경우).
    from app.shutdown import force_exit_after
    # reload를 쓰려면 앱을 import 문자열로 넘겨야 한다. 감시 프로세스가 코드를
    # 다시 import할 자식 프로세스를 띄우기 때문에, 객체를 넘기면 켜지지 않는다.
    uvicorn.run(
        "app.main:app",
        port=PORT,
        reload=True,
        reload_dirs=[str(API_DIR)],
        timeout_graceful_shutdown=5,
    )

    # 여기까지 오면 서버는 완전히 내려갔다. 그런데도 프로세스가 안 죽는 경우가 있다 —
    # motor의 워커 스레드가 데몬이 아니라, 소켓에 걸린 읽기 하나 때문에 인터프리터가
    # 37~122초를 기다린다(2026-08-06 실측). 아무도 그 결과를 쓰지 않는다.
    #
    # **앱이 아니라 여기에 둔다.** lifespan에 두면 앱을 띄웠다 내리는 테스트까지
    # 3초 뒤에 죽는다(실제로 pytest가 20%에서 통째로 끝났다).
    force_exit_after()


if __name__ == "__main__":
    main()
