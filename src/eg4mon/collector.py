"""Collector process: source -> EstimatorService -> SQLite. The only DB writer."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from .model.lfp_soc_model import ModelParams
from .pipeline.estimator_service import EstimatorService, PipelineConfig
from .settings import Settings
from .store.db import Store

log = logging.getLogger(__name__)

STATUS_KEY = "collector_status"
PRUNE_EVERY_S = 3600.0


def build_source(settings: Settings, replay_retime: bool = False):
    kind = settings.eg4mon_source
    if kind == "eg4_cloud":
        from .sources.eg4_cloud import EG4CloudClient, EG4CloudSource, load_field_map

        client = EG4CloudClient(settings.eg4_base_url, settings.eg4_username,
                                settings.eg4_password.get_secret_value())
        return EG4CloudSource(client, settings.eg4_inverter_serials, settings.eg4_gridboss_serial,
                              load_field_map(settings.eg4_field_map), settings.eg4_poll_seconds)
    if kind == "solar_assistant":
        from .sources.solar_assistant import SolarAssistantSource

        return SolarAssistantSource(settings.mqtt_host, settings.mqtt_port, settings.mqtt_topics_file,
                                    settings.mqtt_username, settings.mqtt_password.get_secret_value())
    if kind == "replay":
        from .sources.replay import ReplaySource

        if not settings.replay_csv:
            raise SystemExit("EG4MON_SOURCE=replay needs REPLAY_CSV")
        return ReplaySource(settings.replay_csv, settings.site_tz, settings.replay_speed, replay_retime)
    raise SystemExit(f"unknown source {kind!r}")


def build_service(settings: Settings, store: Store) -> EstimatorService:
    return EstimatorService(store, ModelParams.from_dict(settings.model_params()),
                            PipelineConfig.from_dict(settings.pipeline()), settings.site_tz)


async def run(settings: Settings, source=None, store: Store | None = None) -> None:
    store = store or Store.open(settings.db_path)
    service = build_service(settings, store)
    source = source or build_source(settings)
    log.info("collector: source=%s db=%s params=%s", source.name, settings.db_path, service.params_hash)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-unix
            pass

    last_prune = 0.0
    last_status = 0.0
    n = 0

    def write_status(force: bool = False) -> None:
        nonlocal last_status
        now = time.time()
        if force or now - last_status >= 15:
            store.put_kv(STATUS_KEY, {
                **source.status(), "updated": now, "samples": n,
                "soc": service.est.soc, "sigma": service.sigma,
                "degraded": service.state.degraded, "params_hash": service.params_hash,
            })
            last_status = now

    async def consume() -> None:
        nonlocal n, last_prune
        async for sample in source.samples():
            if service.process(sample) is not None:
                n += 1
            write_status()
            days = service.cfg.raw_retention_days
            if days and time.time() - last_prune > PRUNE_EVERY_S:
                removed = store.prune(time.time() - days * 86400)
                last_prune = time.time()
                if removed:
                    log.info("pruned %d readings older than %s days", removed, days)
            if stop.is_set():
                break

    async def heartbeat() -> None:
        while not stop.is_set():
            write_status(force=True)
            await asyncio.sleep(15)

    consumer = asyncio.create_task(consume())
    hb = asyncio.create_task(heartbeat())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({consumer, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for t in (consumer, hb, stopper):
        t.cancel()
    await asyncio.gather(consumer, hb, stopper, return_exceptions=True)
    if consumer.done() and not consumer.cancelled() and consumer.exception():
        raise consumer.exception()
    write_status(force=True)
    log.info("collector stopped after %d samples", n)
