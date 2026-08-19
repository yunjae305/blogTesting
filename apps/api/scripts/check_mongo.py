"""앱을 탓하기 전에 MongoDB 연결을 진단한다.

    python apps/api/scripts/check_mongo.py

Atlas는 몇 가지 뻔한 방식으로 실패한다(IP 미등록, 비밀번호 오류, 인코딩되지
않은 비밀번호, URI에 DB 이름 누락). 드라이버가 뱉는 원본 에러는 그중 무엇인지
알려주지 않으므로, 여기서 실제 해결책으로 연결해 준다.
"""

import re
import sys
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from pymongo import MongoClient
from pymongo.errors import ConfigurationError, OperationFailure, ServerSelectionTimeoutError

sys.path.insert(0, str(Path(__file__).parent))
from _env import mongodb_uri  # noqa: E402

EXPECTED_COLLECTIONS = [
    # 사용자 설정은 users.settings 서브도큐먼트로 들어갔다 — 옛 userSettings 컬렉션은 없다.
    "users",
    "persona",
    "blogTask",
    "trend_keywords",
    "material_related_keywords",
]
# init_mongo 없이도 첫 사용 때 런타임이 만드는 컬렉션. 지금은 전부 init_mongo에
# 등록돼 있어 비어 있다 — 새 런타임 컬렉션이 생기면 여기 먼저 올린다.
RUNTIME_COLLECTIONS: list[str] = []


def _redact(uri: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:****@", uri)


def _warn_unencoded_password(uri: str) -> None:
    """비밀번호에 그대로 들어간 @ 나 / 는 조용히 URI 파싱을 깨뜨린다.

    마지막 @ 기준으로 나눈다 — 비밀번호 안에 인코딩 안 된 @ 가 있으면 @ 가
    둘 이상이라는 뜻이고, 마지막 @ 앞부분 전체가 자격 증명이다.
    """
    match = re.match(r"^mongodb(?:\+srv)?://(.+)@[^@/]+", uri)
    if not match:
        return
    credentials = match.group(1)
    if ":" not in credentials:
        return
    _, password = credentials.split(":", 1)
    bad = [c for c in "@/?#" if c in password]
    if bad:
        print(f"  경고: 비밀번호에 {' '.join(bad)} 가 인코딩되지 않은 채 들어 있습니다.")
        print(f"        URL 인코딩이 필요합니다. 예: p@ss/word -> {quote_plus('p@ss/word')}")


def main() -> int:
    uri = mongodb_uri()

    print(f"URI: {_redact(uri)}")
    _warn_unencoded_password(uri)

    is_local = "localhost" in uri or "127.0.0.1" in uri
    print(f"대상: {'이 PC의 로컬 MongoDB (팀원과 공유되지 않음)' if is_local else '원격 (공유 가능)'}")

    # URI에 DB가 없으면 앱이 DB를 고를 수 없으므로, 나중에 드라이버
    # 트레이스백으로 실패하지 말고 여기서 멈춘다.
    if not urlparse(uri).path.lstrip("/").split("?")[0]:
        print("\n실패: URI에 데이터베이스 이름이 없습니다.")
        print("  → 끝에 /blog_it 을 붙이세요. 예: mongodb://localhost:27017/blog_it")
        return 1

    try:
        client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
    except ConfigurationError as error:
        print(f"\n실패 (설정): {error}")
        print("  → mongodb+srv 주소의 호스트명이 맞는지 확인하세요.")
        return 1
    except OperationFailure as error:
        print(f"\n실패 (인증): {error}")
        print("  → Atlas의 Database Access에서 사용자명·비밀번호를 확인하세요.")
        print("  → 비밀번호에 특수문자가 있으면 URL 인코딩이 필요합니다.")
        return 1
    except ServerSelectionTimeoutError as error:
        print(f"\n실패 (접속 불가): {str(error)[:200]}")
        if is_local:
            print("  → MongoDB가 실행 중이 아닙니다. 로컬 MongoDB 서비스를 시작하세요(27017).")
        else:
            print("  → Atlas의 Network Access에 현재 IP가 등록돼 있는지 확인하세요.")
            print("     (재택/카페 등 IP가 바뀌면 다시 등록해야 합니다)")
        return 1

    db = client.get_default_database()
    print(f"\n연결 성공: {db.name}")

    names = db.list_collection_names()
    for collection in EXPECTED_COLLECTIONS:
        if collection in names:
            print(f"  {collection}: {db[collection].count_documents({})} 건")
        else:
            print(f"  {collection}: 없음  → python apps/api/scripts/init_mongo.py 를 실행하세요")

    for collection in RUNTIME_COLLECTIONS:
        if collection in names:
            print(f"  {collection}: {db[collection].count_documents({})} 건")
        else:
            print(f"  {collection}: 없음  → 첫 트렌드 수집 때 자동 생성됩니다")

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
