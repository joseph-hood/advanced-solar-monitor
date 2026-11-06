import yaml

from eg4mon.settings import REPO_ROOT
from eg4mon.sources.solar_assistant import TopicAssembler


def test_assembler_emits_on_trigger_with_latest_voltage():
    tm = yaml.safe_load((REPO_ROOT / "config" / "topics.yaml").read_text())
    a = TopicAssembler(tm)
    pre = "solar_assistant/"
    assert a.feed(pre + "battery_1/power/state", b"1200", 1.0) is None  # no voltage yet
    assert a.feed(pre + "battery_1/voltage/state", b"53.4", 2.0) is None  # not a trigger
    a.feed(pre + "inverter_1/device_mode/state", b"Solar/Battery", 2.5)
    s = a.feed(pre + "battery_1/power/state", b"-800", 3.0)
    assert s.ts == 3.0 and s.battery_v == 53.4 and s.battery_p == -800
    assert s.status == "Solar/Battery"
    # one topic may feed several signals (battery current is also the BMS current)
    s = a.feed(pre + "battery_1/current/state", b"-15.2", 4.0)
    assert s.battery_i == -15.2 and s.bms_i == -15.2
    assert a.feed(pre + "battery_1/voltage/state", b"garbage", 5.0) is None


def test_scale_flips_sign():
    a = TopicAssembler({"prefix": "sa", "trigger": ["battery_p"], "numeric": {
        "battery_v": {"topic": "v"}, "battery_p": {"topic": "p", "scale": -1}}})
    a.feed("sa/v", "53", 0)
    assert a.feed("sa/p", "100", 1).battery_p == -100
