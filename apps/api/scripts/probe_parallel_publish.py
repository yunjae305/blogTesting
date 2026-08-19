"""동시 발행 시뮬레이션: 사용자 3명이 **동시에** 합성 붙여넣기를 해도 섞이지 않는가.

    python apps/api/scripts/probe_parallel_publish.py

실제 네이버 계정 3개를 요구하지 않는다 — 검증하려는 것은 에디터가 아니라 **격리
구조**이기 때문이다: Chrome 3개(사용자별 세션에 해당)를 띄우고, 각각 paste 이벤트를
기록하는 contenteditable 페이지를 연 뒤, 스레드 3개가 동시에 서로 다른 제목·본문·
이미지를 합성 붙여넣기로 쏜다. 판정:

1. **격리** — 각 페이지에 자기 내용만 들어갔는가(다른 사용자 것이 하나라도 섞이면 실패).
2. **병렬성** — 총 시간이 순차 실행 추정보다 확실히 짧은가(전역 잠금이 남아 있으면
   직렬화되어 길어진다). synthetic 모드는 잠금이 없어야 한다.

실제 SmartEditor가 합성 붙여넣기를 받는지는 probe_synthetic_paste.py(전체)와
probe_synthetic_image_paste.py(이미지)가 판정한다 — 여기서는 다루지 않는다.
"""

import base64
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["NAVER_PASTE_MODE"] = "synthetic"

from app.posting.naver.editor import SmartEditorOne  # noqa: E402

WORKERS = 3
# 붙여넣기당 인위적 처리 시간(초). 병렬성 판정을 위해 각 워커가 이만큼 여러 번 쉰다 —
# 잠금이 있으면 총 시간이 WORKERS배로 늘어나는 것이 또렷해진다.
PASTES_PER_WORKER = 5

# paste 이벤트를 preventDefault로 소비하고 내용을 기록하는 실험 페이지.
_PAGE = """
<meta charset="utf-8">
<div id="pad" contenteditable="true" style="min-height:200px;border:1px solid #888">.</div>
<script>
window.__captured = [];
const pad = document.getElementById('pad');
pad.addEventListener('paste', (event) => {
  event.preventDefault();
  const dt = event.clipboardData;
  window.__captured.push({
    text: dt.getData('text/plain') || '',
    html: dt.getData('text/html') || '',
    files: dt.files.length,
  });
});
pad.focus();
</script>
"""


def _make_driver(index: int):
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    profile = Path(tempfile.mkdtemp(prefix=f"probe-parallel-{index}-"))
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--headless=new")
    options.add_argument("--no-first-run")
    return uc.Chrome(options=options, headless=True)


def _worker(index: int, driver, results: list, errors: list) -> None:
    try:
        marker = f"user-{index}"
        editor = SmartEditorOne(driver)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
        )
        for sequence in range(PASTES_PER_WORKER):
            with editor._paste_guard():  # synthetic이면 잠금이 없어 여기서 줄을 서지 않는다
                editor._pending_paste = {
                    "html": f"<b>{marker}-{sequence}</b>",
                    "text": f"{marker}-{sequence}",
                }
                editor._synthetic_paste(f"{marker} 본문")
                editor._pending_paste = {"image_bytes": png}
                editor._synthetic_paste(f"{marker} 이미지")
                time.sleep(0.3)  # 발행의 반영 확인 대기에 해당 — 잠금이 있으면 직렬화된다
        captured = driver.execute_script("return window.__captured;") or []
        results.append((index, captured))
    except Exception as error:  # noqa: BLE001 — 실험 스크립트: 원인 그대로 보고한다
        errors.append((index, error))


def main() -> int:
    print(f"=== 동시 발행 시뮬레이션 (사용자 {WORKERS}명 · 각 {PASTES_PER_WORKER}회 붙여넣기) ===\n")
    print("Chrome을 하나씩 띄웁니다(드라이버 준비는 순차 — 실측 대상이 아닙니다)…")
    drivers = []
    try:
        for index in range(WORKERS):
            driver = _make_driver(index)
            driver.get("data:text/html;charset=utf-8," + _PAGE.replace("\n", ""))
            drivers.append(driver)
            print(f"  사용자 {index}: 준비 완료")

        results: list = []
        errors: list = []
        threads = [
            threading.Thread(target=_worker, args=(index, driver, results, errors))
            for index, driver in enumerate(drivers)
        ]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started

        if errors:
            for index, error in errors:
                print(f"[실패] 사용자 {index}: {error}", file=sys.stderr)
            return 1

        # 1) 격리: 각 페이지에 자기 marker만 있어야 한다.
        mixed = False
        for index, captured in results:
            texts = " ".join(item.get("text", "") for item in captured)
            own = f"user-{index}"
            others = [f"user-{other}" for other in range(WORKERS) if other != index]
            paste_count = len(captured)
            file_count = sum(item.get("files", 0) for item in captured)
            leaked = [other for other in others if other in texts]
            if leaked or own not in texts:
                mixed = True
                print(f"[실패] 사용자 {index}: 자기 내용 {own in texts} · 섞임 {leaked}")
            else:
                print(
                    f"[통과] 사용자 {index}: paste {paste_count}회 "
                    f"(텍스트 {PASTES_PER_WORKER}·이미지 {PASTES_PER_WORKER}) · 파일 {file_count}건 · 섞임 없음"
                )

        # 2) 병렬성: 순차라면 최소 WORKERS × PASTES_PER_WORKER × 0.3초다.
        serial_floor = WORKERS * PASTES_PER_WORKER * 0.3
        parallel = elapsed < serial_floor * 0.7
        print(
            f"\n총 시간 {elapsed:.1f}초 (순차 하한 {serial_floor:.1f}초) → "
            f"{'병렬로 돌았습니다' if parallel else '직렬화 의심 — 잠금이 남아 있는지 확인하세요'}"
        )

        if mixed or not parallel:
            return 1
        print("\n[판정] 통과 — 세션 격리·무잠금 병렬이 확인됐습니다.")
        return 0
    finally:
        for driver in drivers:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
