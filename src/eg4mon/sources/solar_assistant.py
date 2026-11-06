"""SolarAssistant MQTT source (future upgrade path).

SolarAssistant publishes each metric as its own message. We keep a latest-value
cache and emit a Sample whenever a trigger topic (battery power or current)
arrives, using the latest voltage (HANDOFF 3.3).

Topic names vary by SolarAssistant version: map them in config/topics.yaml after
capturing real traffic with mosquitto_sub.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml

from .base import Sample, to_float

log = logging.getLogger(__name__)


class TopicAssembler:
    """Pure latest-value cache: feed (topic, payload), get Samples out. No I/O."""

    def __init__(self, topic_map: dict, source: str = "solar_assistant"):
        prefix = (topic_map.get("prefix") or "").strip("/")
        self.source = source
        self.triggers = set(topic_map.get("trigger") or ["battery_p"])
        self.numeric: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self.text: dict[str, list[str]] = defaultdict(list)
        for name, spec in (topic_map.get("numeric") or {}).items():
            self.numeric[_full(prefix, spec["topic"])].append((name, float(spec.get("scale", 1.0))))
        for name, spec in (topic_map.get("text") or {}).items():
            self.text[_full(prefix, spec["topic"])].append(name)
        self.cache: dict[str, Any] = {}

    @property
    def topics(self) -> list[str]:
        return sorted(set(self.numeric) | set(self.text))

    def feed(self, topic: str, payload: bytes | str, ts: float) -> Sample | None:
        raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        fired = False
        for name, scale in self.numeric.get(topic, ()):
            v = to_float(raw.strip())
            if v is None:
                log.debug("non-numeric payload on %s: %r", topic, raw[:50])
                continue
            self.cache[name] = v * scale
            fired |= name in self.triggers
        for name in self.text.get(topic, ()):
            self.cache[name] = raw.strip()
        if fired and self.cache.get("battery_v") is not None:
            return Sample.from_signals(ts, dict(self.cache), self.source)
        return None


def _full(prefix: str, topic: str) -> str:
    topic = topic.strip("/")
    return f"{prefix}/{topic}" if prefix and not topic.startswith(prefix + "/") else topic


class SolarAssistantSource:
    name = "solar_assistant"

    def __init__(self, host: str, port: int, topics_file: Path,
                 username: str = "", password: str = ""):
        with open(topics_file, encoding="utf-8") as f:
            self.assembler = TopicAssembler(yaml.safe_load(f))
        self.host, self.port = host, port
        self.username, self.password = username or None, password or None
        self._status: dict[str, Any] = {"source": self.name, "broker": f"{host}:{port}"}

    def status(self) -> dict:
        return dict(self._status)

    async def samples(self) -> AsyncIterator[Sample]:
        import asyncio

        import aiomqtt  # optional dependency: pip install .[mqtt]

        while True:
            try:
                async with aiomqtt.Client(self.host, self.port, username=self.username,
                                          password=self.password) as client:
                    for t in self.assembler.topics:
                        await client.subscribe(t)
                    self._status.update(connected=True, last_error=None)
                    async for msg in client.messages:
                        now = time.time()
                        self._status["last_ok_ts"] = now
                        s = self.assembler.feed(str(msg.topic), msg.payload, now)
                        if s is not None:
                            yield s
            except aiomqtt.MqttError as e:
                self._status.update(connected=False, last_error=str(e))
                log.warning("MQTT error: %s; reconnecting in 10 s", e)
                await asyncio.sleep(10)
