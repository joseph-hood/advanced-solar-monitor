#!/usr/bin/env python3
"""Fake SolarAssistant: publish a CSV onto an MQTT broker using config/topics.yaml.

Lets the solar_assistant source be developed without the Pi:

    mosquitto -p 1883 &
    python scripts/replay_mqtt.py data/simulated.csv --speed 60
    EG4MON_SOURCE=solar_assistant MQTT_HOST=localhost eg4mon collect
"""

import argparse
import asyncio
import time
from pathlib import Path

import aiomqtt
import yaml

from eg4mon.sources.replay import read_csv

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--topics", default=str(ROOT / "config" / "topics.yaml"))
    ap.add_argument("--speed", type=float, default=60.0, help="N x real time (0 = no delay)")
    ap.add_argument("--tz", default="UTC")
    args = ap.parse_args()

    tm = yaml.safe_load(Path(args.topics).read_text())
    prefix = tm.get("prefix", "").strip("/")
    topics = {name: f"{prefix}/{spec['topic']}" for name, spec in tm.get("numeric", {}).items()}
    text_topics = {name: f"{prefix}/{spec['topic']}" for name, spec in tm.get("text", {}).items()}
    triggers = set(tm.get("trigger") or ["battery_p"])
    samples = read_csv(args.csv, args.tz)

    async with aiomqtt.Client(args.host, args.port) as client:
        t0, w0 = samples[0].ts, time.time()
        for s in samples:
            if args.speed > 0:
                await asyncio.sleep(max(0.0, w0 + (s.ts - t0) / args.speed - time.time()))
            values = s.model_dump()
            # publish non-trigger signals first so the trigger sees fresh voltage
            for name in sorted(topics, key=lambda n: n in triggers):
                v = values.get(name)
                if v is not None:
                    scale = float(tm["numeric"][name].get("scale", 1.0))
                    await client.publish(topics[name], str(v / scale))
            for name, topic in text_topics.items():
                if values.get(name):
                    await client.publish(topic, values[name])
        print(f"published {len(samples)} samples")


if __name__ == "__main__":
    asyncio.run(main())
