"""控制台 HTTP 接口。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from hydrorefuel.console.server import ConsoleServer, probe_endpoints


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_console_serves_status_and_routes(platform):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        status, payload = _get(f"{base}/api/status")
        assert status == 200
        assert payload["platform"]
        ready = probe_endpoints(base, ["/api/readiness", "/api/topology"])
        assert all(item["ok"] for item in ready.values())
        assert any(route["path"] == "/api/fill" for route in server.api.routes())
    finally:
        server.stop()


def test_console_actions_and_rejections(platform):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        assert _post(f"{base}/api/step", {"dt": 1.0})[0] == 200
        assert _post(f"{base}/api/unload", {"target_kg": 10.0})[1]["ok"] is True
        assert _post(f"{base}/api/pressurize", {"charge_kg": 12.0})[1]["ok"] is True
        assert _post(f"{base}/api/fill", {"steps": 2})[1]["ok"] is True
        assert _post(f"{base}/api/stop", {})[1]["ok"] is True
        assert _post(f"{base}/api/leak/sample", {"concentration_ppm": 4500.0})[1]["tripped"] is True
        status, payload = _post(f"{base}/api/leak/reset", {})
        assert status == 409
        assert payload["error"] == "LeakError"
        resolved = _get(f"{base}/api/namespace/station.vent")
        assert resolved[0] == 200
        assert resolved[1]["device"]["kind"] == "vent-valve"
    finally:
        server.stop()


def test_console_leak_zone_endpoints(platform, clock):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        status, payload = _post(f"{base}/api/leak/zones/buffer/sample", {"concentration_ppm": 4500.0})
        assert status == 200
        assert payload["state"] == "isolated"
        assert payload["cause"] == "threshold"
        zones = _get(f"{base}/api/leak/zones")[1]["leak_zones"]
        assert zones["isolated_zones"] == ["buffer"]
        assert zones["degraded"] is False
        # 泄漏未消干净，人工复位被拒并落审计
        status, payload = _post(f"{base}/api/leak/zones/buffer/reset", {})
        assert status == 409
        assert payload["error"] == "LeakError"
        # 探头掉线属于部分失败，按有泄漏处理
        status, payload = _post(f"{base}/api/leak/zones/unload/sample", {"probe_ok": False})
        assert status == 200
        assert payload["cause"] == "offline"
        assert _get(f"{base}/api/leak/zones")[1]["leak_zones"]["degraded"] is True
        incidents = _get(f"{base}/api/leak/incidents")[1]
        assert incidents["active"][0]["kind"] == "multi"
        assert set(incidents["active"][0]["zones"]) == {"buffer", "unload"}
        # 消隐后人工复位成功，控制台快照与审计一致
        clock.advance(platform.config.leak.hold_s + 1.0)
        _post(f"{base}/api/leak/zones/buffer/sample", {"concentration_ppm": 80.0})
        status, payload = _post(f"{base}/api/leak/zones/buffer/reset", {"actor": "console"})
        assert status == 200
        assert payload["released"] is True
        zones = _get(f"{base}/api/leak/zones")[1]["leak_zones"]
        assert zones["isolated_zones"] == ["unload"]
        assert any(
            entry["action"] == "zone_recovered" and entry["payload"]["zone"] == "buffer"
            for entry in _get(f"{base}/api/audit")[1]["entries"]
        )
    finally:
        server.stop()


def test_console_unknown_path(platform):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        try:
            _get(f"{base}/api/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.stop()
