"""프로세스 바깥에 두어야 하는 상태 — 트렌드 노출 이력과 멱등성 키.

메모리에 두면 재시작하면 사라지고, API 서버를 두 대로 늘리면 서버마다 다른 것을 본다.
여기서는 그 두 성질(재시작 생존·프로세스 간 공유)을 가짜 Redis로 고정한다. 진짜 Redis를
띄우지 않는 이유는 테스트가 외부 프로세스에 매달리면 CI에서 먼저 깨지기 때문이다 —
대신 우리가 실제로 쓰는 명령(ZADD·ZRANGEBYSCORE·ZREMRANGEBY*·EXPIRE·SET NX)만 구현한다.
"""

import time


from app.llm.trends.exposure import (
    ExposureSignature,
    InMemoryTrendExposureStore,
    RedisTrendExposureStore,
)

TTL = 3600.0
CAP = 5


class FakeRedis:
    """우리가 쓰는 명령만 구현한 가짜 Redis. 인스턴스 하나 = 서버 하나."""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis is down")

    async def zrangebyscore(self, key, low, high):
        self._check()
        low = float("-inf") if low == "-inf" else float(low)
        high = float("inf") if high == "+inf" else float(high)
        items = self.zsets.get(key, {})
        return [m for m, score in sorted(items.items(), key=lambda i: i[1]) if low <= score <= high]

    async def zcount(self, key, low, high):
        return len(await self.zrangebyscore(key, low, high))

    async def delete(self, key):
        self._check()
        self.zsets.pop(key, None)
        self.strings.pop(key, None)

    async def get(self, key):
        self._check()
        return self.strings.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        if ex:
            self.expires[key] = ex
        return True

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakePipeline:
    """명령을 모았다가 execute에서 한꺼번에 적용한다 — 실제 트랜잭션과 같은 순서 보장."""

    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._queued: list = []

    def zadd(self, key, mapping):
        self._queued.append(("zadd", key, mapping))
        return self

    def zremrangebyscore(self, key, low, high):
        self._queued.append(("zremrangebyscore", key, low, high))
        return self

    def zremrangebyrank(self, key, start, stop):
        self._queued.append(("zremrangebyrank", key, start, stop))
        return self

    def expire(self, key, seconds):
        self._queued.append(("expire", key, seconds))
        return self

    async def execute(self):
        self._redis._check()
        for command in self._queued:
            name = command[0]
            key = command[1]
            if name == "zadd":
                self._redis.zsets.setdefault(key, {}).update(command[2])
            elif name == "zremrangebyscore":
                low = float("-inf") if command[2] == "-inf" else float(command[2])
                high = float("inf") if command[3] == "+inf" else float(command[3])
                items = self._redis.zsets.get(key, {})
                for member in [m for m, s in items.items() if low <= s <= high]:
                    items.pop(member, None)
            elif name == "zremrangebyrank":
                items = self._redis.zsets.get(key, {})
                ordered = sorted(items.items(), key=lambda i: i[1])
                # Redis의 순위 범위 규칙: 음수는 끝에서부터 세고, 변환 후 start > stop이면
                # 아무것도 지우지 않는다(파이썬 슬라이스와 달라서 그대로 쓰면 과삭제된다).
                size = len(ordered)
                start = command[2] if command[2] >= 0 else size + command[2]
                stop = command[3] if command[3] >= 0 else size + command[3]
                doomed = [] if start > stop else ordered[max(start, 0) : stop + 1]
                for member, _score in doomed:
                    items.pop(member, None)
            elif name == "expire":
                self._redis.expires[key] = command[2]
        self._queued.clear()


def signature(word: str) -> ExposureSignature:
    return ExposureSignature(word, f"ts:{word}", f"cl:{word}")


def redis_store(redis: FakeRedis, clock=time.time, ttl: float = TTL, cap: int = CAP):
    return RedisTrendExposureStore(redis, ttl, cap, clock=clock)


# --- 트렌드 노출 이력 ---


async def test_an_exposure_is_stored_outside_the_process():
    """노출한 키워드가 Redis에 남아야 다음 요청이 같은 것을 다시 내주지 않는다."""
    redis = FakeRedis()
    store = redis_store(redis)

    await store.remember("user_1:post_1:TRENDING", [signature("폭염")])

    assert redis.zsets  # 프로세스 메모리가 아니라 Redis에 있다
    sets = await store.sets("user_1:post_1:TRENDING")
    assert "폭염" in sets.normalized


async def test_two_api_processes_share_one_history():
    """서버를 두 대로 늘려도 한쪽이 보여준 키워드를 다른 쪽이 다시 보여주면 안 된다."""
    redis = FakeRedis()
    first, second = redis_store(redis), redis_store(redis)

    await first.remember("user_1:post_1:TRENDING", [signature("폭염")])

    assert "폭염" in (await second.sets("user_1:post_1:TRENDING")).normalized


async def test_a_restarted_process_still_sees_the_history():
    """재시작 = 저장소 객체를 새로 만드는 것. Redis에 있으므로 이력은 그대로다."""
    redis = FakeRedis()
    await redis_store(redis).remember("user_1:post_1:TRENDING", [signature("폭염")])

    revived = redis_store(redis)

    assert await revived.has_any("user_1:post_1:TRENDING")
    assert "폭염" in (await revived.sets("user_1:post_1:TRENDING")).normalized


async def test_the_same_keyword_twice_is_recorded_once():
    """같은 키워드를 다시 노출하면 항목이 늘지 않고 시각만 갱신된다.

    ZADD가 원자적이라, 동시에 들어온 요청 둘이 같은 키워드를 기록해도 중복이 생기지 않는다.
    """
    redis = FakeRedis()
    store = redis_store(redis)
    key = "user_1:post_1:TRENDING"

    await store.remember(key, [signature("폭염")])
    await store.remember(key, [signature("폭염")])

    stored = next(iter(redis.zsets.values()))
    assert len(stored) == 1


async def test_concurrent_requests_do_not_double_record():
    import asyncio

    redis = FakeRedis()
    store = redis_store(redis)
    key = "user_1:post_1:TRENDING"

    await asyncio.gather(*(store.remember(key, [signature("폭염")]) for _ in range(8)))

    assert len(next(iter(redis.zsets.values()))) == 1


async def test_entries_past_the_ttl_are_not_returned_and_get_swept():
    """보관 기간이 지난 이력은 읽히지도, 남아 있지도 않아야 한다."""
    now = [1000.0]
    redis = FakeRedis()
    store = redis_store(redis, clock=lambda: now[0])
    key = "user_1:post_1:TRENDING"

    await store.remember(key, [signature("어제키워드")])
    now[0] += TTL + 1

    assert (await store.sets(key)).normalized == set()
    assert not await store.has_any(key)

    # 다음 기록 때 만료분이 실제로 지워진다(키가 무한정 자라지 않는 이유).
    await store.remember(key, [signature("오늘키워드")])
    assert list(next(iter(redis.zsets.values()))) == [signature("오늘키워드").encode()]


async def test_the_history_is_capped_so_one_key_cannot_grow_forever():
    now = [1000.0]
    redis = FakeRedis()
    store = redis_store(redis, clock=lambda: now[0])
    key = "user_1:post_1:TRENDING"

    for index in range(CAP + 4):
        now[0] += 1
        await store.remember(key, [signature(f"키워드{index}")])

    stored = next(iter(redis.zsets.values()))
    assert len(stored) == CAP
    # 남는 것은 최근 것이다 — 오래된 쪽부터 잘린다.
    assert signature(f"키워드{CAP + 3}").encode() in stored
    assert signature("키워드0").encode() not in stored


async def test_the_key_itself_expires_so_dormant_users_do_not_pile_up():
    redis = FakeRedis()
    await redis_store(redis).remember("user_1:post_1:TRENDING", [signature("폭염")])

    assert next(iter(redis.expires.values())) == int(TTL)


async def test_users_and_modes_do_not_bleed_into_each_other():
    """다른 사용자·다른 탭의 이력이 섞이면 한쪽에서 본 키워드가 다른 쪽에서 사라진다."""
    redis = FakeRedis()
    store = redis_store(redis)

    await store.remember("user_1:post_1:TRENDING", [signature("폭염")])

    assert (await store.sets("user_2:post_1:TRENDING")).normalized == set()
    assert (await store.sets("user_1:post_1:MATERIAL_RELATED")).normalized == set()
    assert (await store.sets("user_1:post_2:TRENDING")).normalized == set()


async def test_a_redis_outage_degrades_instead_of_breaking_the_panel():
    """Redis가 죽으면 이력을 못 읽을 뿐이다. 같은 키워드가 다시 보일 수는 있어도,
    트렌드 패널이 통째로 실패하면 안 된다 — 빈 패널보다 중복 노출이 낫다."""
    redis = FakeRedis()
    store = redis_store(redis)
    key = "user_1:post_1:TRENDING"
    await store.remember(key, [signature("폭염")])

    redis.fail = True

    assert (await store.sets(key)).normalized == set()  # 예외가 아니라 빈 이력
    await store.remember(key, [signature("장마")])  # 실패해도 예외가 새어 나오지 않는다
    assert "Redis 연결 실패" in store.name


async def test_redis_is_used_again_after_it_recovers():
    now = [1000.0]
    redis = FakeRedis()
    store = redis_store(redis, clock=lambda: now[0])
    key = "user_1:post_1:TRENDING"

    redis.fail = True
    await store.sets(key)
    assert "Redis 연결 실패" in store.name

    redis.fail = False
    now[0] += RedisTrendExposureStore.RETRY_INTERVAL_SECONDS + 1
    await store.remember(key, [signature("폭염")])

    assert store.name == "Redis"
    assert "폭염" in (await store.sets(key)).normalized


async def test_the_memory_store_keeps_the_same_rules():
    """Redis가 없는 배포도 동작이 같아야 한다 — 저장 위치만 다르다."""
    now = [1000.0]
    store = InMemoryTrendExposureStore(TTL, CAP, clock=lambda: now[0])
    key = "user_1:post_1:TRENDING"

    await store.remember(key, [signature("폭염")])
    await store.remember(key, [signature("폭염")])
    assert (await store.sets(key)).normalized == {"폭염"}

    now[0] += TTL + 1
    assert (await store.sets(key)).normalized == set()


def test_ttl_settings_come_from_the_environment_not_the_code(monkeypatch):
    """운영 중 보관 기간을 바꾸려고 배포를 다시 하게 만들면 안 된다."""
    from app import config

    monkeypatch.setenv("TREND_EXPOSURE_TTL_SECONDS", "60")

    assert config.trend_exposure_ttl_seconds() == 60.0


def test_a_broken_ttl_setting_falls_back_instead_of_stopping_the_server(monkeypatch):
    from app import config

    monkeypatch.setenv("TREND_EXPOSURE_TTL_SECONDS", "잘못된값")

    assert config.trend_exposure_ttl_seconds() == 24 * 60 * 60.0


# --- 트렌드 프로바이더와 실제로 연결됐는지 ---


async def test_the_provider_records_and_honours_the_shared_exposure_store():
    """저장소만 만들어 두고 프로바이더가 안 쓰면 아무 의미가 없다.

    같은 저장소를 보는 프로바이더 두 개(=API 서버 두 대)를 세워, 한쪽이 보여준 키워드를
    다른 쪽이 다시 내주지 않는지 본다.
    """
    from app.llm.trends.aggregate import AggregateTrendProvider
    from app.llm.trends.base import TrendSource
    from test_trend_sources import FakeCollector, fetch_input

    redis = FakeRedis()
    words = ["민생지원금", "식중독", "미군", "매매", "참교육", "강풍"]

    def provider():
        return AggregateTrendProvider(
            [FakeCollector(TrendSource.GOOGLE_TRENDS, words)],
            rotate=lambda size: 0,
            exposure=redis_store(redis, cap=50),
        )

    first = await provider().fetch_trends(fetch_input(max_keywords=2))
    shown = {item.keyword for item in first.trend_keywords}
    assert shown

    # 노출 이력이 프로세스가 아니라 Redis에 남았다.
    assert redis.zsets

    # 다른 서버(새 프로바이더)가 같은 이력을 보고 방금 보여준 것을 피한다.
    second = await provider().fetch_trends(fetch_input(max_keywords=2))

    assert shown.isdisjoint({item.keyword for item in second.trend_keywords})


# --- 작업 임차(중복 실행 방지)와 재시작 복구 ---


class LeaseRedis(FakeRedis):
    """임차가 쓰는 명령(SET NX EX / EXISTS / EVAL)을 더한 가짜 Redis."""

    async def exists(self, key):
        self._check()
        return 1 if key in self.strings else 0

    async def eval(self, script, numkeys, key, token, *args):
        self._check()
        if self.strings.get(key) != token:
            return 0
        if "del" in script:
            self.strings.pop(key, None)
        return 1


async def test_only_one_process_can_hold_a_job():
    """같은 글의 원고 생성을 두 프로세스가 동시에 집으면 이미지 모델까지 두 벌 돈다."""
    from app.modules.blog_task.locks import RedisJobLease, lease_key

    redis = LeaseRedis()
    first, second = RedisJobLease(redis), RedisJobLease(redis)
    key = lease_key("post_1", "m4")

    assert await first.acquire(key) is not None
    assert await second.acquire(key) is None


async def test_releasing_only_removes_your_own_lease():
    """만료된 뒤 남이 잡은 임차를 앞선 프로세스가 뒤늦게 풀면 둘이 같이 돌게 된다."""
    from app.modules.blog_task.locks import RedisJobLease, lease_key

    redis = LeaseRedis()
    lease = RedisJobLease(redis)
    key = lease_key("post_1", "m4")

    mine = await lease.acquire(key)
    await lease.release(key, "남의-토큰")
    assert await lease.is_held(key)  # 남의 토큰으로는 못 푼다

    await lease.release(key, mine)
    assert not await lease.is_held(key)


async def test_a_lease_carries_an_expiry_so_a_dead_worker_does_not_block_forever():
    from app.modules.blog_task.locks import DEFAULT_LEASE_SECONDS, RedisJobLease, lease_key

    redis = LeaseRedis()
    await RedisJobLease(redis).acquire(lease_key("post_1", "m4"))

    assert redis.expires[lease_key("post_1", "m4")] == DEFAULT_LEASE_SECONDS


async def test_a_redis_outage_lets_the_job_run_instead_of_blocking_it():
    """Redis가 흔들린다고 원고 생성이 통째로 멈추면 안 된다 — 중복 위험보다 정지가 나쁘다."""
    from app.modules.blog_task.locks import RedisJobLease, lease_key

    redis = LeaseRedis()
    redis.fail = True

    assert await RedisJobLease(redis).acquire(lease_key("post_1", "m4")) is not None


async def test_a_restart_revives_a_post_stuck_in_generating():
    """원고를 만들던 프로세스가 죽으면 글이 GENERATING에 남아 스피너가 영영 돈다.
    직전 상태로 되돌려 사용자가 다시 누를 수 있게 해야 한다."""
    from app.modules.blog_task.locks import NoOpJobLease
    from app.modules.blog_task.recovery import recover_orphaned_tasks
    from app.modules.blog_task.repository import InMemoryBlogTaskRepository
    from app.shared import BlogTaskStatus
    from test_draft_service import build_task

    repository = InMemoryBlogTaskRepository()
    await repository.create(build_task(status=BlogTaskStatus.GENERATING))

    sweep = await recover_orphaned_tasks(repository, NoOpJobLease(), None)

    assert sweep.recovered == 1
    revived = await repository.find_by_post_id("post_1")
    assert revived.status == BlogTaskStatus.INTENT_SELECTED
    assert revived.progress is None


async def test_recovery_leaves_alone_a_job_another_server_is_running():
    """서버 B가 재시작한다고 서버 A가 지금 돌리는 작업을 실패로 만들면 안 된다."""
    from app.modules.blog_task.locks import RedisJobLease, lease_key
    from app.modules.blog_task.recovery import recover_orphaned_tasks
    from app.modules.blog_task.repository import InMemoryBlogTaskRepository
    from app.shared import BlogTaskStatus
    from test_draft_service import build_task

    repository = InMemoryBlogTaskRepository()
    await repository.create(build_task(status=BlogTaskStatus.GENERATING))
    redis = LeaseRedis()
    lease = RedisJobLease(redis)
    await lease.acquire(lease_key("post_1", "m4"))  # 살아 있는 서버가 붙들고 있다

    sweep = await recover_orphaned_tasks(repository, lease, None)

    assert sweep.recovered == 0
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.GENERATING


async def test_a_stuck_verification_gets_a_retryable_failure_instead_of_a_spinner():
    from app.modules.blog_task.locks import NoOpJobLease
    from app.modules.blog_task.recovery import recover_orphaned_tasks
    from app.modules.blog_task.repository import InMemoryBlogTaskRepository
    from app.modules.blog_task.service import _failed_validation_result
    from app.shared import BlogTaskStatus
    from test_draft_service import build_task

    repository = InMemoryBlogTaskRepository()
    await repository.create(build_task(status=BlogTaskStatus.SEARCH_ANALYZING))

    sweep = await recover_orphaned_tasks(repository, NoOpJobLease(), _failed_validation_result)

    assert sweep.recovered == 1
    revived = await repository.find_by_post_id("post_1")
    # 상태는 그대로 두고 실패 사유를 남긴다 — 검증 팝업이 '다시 검증'을 보여주는 자리다.
    assert revived.status == BlogTaskStatus.SEARCH_ANALYZING
    assert revived.intent_validation_result is not None
    assert revived.progress is None


async def test_recovery_leaves_alone_a_generation_that_only_just_started():
    """2026-08-05 실사용 회귀: 돌고 있는 원고 생성을 두 번째 인스턴스가 회수해 버렸다.

    임차가 잠깐 끊겨도(그날은 그랬다) 시작한 지 얼마 안 된 작업은 건드리면 안 된다.
    화면이 '생성 실패'를 띄우는 동안 원고는 멀쩡히 만들어지고 있었다.
    """
    from app.modules.blog_task.locks import NoOpJobLease
    from app.modules.blog_task.recovery import recover_orphaned_tasks
    from app.modules.blog_task.repository import InMemoryBlogTaskRepository
    from app.shared import BlogTaskStatus
    from app.shared.format import now_iso
    from test_draft_service import build_task

    repository = InMemoryBlogTaskRepository()
    # 방금 GENERATING으로 들어갔다(임차는 없다고 나온다 — NoOpJobLease는 늘 그렇다).
    await repository.create(
        build_task(status=BlogTaskStatus.GENERATING, updated_at=now_iso())
    )

    sweep = await recover_orphaned_tasks(repository, NoOpJobLease(), None)

    assert sweep.recovered == 0
    assert sweep.deferred == 1
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.GENERATING


async def test_a_deferred_job_is_recovered_once_the_grace_period_passes():
    """유예는 미루는 것이지 봐주는 것이 아니다 — 정말 죽은 작업은 결국 되살려야 한다."""
    from datetime import datetime, timedelta, timezone

    from app.modules.blog_task.locks import NoOpJobLease
    from app.modules.blog_task.recovery import FRESH_SECONDS, recover_orphaned_tasks
    from app.modules.blog_task.repository import InMemoryBlogTaskRepository
    from app.shared import BlogTaskStatus
    from app.shared.format import now_iso
    from test_draft_service import build_task

    repository = InMemoryBlogTaskRepository()
    await repository.create(
        build_task(status=BlogTaskStatus.GENERATING, updated_at=now_iso())
    )
    later = datetime.now(timezone.utc) + timedelta(seconds=FRESH_SECONDS + 60)

    sweep = await recover_orphaned_tasks(repository, NoOpJobLease(), None, now=later)

    assert sweep.recovered == 1
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.INTENT_SELECTED


async def test_the_grace_period_reads_when_the_status_actually_changed():
    """유예 기준은 '이 상태로 들어온 시각'이다 — 글을 만든 시각이 아니다.

    어제 만들어 둔 글을 오늘 생성하는 경우가 있다. createdAt으로 재면 그 글은 늘
    '오래된 작업'이라 돌고 있어도 회수된다.
    """
    from app.modules.blog_task.locks import NoOpJobLease
    from app.modules.blog_task.recovery import recover_orphaned_tasks
    from app.modules.blog_task.repository import InMemoryBlogTaskRepository
    from app.shared import BlogTaskStatus
    from app.shared.blog_task import StatusHistoryEntry
    from app.shared.format import now_iso
    from test_draft_service import NOW, build_task

    repository = InMemoryBlogTaskRepository()
    await repository.create(
        build_task(
            status=BlogTaskStatus.GENERATING,
            created_at=NOW,  # 1970년에 만든 글
            updated_at=NOW,
            status_history=[
                StatusHistoryEntry.model_validate(
                    {
                        "from": BlogTaskStatus.INTENT_SELECTED,
                        "to": BlogTaskStatus.GENERATING,
                        "at": now_iso(),  # 그러나 생성은 방금 시작했다
                        "by": "system:m4-draft-generation",
                    }
                )
            ],
        )
    )

    sweep = await recover_orphaned_tasks(repository, NoOpJobLease(), None)

    assert sweep.deferred == 1
    assert (await repository.find_by_post_id("post_1")).status == BlogTaskStatus.GENERATING


async def test_a_renew_failure_does_not_end_the_heartbeat():
    """갱신이 한 번 실패했다고 하트비트가 끝나면, 돌고 있는 작업의 임차가 그대로 만료된다.

    그 임차 없음이 복구 스위퍼가 작업을 회수하는 근거가 된다 — 2026-08-05에 그랬다.
    """
    import asyncio

    from app.modules.blog_task.locks import HeldLease

    class FlakyLease:
        """첫 갱신만 실패하고, 다시 잡는 것은 허용한다."""

        def __init__(self):
            self.renews = 0
            self.acquires = 0

        async def renew(self, key, token):
            self.renews += 1
            return self.renews != 1

        async def acquire(self, key):
            self.acquires += 1
            return "새-토큰"

        async def release(self, key, token):
            return None

    lease = FlakyLease()
    held = HeldLease(lease, "key", "옛-토큰", renew_interval=0.01)
    async with held:
        await asyncio.sleep(0.05)

    # 첫 실패 뒤 다시 잡고, 그 뒤로도 계속 갱신했다.
    assert lease.acquires == 1
    assert lease.renews >= 2
