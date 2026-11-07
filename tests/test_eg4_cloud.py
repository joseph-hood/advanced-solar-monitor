import asyncio
import json

import httpx
import pytest

from eg4mon.settings import REPO_ROOT
from eg4mon.sources.eg4_cloud import (
    EG4AuthError,
    EG4CloudClient,
    EG4CloudSource,
    combine,
    load_field_map,
)

FM = load_field_map(REPO_ROOT / "config" / "eg4_cloud.yaml")

RUNTIME = {
    "success": True, "serialNum": "1234567890", "deviceTime": "2026-11-06 12:00:00",
    "vBat": 535, "soc": 86, "pCharge": 3200, "pDisCharge": 0, "ppv1": 2100, "ppv2": 1900,
    "ppv3": 0, "pToUser": 0, "pToGrid": 0, "consumptionPower": 800, "statusText": "PV Charge + Grid off",
    "tinner": 41,
}
MIDBOX = {"success": True, "midboxData": {
    "gridL1ActivePower": 120, "gridL2ActivePower": 80, "loadL1ActivePower": 400,
    "loadL2ActivePower": 450, "upsL1ActivePower": 0, "upsL2ActivePower": 0,
    "smartLoad1L1ActivePower": 163, "smartLoad1L2ActivePower": 0, "smartLoad2L1ActivePower": 301,
    "genL1ActivePower": 0, "genL2ActivePower": 0, "gridRmsVolt": 2405,
    "deviceTime": "2026-11-06 12:00:00",
}}


class FakePortal:
    def __init__(self):
        self.logins = 0
        self.expire_next = False
        self.runtime = dict(RUNTIME)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(x.split("=", 1) for x in request.content.decode().split("&") if x)
        if "json" not in request.headers.get("accept", ""):
            # what the real portal does for a plain form post
            return httpx.Response(200, text="<html><title>Login</title></html>",
                                  headers={"content-type": "text/html;charset=UTF-8"})
        if request.url.path == "/WManage/api/login":
            self.logins += 1
            ok = form.get("password") == "pw"
            return httpx.Response(200, json={"success": ok, "msg": None if ok else "bad password"},
                                  headers={"set-cookie": "JSESSIONID=abc; Path=/"})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
        if request.url.path.endswith("getInverterRuntime"):
            return httpx.Response(200, json=self.runtime)
        if request.url.path.endswith("getMidboxRuntime"):
            return httpx.Response(200, json=MIDBOX)
        return httpx.Response(404, json={"success": False, "msg": "nope"})


def _client(portal, pw="pw"):
    return EG4CloudClient("https://portal.test", "me", pw, transport=httpx.MockTransport(portal))


def test_combine_maps_and_overrides():
    v = combine(FM, [RUNTIME], MIDBOX["midboxData"] and MIDBOX)
    assert v["battery_v"] == pytest.approx(53.5)
    assert v["battery_p"] == 3200
    assert v["pv1_w"] == 2100 and v["pv2_w"] == 1900
    assert v["grid_w"] == 200
    assert v["load_w"] == 850 + 163 + 301  # GridBOSS load port + smart ports; wins over inverter
    assert v["status"] == "PV Charge + Grid off"


def test_combine_parallel_inverters():
    other = {**RUNTIME, "vBat": 537, "pCharge": 1000, "ppv1": 500}
    v = combine(FM, [RUNTIME, other], None)
    assert v["battery_v"] == pytest.approx(53.6)  # averaged
    assert v["battery_p"] == 4200  # summed


def test_poll_dedupe_and_relogin():
    async def go():
        portal = FakePortal()
        src = EG4CloudSource(_client(portal), ["1234567890"], "GB1", FM, poll_seconds=10)
        s1 = await src.poll_once()
        assert s1 is not None and s1.battery_p == 3200 and s1.grid_w == 200
        assert await src.poll_once() is None  # same deviceTime -> stale, not re-emitted
        portal.runtime = {**RUNTIME, "deviceTime": "2026-11-06 12:01:00", "pCharge": 0, "pDisCharge": 900}
        portal.expire_next = True
        s2 = await src.poll_once()
        assert s2.battery_p == -900 and portal.logins == 2
        await src.client.aclose()
    asyncio.run(go())


def test_bad_credentials():
    async def go():
        c = _client(FakePortal(), pw="wrong")
        with pytest.raises(EG4AuthError, match="bad password"):
            await c.login()
        await c.aclose()
    asyncio.run(go())


def test_samples_loop_backs_off_on_errors():
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    def boom(request):
        if request.url.path == "/WManage/api/login":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(500, text=json.dumps({"success": False}),
                              headers={"content-type": "application/json"})

    async def go():
        c = EG4CloudClient("https://portal.test", "me", "pw", transport=httpx.MockTransport(boom))
        src = EG4CloudSource(c, ["1"], "", FM, poll_seconds=10, sleep=fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            async for _ in src.samples():
                pass
        assert sleeps == [10, 20, 40]
        assert src.status()["last_error"]
        await c.aclose()
    asyncio.run(go())


def _html(request):
    return httpx.Response(200, text="<!DOCTYPE html><html><head><title>EG4 Login</title></html>",
                          headers={"content-type": "text/html;charset=UTF-8"})


def test_login_falls_back_to_legacy_path():
    def portal(request):
        if request.url.path == "/WManage/web/login":
            return httpx.Response(200, json={"success": True})
        return _html(request)

    async def go():
        c = EG4CloudClient("https://portal.test", "me", "pw", transport=httpx.MockTransport(portal))
        assert (await c.login())["success"] is True
        await c.aclose()
    asyncio.run(go())


def test_login_html_error_is_diagnosable():
    async def go():
        c = EG4CloudClient("https://portal.test", "me", "secret-pw", transport=httpx.MockTransport(_html))
        with pytest.raises(EG4AuthError) as ei:
            await c.login()
        msg = str(ei.value)
        assert "text/html" in msg and "EG4 Login" in msg and "EG4_BASE_URL" in msg
        assert "secret-pw" not in msg
        await c.aclose()
    asyncio.run(go())
