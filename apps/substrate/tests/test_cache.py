import asyncio
import re

from src import cache


def _redis_glob_to_regex(pattern: str) -> re.Pattern:
    parts = []
    escaped = False
    for char in pattern:
        if escaped:
            parts.append(re.escape(char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    if escaped:
        parts.append(re.escape("\\"))
    return re.compile(f"^{''.join(parts)}$")


class _FakeRedis:
    def __init__(self, keys: list[str]):
        self.store = set(keys)
        self.deleted: list[str] = []
        self.flushes = 0
        self.scan_calls: list[dict] = []
        self.keys_calls = 0
        self._scan_snapshots: dict[str, list[str]] = {}

    async def scan(self, cursor=0, match=None, count=None):
        self.scan_calls.append({"cursor": cursor, "match": match, "count": count})
        cursor = int(cursor)
        if cursor == 0 or match not in self._scan_snapshots:
            pattern = _redis_glob_to_regex(match)
            self._scan_snapshots[match] = sorted(
                key for key in self.store if pattern.match(key)
            )

        keys = self._scan_snapshots[match]
        page_size = count or len(keys) or 1
        page = keys[cursor : cursor + page_size]
        next_cursor = cursor + page_size
        if next_cursor >= len(keys):
            next_cursor = 0
        return next_cursor, page

    async def delete(self, *keys):
        count = 0
        for key in keys:
            if key in self.store:
                self.store.remove(key)
                count += 1
            self.deleted.append(key)
        return count

    async def flushdb(self):
        self.flushes += 1
        self.store.clear()

    async def keys(self, _pattern):
        self.keys_calls += 1
        return []


def _patch_redis(monkeypatch, redis):
    async def get_redis():
        return redis

    monkeypatch.setattr(cache, "get_redis", get_redis)


def test_invalidate_cache_namespace_deletes_namespace_and_wildcard_keys(monkeypatch):
    redis = _FakeRedis(
        [
            "beads:finance:transaction:*:*:*:*:100:0",
            "beads:finance:account:*:*:*:*:100:0",
            "beads:dev:note:*:*:*:*:100:0",
            "beads:*:*:*:*:*:*:100:0",
            "unrelated:key",
        ]
    )
    _patch_redis(monkeypatch, redis)

    asyncio.run(cache.invalidate_cache("finance"))

    assert "beads:finance:transaction:*:*:*:*:100:0" not in redis.store
    assert "beads:finance:account:*:*:*:*:100:0" not in redis.store
    assert "beads:*:*:*:*:*:*:100:0" not in redis.store
    assert "beads:dev:note:*:*:*:*:100:0" in redis.store
    assert "unrelated:key" in redis.store
    assert redis.flushes == 0
    assert redis.keys_calls == 0
    assert {call["match"] for call in redis.scan_calls} == {
        "beads:finance:*",
        r"beads:\*:*",
    }


def test_invalidate_cache_none_flushes_entire_cache(monkeypatch):
    redis = _FakeRedis(
        [
            "beads:finance:transaction:*:*:*:*:100:0",
            "beads:dev:note:*:*:*:*:100:0",
        ]
    )
    _patch_redis(monkeypatch, redis)

    asyncio.run(cache.invalidate_cache(None))

    assert redis.store == set()
    assert redis.flushes == 1
    assert redis.scan_calls == []
    assert redis.keys_calls == 0


def test_invalidate_cache_namespace_uses_scan_pagination(monkeypatch):
    redis = _FakeRedis(
        [
            "beads:finance:transaction:*:*:*:*:100:0",
            "beads:finance:transaction:active:*:*:*:100:0",
            "beads:finance:account:*:*:*:*:100:0",
        ]
    )
    _patch_redis(monkeypatch, redis)
    monkeypatch.setattr(cache, "_SCAN_COUNT", 2)

    asyncio.run(cache.invalidate_cache("finance"))

    assert redis.store == set()
    assert [call["cursor"] for call in redis.scan_calls if call["match"] == "beads:finance:*"] == [
        0,
        2,
    ]
    assert redis.keys_calls == 0
