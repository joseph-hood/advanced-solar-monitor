"""EG4 cloud ("WManage" portal API) polling source.

The API is undocumented. Endpoints follow the portal and the pylxpweb project:

    POST /WManage/api/login                       form: account, password, language
    POST /WManage/web/config/plant/list/viewer    form: page, rows
    POST /WManage/api/inverterOverview/list       form: page, rows, plantId
    POST /WManage/api/inverter/getInverterRuntime form: serialNum
    POST /WManage/api/midbox/getMidboxRuntime     form: serialNum   (GridBOSS)

Auth is a session cookie. An expired session comes back as an HTML login page
(or HTTP 401/302), and we log in again once and retry.

Read-only: this module never calls any of the portal's write endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import yaml

from .base import Sample, apply_field_map

log = logging.getLogger(__name__)

LOGIN = "/WManage/api/login"
LOGIN_LEGACY = "/WManage/web/login"  # older portal path, tried if the API path doesn't answer JSON
PLANTS = "/WManage/web/config/plant/list/viewer"
DEVICES = "/WManage/api/inverterOverview/list"
RUNTIME = "/WManage/api/inverter/getInverterRuntime"
MIDBOX = "/WManage/api/midbox/getMidboxRuntime"
ENERGY = "/WManage/api/inverter/getInverterEnergyInfo"
BATTERY = "/WManage/api/battery/getBatteryInfo"

MIN_POLL_S = 10.0  # be polite to a free cloud service
MAX_BACKOFF_S = 300.0


class EG4AuthError(RuntimeError):
    pass


class EG4ApiError(RuntimeError):
    pass


class EG4CloudClient:
    def __init__(self, base_url: str, username: str, password: str,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0):
        if not username or not password:
            raise EG4AuthError("EG4_USERNAME / EG4_PASSWORD are not set (see .env.example)")
        self._username, self._password = username, password
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={
                "User-Agent": "eg4mon (read-only monitor)",
                # Without these the portal answers its HTML login page (HTTP 200) instead of JSON.
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
            follow_redirects=False,
        )
        self._logged_in = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def login(self) -> dict:
        form = {"account": self._username, "password": self._password, "language": "ENGLISH"}
        problems = []
        for path in (LOGIN, LOGIN_LEGACY):
            r = await self._http.post(path, data=form)
            data = _json_or_none(r)
            if data is None:
                problems.append(f"{path}: {_describe(r)}")
                continue
            if data.get("success"):
                self._logged_in = True
                log.info("EG4 cloud login ok (%s)", path)
                return data
            # A JSON answer is authoritative: wrong account/password, locked account, ...
            msg = data.get("msg") or data.get("message") or f"no message; keys {sorted(data)}"
            raise EG4AuthError(f"EG4 login rejected by {path}: {msg}")
        raise EG4AuthError(
            "EG4 login failed, the portal did not return a JSON login result. "
            "Check EG4_BASE_URL (US: https://monitor.eg4electronics.com). Responses: "
            + " | ".join(problems))

    async def post(self, path: str, data: dict[str, Any]) -> dict:
        if not self._logged_in:
            await self.login()
        for attempt in (0, 1):
            r = await self._http.post(path, data=data)
            body = _json_or_none(r)
            session_lost = r.status_code in (301, 302, 401, 403) or body is None
            if session_lost and attempt == 0:
                log.info("EG4 session expired (HTTP %s); logging in again", r.status_code)
                self._logged_in = False
                await self.login()
                continue
            if body is None:
                raise EG4ApiError(f"{path}: non-JSON response ({_describe(r)})")
            if r.status_code != 200:
                raise EG4ApiError(f"{path}: HTTP {r.status_code}")
            if body.get("success") is False:
                raise EG4ApiError(f"{path}: {body.get('msg') or body.get('message') or body}")
            return body
        raise EG4ApiError(f"{path}: failed after re-login")  # pragma: no cover

    # ---- convenience ----------------------------------------------------------
    async def plants(self) -> dict:
        return await self.post(PLANTS, {"page": 1, "rows": 50})

    async def devices(self, plant_id: str | int) -> dict:
        return await self.post(DEVICES, {
            "page": 1, "rows": 50, "plantId": plant_id, "searchText": "", "statusText": "all",
        })

    async def inverter_runtime(self, serial: str) -> dict:
        return await self.post(RUNTIME, {"serialNum": serial})

    async def midbox_runtime(self, serial: str) -> dict:
        return await self.post(MIDBOX, {"serialNum": serial})

    async def energy(self, serial: str) -> dict:
        return await self.post(ENERGY, {"serialNum": serial})

    async def battery(self, serial: str) -> dict:
        return await self.post(BATTERY, {"serialNum": serial})


def _json_or_none(r: httpx.Response) -> dict | None:
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _describe(r: httpx.Response) -> str:
    """Short, credential-free description of an unexpected response, for error messages."""
    ctype = r.headers.get("content-type", "?")
    where = f" -> {r.headers['location']}" if "location" in r.headers else ""
    snippet = " ".join(r.text[:160].split()) if r.content else "(empty body)"
    return f"HTTP {r.status_code}{where}, {ctype}, body starts: {snippet!r}"


def load_field_map(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def combine(field_map: dict, inverters: list[dict], gridboss: dict | None) -> dict[str, Any]:
    """Map raw runtime payloads (one per inverter, optional GridBOSS) to signals."""
    inv_sec = field_map["inverter"]
    avg_keys = set(inv_sec.get("average") or [])
    per_inv = [apply_field_map(p, inv_sec) for p in inverters]

    values: dict[str, Any] = {}
    for vals in per_inv:
        for k, v in vals.items():
            if isinstance(v, str):
                values.setdefault(k, v)  # status from the first inverter
            else:
                values.setdefault(k, []).append(v)
    for k, v in list(values.items()):
        if isinstance(v, list):
            values[k] = sum(v) / len(v) if k in avg_keys else sum(v)

    if gridboss is not None and "gridboss" in field_map:
        values.update(apply_field_map(gridboss, field_map["gridboss"]))
    return values


class EG4CloudSource:
    name = "eg4_cloud"

    def __init__(self, client: EG4CloudClient, inverter_serials: list[str], gridboss_serial: str,
                 field_map: dict, poll_seconds: float = 30.0, clock=time.time, sleep=asyncio.sleep):
        if not inverter_serials:
            raise ValueError("EG4_INVERTER_SERIALS is empty; run `eg4mon discover` to find them")
        self.client = client
        self.serials = inverter_serials
        self.gridboss_serial = gridboss_serial
        self.field_map = field_map
        self.poll_s = max(MIN_POLL_S, float(poll_seconds))
        self._clock, self._sleep = clock, sleep
        self._last_key: tuple | None = None
        self._status: dict[str, Any] = {"source": self.name, "poll_seconds": self.poll_s}

    def status(self) -> dict:
        return dict(self._status)

    async def poll_once(self) -> Sample | None:
        """One poll. Returns a Sample, or None if the cloud data has not changed."""
        inverters = [await self.client.inverter_runtime(s) for s in self.serials]
        gridboss = None
        if self.gridboss_serial:
            try:
                gridboss = await self.client.midbox_runtime(self.gridboss_serial)
            except EG4ApiError as e:  # GridBOSS is dashboard-only; don't lose the battery sample
                log.warning("GridBOSS runtime failed: %s", e)
                self._status["gridboss_error"] = str(e)

        now = self._clock()
        self._status.update(last_poll_ts=now, last_ok_ts=now, last_error=None)
        fkey = self.field_map["inverter"].get("freshness")
        key = tuple(p.get(fkey) for p in inverters) if fkey else None
        if key is not None and all(k is not None for k in key) and key == self._last_key:
            self._status["stale_polls"] = self._status.get("stale_polls", 0) + 1
            return None
        self._last_key = key
        self._status["stale_polls"] = 0
        self._status["device_time"] = key[0] if key else None

        values = combine(self.field_map, inverters, gridboss)
        return Sample.from_signals(now, values, self.name)

    async def samples(self) -> AsyncIterator[Sample]:
        backoff = self.poll_s
        while True:
            t0 = self._clock()
            try:
                sample = await self.poll_once()
                backoff = self.poll_s
                if sample is not None:
                    yield sample
                delay = max(0.0, self.poll_s - (self._clock() - t0))
            except EG4AuthError as e:
                # Bad credentials won't fix themselves; back off hard but keep trying
                # (a portal outage can also look like an auth failure).
                log.error("%s", e)
                self._status.update(last_poll_ts=self._clock(), last_error=str(e))
                delay = backoff = MAX_BACKOFF_S
            except (httpx.HTTPError, EG4ApiError) as e:
                log.warning("EG4 poll failed: %s (retry in %.0fs)", e, backoff)
                self._status.update(last_poll_ts=self._clock(), last_error=str(e))
                delay = backoff
                backoff = min(MAX_BACKOFF_S, backoff * 2)
            await self._sleep(delay)
