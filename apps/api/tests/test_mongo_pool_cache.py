"""MongoPoolCache: 풀 키는 키워드당 문서(trend_keywords), 관련도 등 내부 캐시는 디스크로."""

from app.llm.trends import CollectedKeyword, InMemoryPoolCache, MongoPoolCache

POOL_KEY = "trend:pool:YOUTUBE:::"
RELEVANCE_KEY = "trend:relevance:AIONA::digest"


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        self._docs.sort(key=lambda d: d.get(field, 0), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs) if length is None else list(self._docs[:length])


class FakeCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    @staticmethod
    def _match(doc, query) -> bool:
        for field, cond in query.items():
            if isinstance(cond, dict) and "$in" in cond:
                if doc.get(field) not in cond["$in"]:
                    return False
            elif doc.get(field) != cond:
                return False
        return True

    async def find_one(self, query):
        return next((d for d in self.docs.values() if self._match(d, query)), None)

    def find(self, query, projection=None):
        return FakeCursor([d for d in self.docs.values() if self._match(d, query)])

    async def count_documents(self, query):
        return sum(1 for d in self.docs.values() if self._match(d, query))

    async def update_one(self, query, update, upsert=False):
        _id = query["_id"]
        doc = self.docs.get(_id, {"_id": _id})
        doc.update(update.get("$set", {}))
        for field in update.get("$unset", {}):
            doc.pop(field, None)
        self.docs[_id] = doc

    async def insert_one(self, doc):
        # 실제 Mongo처럼 _id 중복이면 거부한다.
        from pymongo.errors import DuplicateKeyError

        if doc["_id"] in self.docs:
            raise DuplicateKeyError(f"dup key {doc['_id']}")
        self.docs[doc["_id"]] = dict(doc)

    async def delete_one(self, query):
        self.docs.pop(query.get("_id"), None)

    async def delete_many(self, query):
        for key in [k for k, d in self.docs.items() if self._match(d, query)]:
            self.docs.pop(key, None)


class FakeDb:
    def __init__(self):
        self.cols: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


def _kw(word: str, score: float = 50.0, rank: int = 1) -> CollectedKeyword:
    return CollectedKeyword(keyword=word, score=score, rank=rank, category=None)


async def test_get_returns_none_when_absent():
    assert await MongoPoolCache(FakeDb()).get(POOL_KEY) is None


async def test_pool_stores_one_document_per_keyword_with_key_ids_and_seq():
    db = FakeDb()
    clock = [1000.0]
    await MongoPoolCache(db, clock=lambda: clock[0]).set(POOL_KEY, [_kw("스타벅스"), _kw("아메리카노")])

    kw_docs = db["trend_keywords"].docs
    # _id는 key1부터 순번, seq는 그 숫자를 숫자 타입으로 둔 발급용 키(문자열 _id는
    # 사전순이라 "key9">"key10" — seq 없이는 다음 번호를 못 뽑는다).
    assert set(kw_docs) == {"key1", "key2"}
    first = kw_docs["key1"]
    assert first["keyword"] == "스타벅스"
    assert first["source"] == "YOUTUBE"
    assert first["at"] == 1000.0
    assert first["seq"] == 1
    # 풀 키는 source로 대체된다 — 문서에 저장하지 않는다.
    assert "poolKey" not in first


async def test_set_then_get_roundtrip():
    clock = [1000.0]
    cache = MongoPoolCache(FakeDb(), clock=lambda: clock[0])

    await cache.set(POOL_KEY, [_kw("스타벅스"), _kw("아메리카노")])
    clock[0] = 1005.0
    cached = await cache.get(POOL_KEY)

    assert cached is not None
    assert {k.keyword for k in cached.keywords} == {"스타벅스", "아메리카노"}
    assert cached.age_seconds == 5.0


async def test_writes_accumulate_keywords():
    db = FakeDb()
    cache = MongoPoolCache(db)
    await cache.set(POOL_KEY, [_kw("A"), _kw("B")])
    await cache.set(POOL_KEY, [_kw("C"), _kw("A")])  # A는 중복 → 새 문서 없이 갱신만

    cached = await cache.get(POOL_KEY)
    assert {k.keyword for k in cached.keywords} == {"A", "B", "C"}
    # A(key1)·B(key2)는 유지되고 C만 key3으로 추가된다(중복은 새 순번을 쓰지 않는다).
    assert set(db["trend_keywords"].docs) == {"key1", "key2", "key3"}


async def test_existing_doc_keeps_its_id_and_seq_on_update():
    db = FakeDb()
    db["trend_keywords"].docs["key1"] = {
        "_id": "key1",
        "keyword": "A",
        "source": "YOUTUBE",
        "at": 1.0,
        "score": 1.0,
        "seq": 1,
    }

    await MongoPoolCache(db, clock=lambda: 2.0).set(POOL_KEY, [_kw("A", score=9.0)])

    assert db["trend_keywords"].docs["key1"] == {
        "_id": "key1",
        "keyword": "A",
        "source": "YOUTUBE",
        "at": 2.0,
        "score": 9.0,
        "seq": 1,
    }


async def test_persists_without_expiry():
    clock = [0.0]
    cache = MongoPoolCache(FakeDb(), clock=lambda: clock[0])
    await cache.set(POOL_KEY, [_kw("A")])
    clock[0] = 60 * 60 * 24 * 30  # 30일 뒤
    cached = await cache.get(POOL_KEY)
    assert cached is not None and [k.keyword for k in cached.keywords] == ["A"]


async def test_new_keyword_does_not_make_an_old_high_score_keyword_current():
    from app.llm.trends.cache import POOL_TTL_SECONDS

    db = FakeDb()
    now = POOL_TTL_SECONDS + 100.0
    db["trend_keywords"].docs = {
        "key1": {"_id": "key1", "keyword": "old", "source": "YOUTUBE", "at": 1.0, "score": 100},
        "key2": {"_id": "key2", "keyword": "new", "source": "YOUTUBE", "at": now, "score": 40},
    }

    cached = await MongoPoolCache(db, clock=lambda: now).get(POOL_KEY)

    assert cached is not None
    assert [keyword.keyword for keyword in cached.keywords] == ["new"]


async def test_cap_bounds_growth():
    from app.llm.trends.cache import MONGO_POOL_CAP

    cache = MongoPoolCache(FakeDb())
    await cache.set(POOL_KEY, [_kw(f"kw{i}") for i in range(MONGO_POOL_CAP + 50)])
    cached = await cache.get(POOL_KEY)
    assert len(cached.keywords) == MONGO_POOL_CAP


async def test_relevance_stays_out_of_mongo():
    """관련도 등 내부 캐시는 DB 컬렉션을 만들지 않고 폴백(디스크) 캐시로 간다."""
    db = FakeDb()
    fallback = InMemoryPoolCache()
    cache = MongoPoolCache(db, fallback=fallback)
    await cache.set(RELEVANCE_KEY, [_kw("A", score=90), _kw("B", score=80)])

    # trend_keywords는 깨끗하게 유지되고, DB에 다른 컬렉션도 생기지 않는다.
    assert db["trend_keywords"].docs == {}
    assert set(db.cols) == {"trend_keywords"}

    cached = await cache.get(RELEVANCE_KEY)
    assert {k.keyword for k in cached.keywords} == {"A", "B"}
