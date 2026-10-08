import json
import logging
import os
from typing import Optional

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis.platform-core.svc.cluster.local:6379/0")
CACHE_TTL = int(os.environ.get("SUBSTRATE_CACHE_TTL", "300"))  # 5 minutes default
DISABLE_CACHE = os.environ.get("DISABLE_SUBSTRATE_CACHE", "").lower() in ("1", "true", "yes")

_redis: Optional[Redis] = None
_SCAN_COUNT = 500
_DELETE_BATCH_SIZE = 500


def _redis_glob_escape(value: str) -> str:
    """Escape Redis glob metacharacters in a SCAN MATCH pattern segment."""
    return "".join(f"\\{char}" if char in "\\*?[]" else char for char in value)


async def _delete_matching_keys(redis: Redis, pattern: str) -> int:
    deleted = 0
    batch: list[str] = []
    cursor: int | str | bytes = 0

    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=_SCAN_COUNT)
        batch.extend(keys)

        while len(batch) >= _DELETE_BATCH_SIZE:
            deleted += await redis.delete(*batch[:_DELETE_BATCH_SIZE])
            del batch[:_DELETE_BATCH_SIZE]

        if cursor in (0, "0", b"0"):
            break

    if batch:
        deleted += await redis.delete(*batch)

    return deleted


async def get_redis() -> Optional[Redis]:
    global _redis
    if DISABLE_CACHE:
        return None
    if _redis is None:
        try:
            # Socket timeouts so a stalled Redis degrades to a cache miss
            # instead of hanging the request past the caller's deadline.
            _redis = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=5.0,
            )
            await _redis.ping()
            logger.info("Redis cache connected: %s", REDIS_URL)
        except Exception as e:
            logger.warning("Redis cache unavailable: %s", e)
            _redis = None
    return _redis


async def get_cached_beads(cache_key: str) -> Optional[list[dict]]:
    redis = await get_redis()
    if not redis:
        return None
    try:
        data = await redis.get(cache_key)
        if data:
            return json.loads(data)
    except Exception as e:
        logger.warning("Redis read error: %s", e)
    return None


async def set_cached_beads(cache_key: str, beads: list[dict], ttl: int = CACHE_TTL):
    redis = await get_redis()
    if not redis:
        return
    try:
        await redis.set(cache_key, json.dumps(beads), ex=ttl)
    except Exception as e:
        logger.warning("Redis write error: %s", e)


async def invalidate_cache(namespace: Optional[str] = None):
    """Invalidate cached bead reads.

    Namespace writes can delete namespace-scoped keys, but wildcard
    list_beads reads span namespaces and must be invalidated by any write.
    """
    redis = await get_redis()
    if not redis:
        return
    try:
        if namespace is None:
            await redis.flushdb()
            logger.info("Substrate cache invalidated.")
            return

        namespace_pattern = f"beads:{_redis_glob_escape(namespace)}:*"
        wildcard_pattern = r"beads:\*:*"
        deleted = 0
        for pattern in (namespace_pattern, wildcard_pattern):
            deleted += await _delete_matching_keys(redis, pattern)

        logger.info("Substrate cache invalidated for namespace %s (%d keys).", namespace, deleted)
    except Exception as e:
        logger.warning("Redis invalidation error: %s", e)
