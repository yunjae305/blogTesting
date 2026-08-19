"""MongoDB 연결. 폴백 허용 여부는 런타임 환경 설정에서 결정한다."""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# 원본 Node는 드라이버의 서버 선택 기본값 30초를 그대로 뒀다. 그래서 mongod가 안 도는
# 개발 머신에서는 in-memory로 물러나기 전까지 시작이 30초간 막혔다. 3초면 로컬 mongod에
# 닿기에 충분하고, 문서에 적힌 "자동으로 물러난다"는 동작을 실제로 빠르게 유지한다.
SERVER_SELECTION_TIMEOUT_MS = 3000

# 응답이 오지 않는 소켓을 **영영 붙들지 않는다.**
#
# motor는 동기 pymongo를 스레드 풀에서 돌린다. 그 워커는 논-데몬이라 인터프리터가 끝날 때
# join된다 — 읽기가 멈춰 있으면 Ctrl+C를 눌러도 서버가 죽지 않는다. 실제로 그랬다
# (2026-08-06, py-spy로 확인: MainThread는 `_python_exit → join`, ThreadPoolExecutor
# 워커들은 `ssl.read`에서 멈춰 있었다). 기본값이 '무제한'이라 한 번 끊긴 연결이 프로세스를
# 통째로 붙잡았다.
#
# **20초로 잡았던 것이 너무 짧았다**(2026-08-06 같은 날 저녁에 드러났다).
#
# 그 값은 '조회' 기준이었다("가장 무거운 예약 목록도 두 번의 왕복이다"). 그런데 이 앱에서
# 가장 무거운 것은 조회가 아니라 **원고 저장**이다: 카드 이미지가 base64로 문서 안에
# 들어가고, `save_draft_generation_result`는 그것을 `draftGenerationResult`와 `finalPost`
# 두 자리에 **두 벌** 쓴다. 한 번의 findAndModify가 1~2MB짜리가 된다.
#
# 실제 로그에서 그 쓰기가 20초 벽에 정확히 두 번 부딪혀 실패했다(pymongo가 쓰기를 한 번
# 재시도하므로 stage=database_save가 41.9초·50.7초). 5분 걸려 만든 원고가 **저장 단계에서
# 통째로 실패**했고, 사용자에게는 '원고 생성 실패'로만 보였다.
#
# 60초로 올린다. 대신 잃는 것도 적어 둔다: 정말 멈춘 소켓이 있으면 종료가 그만큼(최대
# 60초) 늦어진다 — 원래 20초를 고른 이유가 그것이었다. 그래도 '5분짜리 원고를 매번
# 잃는 것'보다는 낫고, '영영 붙들지 않는다'는 애초의 목적은 그대로다.
SOCKET_TIMEOUT_MS = 60000
CONNECT_TIMEOUT_MS = 10000

# **오가는 바이트를 줄인다.** 드라이버는 기본적으로 압축을 쓰지 않는다 — 협상 자체를
# 시도하지 않으므로, 서버가 지원해도 원본 그대로 흐른다.
#
# 이 앱의 문서는 잘 눌린다. 실제로 저장하던 원고 문서(8,280,035바이트)를 zlib으로
# 재 본 결과 6,251,554바이트로, **25% 줄었다**(2026-08-06 측정). 본문이 반복 많은
# 한국어 텍스트이고 base64는 64글자 알파벳만 쓰기 때문이다.
#
# 왜 지금도 켜는가: 팀원의 이미지 외부화(ae56a55)로 본문 문서는 이미 가벼워졌다.
# 그래도 이미지가 옮겨간 `blogTaskImages` 쪽은 여전히 base64 덩어리고, 그것을 읽고
# 쓰는 왕복은 그대로 남았다. 압축은 그 경로에도 똑같이 걸린다.
#
# 대신 무엇을 내주는가: 압축·해제에 CPU를 쓴다. zlib은 그중 가장 느린 축이다
# (zstd·snappy가 빠르지만 각각 별도 패키지가 필요하다 — `pymongo[zstd]`,
# `pymongo[snappy]`). 여기서 병목은 CPU가 아니라 네트워크 왕복이라 zlib으로 둔다.
# 추가 의존성이 없다는 것도 이유다(zlib은 파이썬 표준 라이브러리다).
#
# **여기서 정한 값이 URI를 덮는다.** 확인해 본 결과다(pymongo 4.x):
#   MongoClient("...?compressors=zstd", compressors="zlib") -> ['zlib']
#   MongoClient("...")                                      -> []      (기본은 압축 없음)
# 그러므로 `MONGODB_URI`에 `compressors=`를 적어도 소용이 없다. 끄거나 바꾸려면 이 상수를
# 고쳐야 한다.
COMPRESSORS = "zlib"


async def connect_mongo(uri: str) -> tuple[AsyncIOMotorClient, AsyncIOMotorDatabase]:
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        uri,
        serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
        socketTimeoutMS=SOCKET_TIMEOUT_MS,
        connectTimeoutMS=CONNECT_TIMEOUT_MS,
        compressors=COMPRESSORS,
    )
    # 생성자는 지연 방식이라, 왕복 요청을 강제해서 닿을 수 없는 서버가 첫 쿼리가 아니라
    # 여기서 실패하게 한다.
    await client.admin.command("ping")
    return client, client.get_default_database()


def strip_id(document: dict | None) -> dict | None:
    if document is None:
        return None
    document.pop("_id", None)
    return document
