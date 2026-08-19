"""Mongo 연결 옵션.

빠진 값 하나가 서버를 못 죽게 만든 적이 있다(2026-08-06). motor는 동기 pymongo를 스레드
풀에서 돌리고, 그 워커는 논-데몬이라 인터프리터가 끝날 때 join된다. 소켓 시간제한이 없으면
멈춘 읽기 하나가 프로세스를 통째로 붙잡는다 — Ctrl+C를 눌러도 서버가 죽지 않았다.
py-spy로 확인한 그림: MainThread는 `_python_exit → join`, 워커는 `ssl.read`.
"""

import warnings

import pytest

from app.db import mongo as mongo_module


class FakeAdmin:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def command(self, name: str) -> dict:
        self.commands.append(name)
        return {"ok": 1}


class FakeClient:
    def __init__(self, uri: str, **options):
        self.uri = uri
        self.options = options
        self.admin = FakeAdmin()

    def get_default_database(self):
        return "db"


@pytest.mark.asyncio
async def test_연결에_소켓_시간제한을_준다(monkeypatch):
    created: list[FakeClient] = []

    def factory(uri: str, **options):
        client = FakeClient(uri, **options)
        created.append(client)
        return client

    monkeypatch.setattr(mongo_module, "AsyncIOMotorClient", factory)

    client, db = await mongo_module.connect_mongo("mongodb://localhost:27017/blog_it")

    assert db == "db"
    options = created[0].options
    # 셋 다 있어야 한다. 하나라도 빠지면 그쪽 경로에서 무한정 기다린다.
    assert options["socketTimeoutMS"] == mongo_module.SOCKET_TIMEOUT_MS
    assert options["connectTimeoutMS"] == mongo_module.CONNECT_TIMEOUT_MS
    assert options["serverSelectionTimeoutMS"] == mongo_module.SERVER_SELECTION_TIMEOUT_MS
    # 닿을 수 없는 서버는 첫 쿼리가 아니라 연결 시점에 드러나야 한다.
    assert client.admin.commands == ["ping"]


@pytest.mark.asyncio
async def test_연결에_압축을_켠다(monkeypatch):
    """드라이버 기본값은 '압축 없음'이라, 켜라고 하지 않으면 원본 그대로 흐른다."""
    created: list[FakeClient] = []

    def factory(uri: str, **options):
        client = FakeClient(uri, **options)
        created.append(client)
        return client

    monkeypatch.setattr(mongo_module, "AsyncIOMotorClient", factory)

    await mongo_module.connect_mongo("mongodb://localhost:27017/blog_it")

    assert created[0].options["compressors"] == mongo_module.COMPRESSORS


def test_압축은_켜야_켜지고_URI보다_코드가_이긴다():
    """실제 pymongo로 확인한다. 여기 적힌 전제가 드라이버 판올림에 조용히 뒤집히면 잡힌다.

    `MONGODB_URI`에 `compressors=`를 적어도 소용없다는 것이 이 단언의 핵심이다 —
    끄거나 바꾸려면 `mongo.COMPRESSORS`를 고쳐야 한다.
    """
    from pymongo import MongoClient

    # 아무 말도 안 하면 압축은 꺼져 있다.
    plain = MongoClient("mongodb://localhost:27017/blog_it", connect=False)
    assert plain.options.pool_options._compression_settings.compressors == []

    # 켜라고 하면 켜진다.
    zipped = MongoClient(
        "mongodb://localhost:27017/blog_it",
        compressors=mongo_module.COMPRESSORS,
        connect=False,
    )
    assert zipped.options.pool_options._compression_settings.compressors == ["zlib"]

    # URI가 다른 말을 해도 코드가 이긴다.
    # (zstd는 이 환경에 안 깔려 있어 드라이버가 경고를 낸다 — 여기서 재려는 것은
    #  '어느 쪽이 이기는가'뿐이라 그 경고는 덮는다.)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        conflicting = MongoClient(
            "mongodb://localhost:27017/blog_it?compressors=zstd",
            compressors=mongo_module.COMPRESSORS,
            connect=False,
        )
    assert conflicting.options._options.get("compressors") == ["zlib"]
