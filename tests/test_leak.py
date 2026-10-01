"""分物理区泄漏检测、隔离与恢复。

分区口径定死为物理分区（不按管路分支）。用例覆盖：单路超标只隔离本路、
其他路继续可用；单点与多点同时超标的来源归因；人工复位区没消干净不复位；
自动恢复区在保持期走完且消干净后才由控制周期自动恢复；探头掉线作为部分
失败按有泄漏处理；隔离/恢复/复位动作全部落审计。
"""

from __future__ import annotations

import pytest

from hydrorefuel.errors import LeakError


# ---------------------------------------------------------------------------
# 单路隔离与其他路继续
# ---------------------------------------------------------------------------


def test_single_zone_trip_isolates_only_that_zone(platform):
    platform.start()
    result = platform.sample_leak_zone("unload", 4500.0)
    assert result["tripped"]
    assert result["new_isolation"]
    snapshot = platform.leak_snapshot()
    assert snapshot["zoning_basis"] == "physical_zone"
    assert snapshot["tripped_zones"] == ["unload"]
    assert snapshot["isolated_zones"] == ["unload"]
    assert snapshot["multi_point"] is False
    assert platform.leak.valve_snapshot("unload")["closed"] is True
    # 其余三路隔离阀保持打开，继续可用。
    for zone_id in ("compression", "storage", "dispense"):
        assert platform.leak.valve_snapshot(zone_id)["closed"] is False


def test_isolated_zone_blocks_only_its_operations(platform):
    platform.start()
    platform.sample_leak_zone("unload", 4500.0)
    with pytest.raises(LeakError):
        platform.unload(target_kg=5.0)
    # 加注在加注岛区，与卸氢区物理隔断，仍可继续。
    assert platform.fill(vehicle="v1", steps=1)["ok"] is True
    assert platform.stop_fill()["ok"] is True


def test_compression_zone_trip_blocks_pressurize(platform):
    platform.start()
    platform.sample_leak_zone("compression", 4100.0)
    with pytest.raises(LeakError):
        platform.pressurize()
    # 卸氢区不受压缩区隔离影响。
    assert platform.unload(target_kg=5.0)["ok"] is True


def test_below_threshold_does_not_trip(platform):
    result = platform.sample_leak_zone("storage", 100.0)
    assert not result["tripped"]
    assert not result["isolated"]
    zone = next(z for z in platform.leak_snapshot()["zones"] if z["zone"] == "storage")
    assert zone["samples"] == 1
    assert zone["ready"] is True


def test_unknown_zone_is_rejected(platform):
    with pytest.raises(LeakError):
        platform.sample_leak_zone("nonexistent", 100.0)


# ---------------------------------------------------------------------------
# 多点同时超标与来源归因
# ---------------------------------------------------------------------------


def test_batch_distinguishes_single_from_multi_point(platform):
    platform.start()
    single = platform.sample_leak_batch({"compression": 9000.0, "storage": 0.0})
    assert single["tripped_zones"] == ["compression"]
    assert single["multi_point"] is False

    multi = platform.sample_leak_batch(
        {"unload": 5000.0, "dispense": 6000.0, "compression": 0.0, "storage": 0.0}
    )
    assert multi["multi_point"] is True
    assert multi["simultaneous"] is True
    assert multi["tripped_zones"] == ["unload", "dispense"]
    assert multi["count"] == 2
    summary = platform.leak_snapshot()
    assert set(summary["tripped_zones"]) == {"unload", "compression", "dispense"}
    assert summary["sources_by_zone"]["unload"] == ["unload"]
    # 三路隔离，只剩储氢瓶组区可用。
    assert platform.leak.valve_snapshot("storage")["closed"] is False


# ---------------------------------------------------------------------------
# 人工复位：保持期 + 消干净
# ---------------------------------------------------------------------------


def test_manual_zone_requires_hold_and_clear_before_reset(platform, clock):
    platform.start()
    platform.sample_leak_zone("unload", 4500.0)
    # 保持期未满：拒绝。
    with pytest.raises(LeakError):
        platform.reset_leak_zone("unload")
    clock.advance(platform.config.leak.hold_s + 1.0)
    # 保持期已过但浓度仍超标：泄漏未消干净，拒绝。
    with pytest.raises(LeakError):
        platform.reset_leak_zone("unload")
    platform.sample_leak_zone("unload", 50.0)
    result = platform.reset_leak_zone("unload")
    assert result["reset"] is True
    assert platform.leak.valve_snapshot("unload")["closed"] is False
    zone = next(z for z in platform.leak_snapshot()["zones"] if z["zone"] == "unload")
    assert zone["manual_resets"] == 1
    assert zone["tripped"] is False


def test_manual_zone_does_not_auto_recover_even_after_clear(platform, clock):
    platform.start()
    platform.sample_leak_zone("unload", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak_zone("unload", 0.0)
    # 控制周期不得自动恢复人工复位区。
    assert platform.cycle()["leak_auto_recovered"] == []
    assert "unload" in platform.leak.isolated_zones()
    assert platform.reset_leak_zone("unload")["reset"] is True


def test_repeated_trip_counts_separate_episodes(platform, clock):
    platform.start()
    platform.sample_leak_zone("unload", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak_zone("unload", 0.0)
    platform.reset_leak_zone("unload")
    platform.sample_leak_zone("unload", 4600.0)
    zone = next(z for z in platform.leak_snapshot()["zones"] if z["zone"] == "unload")
    assert zone["trips"] == 2


# ---------------------------------------------------------------------------
# 自动恢复：按区配置
# ---------------------------------------------------------------------------


def test_auto_zone_recovers_only_after_hold_and_clear(platform, clock):
    platform.start()
    platform.sample_leak_zone("dispense", 5000.0)
    assert "dispense" in platform.leak.isolated_zones()
    # 保持期内即使浓度回落也不恢复。
    platform.sample_leak_zone("dispense", 0.0)
    assert platform.cycle()["leak_auto_recovered"] == []
    clock.advance(platform.config.leak.hold_s + 1.0)
    # 保持期过了但最近一次采样仍超标：不恢复。
    platform.sample_leak_zone("dispense", 5000.0)
    assert platform.cycle()["leak_auto_recovered"] == []
    # 重新采到干净读数并再走完一个保持期，控制周期才自动恢复。
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak_zone("dispense", 0.0)
    assert platform.cycle()["leak_auto_recovered"] == ["dispense"]
    assert platform.leak.valve_snapshot("dispense")["closed"] is False
    zone = next(z for z in platform.leak_snapshot()["zones"] if z["zone"] == "dispense")
    assert zone["auto_recoveries"] == 1


def test_auto_zone_keeps_isolated_while_leak_persists(platform, clock):
    platform.start()
    platform.sample_leak_zone("dispense", 5000.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    for _ in range(3):
        cycle = platform.cycle()
        assert cycle["leak_auto_recovered"] == []
        assert "dispense" in cycle["isolated_leak_zones"]


# ---------------------------------------------------------------------------
# 探头掉线：部分失败按有泄漏处理
# ---------------------------------------------------------------------------


def test_probe_offline_is_treated_as_leak(platform):
    platform.start()
    result = platform.mark_probe_offline("storage")
    assert result["new_isolation"]
    assert "storage" in platform.leak.isolated_zones()
    assert platform.leak.valve_snapshot("storage")["closed"] is True
    with pytest.raises(LeakError):
        platform.leak.guard("storage")
    zone = next(z for z in platform.leak_snapshot()["zones"] if z["zone"] == "storage")
    assert zone["probe_online"] is False
    assert zone["leaking"] is True


def test_offline_zone_cannot_reset_until_online_and_clear(platform, clock):
    platform.start()
    platform.mark_probe_offline("unload")
    clock.advance(platform.config.leak.hold_s + 1.0)
    with pytest.raises(LeakError):
        platform.reset_leak_zone("unload")
    platform.mark_probe_online("unload")
    # 重新上线但还没有一次干净采样，仍按未消干净拒绝。
    with pytest.raises(LeakError):
        platform.reset_leak_zone("unload")
    platform.sample_leak_zone("unload", 0.0)
    assert platform.reset_leak_zone("unload")["reset"] is True


def test_auto_zone_offline_then_online_clean_recovers_on_cycle(platform, clock):
    platform.start()
    platform.mark_probe_offline("dispense")
    clock.advance(platform.config.leak.hold_s + 1.0)
    assert platform.cycle()["leak_auto_recovered"] == []
    platform.mark_probe_online("dispense")
    platform.sample_leak_zone("dispense", 0.0)
    assert platform.cycle()["leak_auto_recovered"] == ["dispense"]


# ---------------------------------------------------------------------------
# 审计一致性
# ---------------------------------------------------------------------------


def test_isolation_reset_and_recovery_are_audited(platform, clock):
    platform.start()
    platform.sample_leak_zone("unload", 4500.0)
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak_zone("unload", 0.0)
    platform.reset_leak_zone("unload")
    platform.mark_probe_offline("storage")
    platform.mark_probe_online("storage")

    actions = [entry["action"] for entry in platform.audit.entries()]
    for action in ("zone_isolated", "zone_manual_reset", "probe_offline", "probe_online"):
        assert action in actions
    alarms = [entry for entry in platform.audit.alarms() if entry["component"] == "leak"]
    isolated = [entry for entry in alarms if entry["action"] == "zone_isolated"]
    assert {entry["payload"]["zone"] for entry in isolated} >= {"unload", "storage"}
    # 隔离告警必须带来源与阀值，控制台告警页据此归因。
    record = next(entry for entry in isolated if entry["payload"]["zone"] == "unload")
    assert record["payload"]["reason"] == "leak"
    assert record["payload"]["threshold_ppm"] == platform.config.leak.threshold_ppm


def test_multi_point_emits_sources_event(platform):
    platform.start()
    platform.sample_leak_batch({"unload": 5000.0, "storage": 5000.0})
    entries = platform.audit.entries()
    source_event = next(entry for entry in entries if entry["action"] == "leak_sources")
    assert set(source_event["payload"]["tripped_zones"]) == {"unload", "storage"}
    assert source_event["payload"]["multi_point"] is True


# ---------------------------------------------------------------------------
# 旧接口兼容：默认物理区
# ---------------------------------------------------------------------------


def test_legacy_sample_and_release_default_to_unload_zone(platform, clock):
    platform.start()
    result = platform.sample_leak(4500.0)
    assert result["zone"] == "unload"
    assert result["tripped"]
    clock.advance(platform.config.leak.hold_s + 1.0)
    platform.sample_leak(0.0)
    assert platform.release_leak()["reset"] is True
