"""Isolated watchdog tests: no network requests, real services or child processes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


WINDOWS_DEPLOY = Path(__file__).resolve().parents[1] / "10_web_deploy" / "windows"


def load_module(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def health(monkeypatch):
    return load_module("tunnel_health", WINDOWS_DEPLOY / "tunnel_health.py", monkeypatch)


@pytest.mark.parametrize(
    "value, ready, reason",
    [
        ("1", True, "connected"),
        ("4", True, "connected"),
        ("0", False, "no_edge_connections"),
        ("-1", False, "invalid_connections_metric"),
        ("NaN", False, "invalid_connections_metric"),
        ("+Inf", False, "invalid_connections_metric"),
        ("-Inf", False, "invalid_connections_metric"),
        ("garbled", False, "invalid_connections_metric"),
    ],
)
def test_metrics_distinguish_alive_and_ready(health, value, ready, reason):
    observation = health.observe_metrics(f"cloudflared_tunnel_ha_connections {value}\n")
    assert observation.alive is True
    assert observation.ready is ready
    assert observation.reason == reason


def test_missing_and_labeled_metrics(health):
    assert health.observe_metrics("other_metric 1\n").ready is False
    assert health.observe_metrics("").reason == "missing_connections_metric"
    assert health.observe_metrics(
        '# HELP cloudflared_tunnel_ha_connections Connection count\n'
        'cloudflared_tunnel_ha_connections{scope="edge"}\t2\n'
    ).ready is True


def test_grace_cooldown_and_recovery(health):
    policy = health.TunnelReadinessPolicy()
    zero = health.observe_metrics("cloudflared_tunnel_ha_connections 0")
    connected = health.observe_metrics("cloudflared_tunnel_ha_connections 1")
    first = policy.evaluate(zero, 0)
    assert first.ready is False
    assert first.restart_allowed is False
    assert first.events == (
        "tunnel_degraded alive=true ready=false reason=no_edge_connections",
    )
    assert policy.evaluate(zero, 179.999).restart_allowed is False
    assert policy.evaluate(zero, 180).restart_allowed is True
    policy.child_started(180, restarted=True)
    assert policy.evaluate(zero, 181).restart_allowed is False
    recovered = policy.evaluate(connected, 200)
    assert recovered.events == ("tunnel_recovered alive=true ready=true",)
    assert policy.last_restart_at == 180
    policy.evaluate(zero, 210)
    assert policy.evaluate(zero, 390).restart_allowed is False
    assert policy.evaluate(zero, 1079.999).restart_allowed is False
    assert policy.evaluate(zero, 1080).restart_allowed is True


def test_short_outage_resets_grace_and_instances_are_independent(health):
    first, second = health.TunnelReadinessPolicy(), health.TunnelReadinessPolicy()
    zero = health.observe_metrics("cloudflared_tunnel_ha_connections 0")
    ready = health.observe_metrics("cloudflared_tunnel_ha_connections 1")
    first.evaluate(zero, 0)
    first.evaluate(ready, 150)
    first.evaluate(zero, 160)
    assert first.evaluate(zero, 339).restart_allowed is False
    assert first.evaluate(zero, 340).restart_allowed is True
    assert second.degraded_since is None
    assert second.last_restart_at is None


@pytest.fixture
def service_modules(monkeypatch, health):
    # Load the real parent loop with its Windows API boundary replaced.
    framework = type("ServiceFramework", (), {
        "__init__": lambda self, args: None,
        "ReportServiceStatus": lambda self, state: None,
    })
    modules = {
        "win32serviceutil": {"ServiceFramework": framework},
        "win32service": {"SERVICE_START_PENDING": 2, "SERVICE_RUNNING": 4},
        "win32event": {"CreateEvent": lambda *args: object(), "WAIT_OBJECT_0": 0},
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    common = load_module("service_common", WINDOWS_DEPLOY / "service_common.py", monkeypatch)
    # The service module prepends its own directory on import; restore it after tests.
    monkeypatch.setattr(sys, "path", list(sys.path))
    service = load_module("_test_cloudflared_service", WINDOWS_DEPLOY / "cloudflared_service.py", monkeypatch)
    return common, service


class MetricsResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body


def make_service(monkeypatch, service_modules, clock, metrics):
    common, module = service_modules
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    service = module.CloudflaredService([])
    service._health_opener = SimpleNamespace(open=lambda *args, **kwargs: MetricsResponse(metrics()))
    logs = []
    service._write_health_log = lambda message: logs.append((clock[0], message))
    return common, service, logs


def test_service_returns_false_during_grace_and_handles_request_errors(monkeypatch, service_modules):
    clock = [0.0]
    _, service, logs = make_service(
        monkeypatch, service_modules, clock,
        lambda: b"cloudflared_tunnel_ha_connections 0\n",
    )
    assert service._health_check() is False
    assert service._health_check_failures_before_restart == sys.maxsize
    assert logs[0][1] == "tunnel_degraded alive=true ready=false reason=no_edge_connections"
    clock[0] = 180
    assert service._health_check() is False
    assert service._health_check_failures_before_restart == 1

    def unavailable(*args, **kwargs):
        raise TimeoutError("private request detail must not appear in logs")

    service._health_opener = SimpleNamespace(open=unavailable)
    assert service._health_check() is False
    assert logs[-1][1] == "tunnel_degraded_changed alive=false ready=false reason=metrics_unreachable"
    assert all("private request" not in message for _, message in logs)


@pytest.mark.parametrize("short_outage", [False, True])
@pytest.mark.parametrize("failure", ["zero", "invalid", "unreachable"])
def test_real_parent_loop_respects_grace_and_cooldown(monkeypatch, service_modules, short_outage, failure):
    clock = [0.0]

    def metrics():
        if short_outage and clock[0] >= 90:
            return b"cloudflared_tunnel_ha_connections 1\n"
        if failure == "unreachable":
            raise TimeoutError("test metrics endpoint unavailable")
        value = b"NaN\n" if failure == "invalid" else b"0\n"
        return b"cloudflared_tunnel_ha_connections " + value

    common, service, logs = make_service(
        monkeypatch, service_modules, clock, metrics,
    )
    starts = []
    stop_at = 300 if short_outage else 1111

    def wait_for_event(event, timeout_ms):
        clock[0] += timeout_ms / 1000
        return 0 if clock[0] >= stop_at else 258

    def start_child(self):
        starts.append(clock[0])
        self.process = SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(common.win32event, "WaitForSingleObject", wait_for_event, raising=False)
    monkeypatch.setattr(common.ManagedProcessService, "_start_child", start_child)
    monkeypatch.setattr(common.ManagedProcessService, "_stop_child", lambda self: setattr(self, "process", None))
    service.SvcDoRun()

    expected_degraded = {
        "zero": "tunnel_degraded alive=true ready=false reason=no_edge_connections",
        "invalid": "tunnel_degraded alive=true ready=false reason=invalid_connections_metric",
        "unreachable": "tunnel_degraded alive=false ready=false reason=metrics_unreachable",
    }
    assert logs[0] == (30, expected_degraded[failure])
    if short_outage:
        assert starts == [0]
        assert (90, "tunnel_recovered alive=true ready=true") in logs
    else:
        assert starts == [0, 210, 1110]
        assert starts[1] - logs[0][0] == 180
        assert starts[2] - starts[1] == 900