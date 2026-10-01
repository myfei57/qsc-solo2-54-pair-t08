"""配置解析与校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hydrorefuel.config import (
    CompressorLimits,
    FlowLimits,
    LeakLimits,
    StationConfig,
    config_to_mapping,
    default_config,
    load_config,
)
from hydrorefuel.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_repository_config_matches_defaults():
    loaded = load_config(str(ROOT / "config" / "hydrorefuel.json"))
    assert loaded == default_config()


def test_config_round_trips_through_mapping():
    cfg = default_config()
    assert StationConfig.from_mapping(config_to_mapping(cfg)) == cfg


def test_missing_limits_section_is_rejected():
    with pytest.raises(ConfigError):
        StationConfig.from_mapping({"station": {"name": "x", "storage_root": "y"}})


def test_invalid_limits_are_rejected():
    with pytest.raises(ConfigError):
        CompressorLimits.from_mapping(
            {
                "inlet_min_mpa": 10.0,
                "outlet_target_mpa": 5.0,
                "ramp_mpa_per_s": 1.0,
                "surge_band_mpa": 1.0,
                "min_flow_kg_per_min": 1.0,
            }
        )
    with pytest.raises(ConfigError):
        FlowLimits.from_mapping(
            {
                "step_target_kg": 1.0,
                "step_tolerance_kg": 2.0,
                "max_rate_kg_per_min": 3.0,
                "min_rate_kg_per_min": 0.5,
            }
        )


def test_missing_file_is_rejected():
    with pytest.raises(ConfigError):
        load_config(str(ROOT / "config" / "missing.json"))


def _leak_section(**overrides):
    section = {
        "threshold_ppm": 4000.0,
        "sample_window_s": 5.0,
        "hold_s": 30.0,
        "zoning_basis": "physical_zone",
        "zones": [
            {"id": "unload", "label": "卸氢区", "auto_reset": False},
            {"id": "dispense", "label": "加注区", "auto_reset": True},
        ],
    }
    section.update(overrides)
    return section


def test_leak_zoning_basis_is_fixed_to_physical_zone():
    limits = LeakLimits.from_mapping(_leak_section())
    assert limits.zoning_basis == "physical_zone"
    assert [zone.zone_id for zone in limits.zones] == ["unload", "dispense"]
    assert limits.zone("dispense").auto_reset is True


def test_pipe_branch_zoning_is_rejected():
    # 按管路分支的口径被定死排除，含糊或写错的取值都在装配阶段拒绝。
    with pytest.raises(ConfigError):
        LeakLimits.from_mapping(_leak_section(zoning_basis="pipe_branch"))
    with pytest.raises(ConfigError):
        LeakLimits.from_mapping(_leak_section(zoning_basis="mixed"))


def test_leak_zones_require_explicit_nonempty_list():
    with pytest.raises(ConfigError):
        LeakLimits.from_mapping(_leak_section(zones=[]))
    without_basis = _leak_section()
    del without_basis["zoning_basis"]
    with pytest.raises(ConfigError):
        LeakLimits.from_mapping(without_basis)


def test_duplicate_leak_zone_ids_are_rejected():
    with pytest.raises(ConfigError):
        LeakLimits.from_mapping(
            _leak_section(
                zones=[
                    {"id": "z1", "label": "一区", "auto_reset": False},
                    {"id": "z1", "label": "重名区", "auto_reset": True},
                ]
            )
        )
