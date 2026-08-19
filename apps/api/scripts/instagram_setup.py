"""일회성 Graph API Explorer 토큰을 .env에 필요한 두 값으로 바꿔 준다.

    python apps/api/scripts/instagram_setup.py --write-env

M2의 인스타그램 소스는 FACEBOOK_USER_ACCESS_TOKEN과
INSTAGRAM_BUSINESS_ACCOUNT_ID가 필요하다. 둘 다 대시보드에서 찾아볼 수 있는
값이 아니다: 계정 id는 인스타그램 계정이 연결된 페이스북 페이지를 통해 Graph
API로 되읽어야 하고, Graph API Explorer에서 받은 토큰은 약 한 시간이면 만료된다.
이 스크립트는 둘 다 처리한다 — 토큰을 긴 수명 토큰(~60일)으로 교환하고 계정
id를 찾은 뒤, 해시태그 검색을 시험한다. 앱이 그 기능을 승인받지 못했다면 실제로
실패하는 호출이 바로 이것이기 때문이다.

사전 준비물(순서대로):
  1. 인스타그램 계정이 비즈니스 또는 크리에이터 계정이고(개인 계정 아님),
     페이스북 페이지와 연결돼 있어야 한다.
  2. developers.facebook.com에 인스타그램 제품이 추가된 Meta 앱이 있어야 한다.
     그 앱의 Settings > Basic에 있는 FACEBOOK_APP_ID와 FACEBOOK_APP_SECRET을
     .env에 넣는다.
  3. Graph API Explorer에서 그 앱을 고르고 "Generate Access Token"을 눌러
     instagram_basic, pages_show_list, pages_read_engagement 권한을 부여한다.
     출력된 토큰은 실행 뒤 나타나는 숨김 입력 프롬프트에 붙여 넣는다.
"""

import getpass
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _env import load_env_file  # noqa: E402
from _secure_env_write import atomic_update_env  # noqa: E402

GRAPH = "https://graph.facebook.com/v25.0"

# 어떤 해시태그든 상관없다 — 이 호출은 앱이 애초에 그 호출을 할 수 있는지
# 확인하기 위한 것일 뿐이다.
PROBE_HASHTAG = "여행"


class SetupError(Exception):
    pass


def _get(path: str, params: dict[str, str]) -> dict:
    response = httpx.get(f"{GRAPH}{path}", params=params, timeout=30.0)
    payload = response.json() if response.text else {}

    if response.is_error:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        raise SetupError(
            f"{error.get('message') or response.text}\n"
            f"    (code={error.get('code')}, subcode={error.get('error_subcode')})"
        )
    return payload


def exchange_for_long_lived(app_id: str, app_secret: str, short_token: str) -> str:
    payload = _get(
        "/oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    token = payload.get("access_token")
    if not token:
        raise SetupError("Facebook returned no access_token")

    days = int(payload.get("expires_in", 0)) // 86400
    print(f"  긴 수명 토큰 발급 완료 (약 {days}일 유효)" if days else "  긴 수명 토큰 발급 완료")
    return token


def find_instagram_account(token: str) -> tuple[str, str, str]:
    """인스타그램 계정 id는 연결된 페이지를 통해서만 접근할 수 있다."""
    payload = _get(
        "/me/accounts",
        {"fields": "name,instagram_business_account{id,username}", "access_token": token},
    )

    pages = payload.get("data") or []
    if not pages:
        raise SetupError(
            "이 토큰으로 볼 수 있는 페이스북 페이지가 없습니다.\n"
            "    - Graph API Explorer에서 pages_show_list 권한을 켰는지 확인하세요.\n"
            "    - 인스타그램 계정이 연결된 페이스북 페이지가 있어야 합니다."
        )

    linked = [page for page in pages if page.get("instagram_business_account")]
    if not linked:
        names = ", ".join(page.get("name", "?") for page in pages)
        raise SetupError(
            f"페이지는 찾았지만({names}) 인스타그램 비즈니스 계정이 연결된 페이지가 없습니다.\n"
            "    - 인스타그램 앱 > 설정 > 계정 유형에서 '프로페셔널 계정'(비즈니스/크리에이터)으로 바꾸고,\n"
            "    - 페이스북 페이지와 연결하세요."
        )

    if len(linked) > 1:
        print("  연결된 페이지가 여러 개여서 첫 번째 인스타그램 계정을 사용합니다.")

    page = linked[0]
    account = page["instagram_business_account"]
    return account["id"], account.get("username", "?"), page.get("name", "?")


def probe_hashtag_search(token: str, ig_user_id: str) -> None:
    """M2가 실제로 하는 유일한 호출. 앱 검수(App Review)가 필요한 호출이기도 하다."""
    try:
        payload = _get(
            "/ig_hashtag_search",
            {"user_id": ig_user_id, "q": PROBE_HASHTAG, "access_token": token},
        )
    except SetupError as error:
        print("\n  [경고] 해시태그 검색이 거부되었습니다:")
        print(f"    {error}")
        print(
            "\n    해시태그 검색(ig_hashtag_search)은 Meta 앱 검수(App Review)에서\n"
            "    'Instagram Public Content Access' 기능과 instagram_basic 권한을\n"
            "    승인받아야 동작합니다. 토큰과 계정 ID는 올바르게 발급됐으니 .env에\n"
            "    넣어두고, 검수 승인 후 다시 시도하면 코드 수정 없이 켜집니다.\n"
            "    (승인 전까지 M2는 구글 트렌드 + 유튜브만으로 동작합니다.)"
        )
        return

    if payload.get("data"):
        print(f"\n  해시태그 검색 정상 동작 확인 (#{PROBE_HASHTAG} 조회 성공)")
    else:
        print(f"\n  해시태그 검색은 통과했지만 #{PROBE_HASHTAG} 결과가 비어 있습니다.")


def write_env(token: str, user_id: str) -> Path:
    """장기 토큰을 콘솔에 노출하지 않고 로컬 .env에 기록한다."""
    env_path = Path(__file__).resolve().parents[3] / ".env"
    return atomic_update_env(
        env_path,
        {
            "FACEBOOK_USER_ACCESS_TOKEN": token,
            "INSTAGRAM_BUSINESS_ACCOUNT_ID": user_id,
        },
    )


def main() -> int:
    load_env_file()

    if "--write-env" not in sys.argv:
        print(
            "오류: 장기 토큰을 콘솔에 노출하지 않도록 --write-env가 필수입니다.\n"
            "      python apps/api/scripts/instagram_setup.py --write-env"
        )
        return 2
    positional = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
    if positional:
        print("오류: 토큰을 명령행 인자로 넘기지 말고 숨김 입력 프롬프트에 붙여 넣으세요.")
        return 2

    short_token = getpass.getpass("짧은 수명 액세스 토큰(입력 숨김): ").strip()
    if not short_token:
        print("오류: 짧은 수명 액세스 토큰이 필요합니다.")
        return 1
    app_id = os.environ.get("FACEBOOK_APP_ID", "").strip()
    app_secret = os.environ.get("FACEBOOK_APP_SECRET", "").strip()

    if not app_id or not app_secret:
        print("오류: .env에 FACEBOOK_APP_ID와 FACEBOOK_APP_SECRET이 필요합니다.")
        print("      developers.facebook.com > 내 앱 > 설정 > 기본 설정에서 확인할 수 있습니다.")
        return 1

    try:
        print("1) 짧은 수명 토큰을 긴 수명 토큰으로 교환하는 중...")
        long_token = exchange_for_long_lived(app_id, app_secret, short_token)

        print("\n2) 연결된 인스타그램 비즈니스 계정을 찾는 중...")
        ig_user_id, _username = find_instagram_account(long_token)[:2]
        print("  연결 계정 확인 완료")

        print("\n3) 해시태그 검색 권한을 확인하는 중...")
        probe_hashtag_search(long_token, ig_user_id)
    except SetupError as error:
        print(f"\n실패: {error}")
        return 1

    try:
        write_env(long_token, ig_user_id)
    except (OSError, ValueError):
        print(
            "\n실패: .env를 안전하게 기록하지 못했습니다. Windows에서는 제한된 DACL의 "
            "빈 .env를 먼저 만든 뒤 다시 실행하세요."
        )
        return 1
    print("\n" + "=" * 70)
    print(".env에 기록 완료 (토큰과 계정 ID, 로컬 경로는 표시하지 않습니다).")
    print("\n토큰은 약 60일 뒤 만료됩니다. 만료되면 이 스크립트를 다시 실행하세요.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
