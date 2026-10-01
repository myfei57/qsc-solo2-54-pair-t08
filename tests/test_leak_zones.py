"""多分区泄漏检测：隔离、恢复策略、并发来源区分、掉线与审计一致性。"""

from __future__ import annotations

import pytest

from hydrorefuel.errors import LeakError


def _leak_entries(platform, action):
    return [
        entry
        for entry in platform.audit.entries()
        if entry["component"] == "leak" and entry["action"] == action
    ]


def test_zone_isolation_leaves_other_zones_running(platform):
    platform.start()
    result = platform.sample_leak_zone("unload", 4500.0)
    assert result["transition"] == "isolated"
    assert result["state"] == "isolated"
    assert result["valve_open"] is False
    assert result["cause"] == "threshold"

    snapshot = platform.leak_zones_snapshot()
    assert snapshot["isolated_zones"] == ["unload"]
    states = {zone["name"]: zone["state"] for zone in snapshot["zones"]}
    assert states == {
        "dispenser": "clear",
        "unload": "isolated",
        "compressor": "clear",
        "buffer": "clear",
    }
    # 其他分区照常采样、照常运行
    other = platform.sample_leak_zone("buffer", 120.0)
    assert other["state"] == "clear"
    assert other["transition"] == ""
    # 有分区被隔离时就绪判据不通过
    assert platform.readiness()["checks"]["leak_latch_clear"] is False
    cycle = platform.cycle(dt=1.0)
    assert cycle["blocked"] is True
    assert cycle["isolated_leak_zones"] == ["unload"]


def test_single_zone_trip_is_single_point_incident(platform):
    platform.sample_leak_zone("unload", 4500.0)
    incidents = platform.leak_incidents()
    assert incidents["total"] == 1
    active = incidents["active"][0]
    assert active["kind"] == "single"
    assert active["zones"] == ["unload"]


def test_concurrent_trips_merge_into_multi_point_incident(platform):
    platform.sample_leak_zone("unload", 4500.0)
    platform.sample_leak_zone("compressor", 5000.0)
    incidents = platform.leak_incidents()
    assert incidents["total"] == 1
    active = incidents["active"][0]
    assert active["kind"] == "multi"
    assert active["zones"] == ["unload", "compressor"]
    # 升级与隔离都落审计，多路来源按分区可区分
    escalations = _leak_entries(platform, "incident_escalated")
    assert len(escalations) == 1
    assert escalations[0]["payload"]["zones"] == ["unload", "compressor"]
    isolated = _leak_entries(platform, "zone_isolated")
    assert {entry["payload"]["zone"] for entry in isolated} == {"unload", "compressor"}
    assert {entry["payload"]["incident"] for entry in isolated} == {active["id"]}


def test_trips_outside_window_are_separate_incidents(platform, clock):
    platform.sample_leak_zone("unload", 4500.0)
    clock.advance(platform.config.leak.incident_window_s + 1.0)
    platform.sample_leak_zone("buffer", 4500.0)
    incidents = platform.leak_incidents()
    assert incidents["total"] == 2
    assert {item["kind"] for item in incidents["active"]} == {"single"}


def test_auto_zone_recovers_only_after_cleared_and_hold_expired(platform, clock):
    platform.sample_leak_zone("unload", 4500.0)
    # 保持期内即使读数回落也不恢复
    during = platform.sample_leak_zone("unload", 100.0)
    assert during["state"] == "isolated"
    assert during["transition"] == ""
    clock.advance(platform.config.leak.hold_s + 1.0)
    recovered = platform.sample_leak_zone("unload", 80.0)
    assert recovered["transition"] == "recovered"
    assert recovered["state"] == "clear"
    assert recovered["valve_open"] is True
    assert platform.leak_zones_snapshot()["isolated_zones"] == []
    recovered_entries = _leak_entries(platform, "zone_recovered")
    assert recovered_entries[0]["payload"]["trigger"] == "auto"


def test_auto_zone_never_recovers_while_leak_active(platform, clock):
    platform.sample_leak_zone("unload", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    still = platform.sample_leak_zone("unload", 6000.0)
    assert still["state"] == "isolated"
    assert still["transition"] == ""
    # 没消干净时人工复位同样被拒
    with pytest.raises(LeakError):
        platform.reset_leak_zone("unload")


def test_manual_zone_waits_for_operator_reset(platform, clock):
    platform.sample_leak_zone("dispenser", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    cleared = platform.sample_leak_zone("dispenser", 90.0)
    assert cleared["state"] == "isolated"
    assert cleared["transition"] == ""
    reset = platform.reset_leak_zone("dispenser", actor="operator-a")
    assert reset["released"] is True
    assert reset["state"] == "clear"
    recovered = _leak_entries(platform, "zone_recovered")
    assert recovered[0]["payload"]["trigger"] == "manual"
    assert recovered[0]["payload"]["actor"] == "operator-a"


def test_probe_offline_is_treated_as_leak(platform, clock):
    result = platform.sample_leak_zone("unload", probe_ok=False)
    assert result["state"] == "isolated"
    assert result["cause"] == "offline"
    assert result["tripped"] is True
    snapshot = platform.leak_zones_snapshot()
    assert snapshot["degraded"] is True
    assert snapshot["isolated_zones"] == ["unload"]
    offline = _leak_entries(platform, "probe_offline")
    assert len(offline) == 1
    assert offline[0]["severity"] == "warning"
    # 掉线期间保持期走完也不允许复位
    clock.advance(platform.config.leak.hold_s + 1.0)
    with pytest.raises(LeakError):
        platform.reset_leak_zone("unload")
    # 探头恢复且读数回落后，auto 分区自动恢复
    recovered = platform.sample_leak_zone("unload", 50.0, probe_ok=True)
    assert recovered["transition"] == "recovered"
    assert platform.leak_zones_snapshot()["degraded"] is False
    assert len(_leak_entries(platform, "probe_recovered")) == 1


def test_probe_recovery_with_high_reading_confirms_real_leak(platform, clock):
    platform.sample_leak_zone("unload", probe_ok=False)
    clock.advance(platform.config.leak.hold_s + 1.0)
    confirmed = platform.sample_leak_zone("unload", 5200.0, probe_ok=True)
    assert confirmed["cause"] == "threshold"
    assert confirmed["state"] == "isolated"


def test_incident_stays_open_until_all_sources_recover(platform, clock):
    platform.sample_leak_zone("unload", 4500.0)
    platform.sample_leak_zone("compressor", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak_zone("unload", 70.0)
    incidents = platform.leak_incidents()
    assert len(incidents["active"]) == 1
    assert incidents["active"][0]["kind"] == "multi"
    platform.sample_leak_zone("compressor", 70.0)
    closed = platform.leak_incidents()
    assert closed["active"] == []
    assert closed["recent_closed"][0]["zones"] == ["unload", "compressor"]


def test_isolation_reset_and_audit_are_consistent(platform, clock):
    platform.sample_leak_zone("buffer", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak_zone("buffer", 60.0)
    platform.reset_leak_zone("buffer")
    isolated = _leak_entries(platform, "zone_isolated")
    recovered = _leak_entries(platform, "zone_recovered")
    assert len(isolated) == 1
    assert len(recovered) == 1
    assert isolated[0]["payload"]["zone"] == recovered[0]["payload"]["zone"] == "buffer"
    assert isolated[0]["payload"]["incident"] == recovered[0]["payload"]["incident"]
    # 审计与控制台快照口径一致：全部恢复后无隔离分区、事件已结案
    snapshot = platform.leak_zones_snapshot()
    assert snapshot["isolated_zones"] == []
    assert snapshot["incidents"]["active"] == []
    assert snapshot["incidents"]["recent_closed"][0]["zones"] == ["buffer"]


def test_unknown_zone_is_rejected(platform):
    with pytest.raises(LeakError):
        platform.sample_leak_zone("nowhere", 100.0)
    with pytest.raises(LeakError):
        platform.reset_leak_zone("nowhere")


def test_online_sample_requires_reading(platform):
    with pytest.raises(LeakError):
        platform.sample_leak_zone("unload")
