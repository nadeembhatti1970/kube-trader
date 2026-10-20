import json

import redis

from app.settings import get_settings

BAR_STREAM = "market.bars"
SIGNAL_STREAM = "strategy.signals"


def client():
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def publish(stream: str, payload: dict):
    client().xadd(stream, {"data": json.dumps(payload, separators=(",", ":"))}, maxlen=100_000)


def ensure_group(connection, stream: str, group: str):
    try:
        connection.xgroup_create(stream, group, id="0-0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def read_group(connection, group: str, consumer: str, stream: str,
               count: int = 20, block: int = 5000):
    """Recover abandoned pending events before reading new stream entries."""
    claimed = connection.xautoclaim(stream, group, consumer, min_idle_time=30_000,
                                   start_id="0-0", count=count)
    if len(claimed) > 1 and claimed[1]:
        return [(stream, claimed[1])]
    return connection.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block)
