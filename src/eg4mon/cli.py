"""`eg4mon` command line: collect | api | dashboard | discover | replay | simulate."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from .settings import REPO_ROOT, Settings


def _settings(args) -> Settings:
    s = Settings(_env_file=args.env_file) if args.env_file else Settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return s


def cmd_collect(args) -> None:
    from . import collector

    s = _settings(args)
    asyncio.run(collector.run(s, source=collector.build_source(s, replay_retime=args.retime)))


def cmd_api(args) -> None:
    import uvicorn

    from .api.main import create_app

    s = _settings(args)
    uvicorn.run(create_app(s), host=args.host or s.api_host, port=args.port or s.api_port)


def cmd_dashboard(args) -> None:
    app = REPO_ROOT / "app" / "streamlit_app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app),
           "--server.address", args.host, "--server.port", str(args.port), *args.extra]
    raise SystemExit(subprocess.call(cmd))


def cmd_discover(args) -> None:
    """Log in to the EG4 cloud, list plants/devices and dump raw runtime payloads (M0)."""
    from .sources.eg4_cloud import EG4CloudClient

    s = _settings(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    async def go():
        async with EG4CloudClient(s.eg4_base_url, s.eg4_username, s.eg4_password.get_secret_value()) as c:
            await c.login()
            plants = await c.plants()
            _dump(out / "plants.json", plants)
            serials: list[tuple[str, str]] = []
            for p in plants.get("rows", []):
                pid = p.get("plantId")
                print(f"plant {pid}: {p.get('name')}")
                devs = await c.devices(pid)
                _dump(out / f"devices_{pid}.json", devs)
                for d in devs.get("rows", []):
                    sn = d.get("serialNum")
                    model = d.get("deviceTypeText") or d.get("deviceTypeText4APP") or d.get("deviceType")
                    print(f"  device {sn}: {model} status={d.get('statusText')}")
                    serials.append((sn, str(model)))
            for sn in args.serial or []:
                serials.append((sn, "?"))
            for sn, _model in serials:
                for name, fn in (("runtime", c.inverter_runtime), ("midbox", c.midbox_runtime),
                                 ("energy", c.energy), ("battery", c.battery)):
                    try:
                        _dump(out / f"{name}_{sn}.json", await fn(sn))
                        print(f"  wrote {name}_{sn}.json")
                    except Exception as e:  # noqa: BLE001 - not every endpoint applies to every device
                        print(f"  {name} {sn}: {e}")
        print(f"\nRaw payloads in {out}/ (they contain serial numbers; don't commit them).")
        print("Set EG4_INVERTER_SERIALS / EG4_GRIDBOSS_SERIAL in .env, and check config/eg4_cloud.yaml.")

    asyncio.run(go())


def cmd_replay(args) -> None:
    """Run a CSV through the pipeline and print a summary (optionally write a results CSV)."""
    import csv

    from .api.main import run_replay
    from .sources.replay import read_csv

    s = _settings(args)
    samples = read_csv(args.csv, args.tz or s.site_tz)
    t0 = time.time()
    res = run_replay(samples, s, args.initial_soc)
    rows = res["rows"]
    print(f"{len(samples)} samples -> {len(rows)} steps in {time.time() - t0:.2f}s, "
          f"params {res['params_hash']}")
    if rows:
        print(f"SOC {rows[0]['soc_model']:.3f} -> {rows[-1]['soc_model']:.3f}, "
              f"voltage RMS {res['v_rms']:.4f} V")
    kinds: dict[str, int] = {}
    for e in res["events"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("events:", kinds or "none")
    for c in res["cycles"]:
        print(f"cycle {c['hours']:.1f} h: in {c['ah_in']:.1f} Ah, out {c['ah_out']:.1f} Ah, "
              f"closure {c['closure_ah']:+.1f} Ah")
    if args.out:
        cols = ["ts", "v", "i", "p", "soc_model", "soc_inverter", "v_hat", "v_resid", "pcov", "flags"]
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
        print(f"wrote {args.out}")


def cmd_simulate(args) -> None:
    """Write a synthetic telemetry CSV (for demos and tests without hardware)."""
    from .sim import simulate, write_csv

    s = _settings(args)
    start = time.time() - args.days * 86400 if args.start is None else args.start
    rows = simulate(start, days=args.days, step_s=args.step, soc0=args.soc0, tz=s.site_tz, seed=args.seed)
    write_csv(rows, args.out)
    print(f"wrote {len(rows)} rows to {args.out}")


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="eg4mon", description=__doc__)
    ap.add_argument("--env-file", help="alternative .env file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="run the collector (source -> model -> SQLite)")
    p.add_argument("--retime", action="store_true",
                   help="replay source only: shift timestamps so the recording ends now")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("api", help="run the read API")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("dashboard", help="run the Streamlit dashboard")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("extra", nargs=argparse.REMAINDER, help="extra streamlit args")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("discover", help="EG4 cloud: list devices and dump raw payloads")
    p.add_argument("--out", default="discovery")
    p.add_argument("--serial", action="append", help="extra serial to query (repeatable)")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("replay", help="run a CSV through the pipeline offline")
    p.add_argument("csv")
    p.add_argument("--out", help="write per-step results CSV")
    p.add_argument("--initial-soc", type=float)
    p.add_argument("--tz")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("simulate", help="write a synthetic telemetry CSV")
    p.add_argument("--out", default="data/simulated.csv")
    p.add_argument("--days", type=float, default=5.0)
    p.add_argument("--step", type=float, default=60.0, help="sample period, s")
    p.add_argument("--soc0", type=float, default=0.75)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--start", type=float, help="unix start time (default: ends now)")
    p.set_defaults(func=cmd_simulate)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
