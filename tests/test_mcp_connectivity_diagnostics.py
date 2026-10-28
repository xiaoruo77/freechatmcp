from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

spec = spec_from_file_location("connectivity_diagnostics", Path(__file__).resolve().parents[1] / "10_web_deploy/diagnose-mcp-connectivity.py")
diag = module_from_spec(spec)
spec.loader.exec_module(diag)


def checks(**overrides):
    result = {"local_mcp": {"ok": True}, "public_health": {"http_status": 200},
              "public_mcp": {"http_status": 401}, "tunnel_ready": {"http_status": 200},
              "tunnel_metrics": {"active_connections": 3}}
    result.update(overrides)
    return result


@pytest.mark.parametrize("overrides,expected", [
    ({}, "local_and_public_transport_healthy"),
    ({"local_mcp": {"ok": False}}, "local_mcp_unavailable"),
    ({"public_health": {"http_status": 530, "cloudflare_error": 1033}}, "tunnel_disconnected"),
    ({"public_health": {"http_status": 502}}, "public_entry_unavailable"),
    ({"public_health": {"error_type": "ReadTimeout"}}, "public_entry_unavailable"),
    ({"public_mcp": {"http_status": 500}}, "public_mcp_route_unexpected"),
    ({"public_mcp": {"http_status": 200}}, "public_mcp_route_unexpected"),
    ({"tunnel_metrics": {"active_connections": 0}}, "local_tunnel_not_ready"),
    ({"tunnel_ready": {"http_status": 503}}, "local_tunnel_not_ready"),
])
def test_layer_classification(overrides, expected):
    assert diag.classify(checks(**overrides)) == expected


@pytest.mark.parametrize("value,expected", [("0", 0), ("4", 4), ("NaN", None), ("inf", None), ("-1", None), ("bad", None)])
def test_invalid_metrics_are_not_healthy(value, expected):
    assert diag.connection_count("cloudflared_tunnel_ha_connections " + value) == expected


def test_only_known_cloudflare_error_is_retained():
    body = "Error 1033 credential=SECRET source=PRIVATE"
    assert diag.http_summary(530, {"server": "cloudflare"}, body) == {
        "http_status": 530, "via_cloudflare": True, "cloudflare_error": 1033}
    assert "cloudflare_error" not in diag.http_summary(500, {}, body)


async def test_transport_exception_does_not_disclose_request_or_secret():
    async def handle(request):
        raise httpx.ReadTimeout("token=SECRET", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await diag.http_probe(client, "https://example.test/health")
    assert result == {"error_type": "ReadTimeout"}


async def test_real_http_shape_is_bounded_and_sanitized():
    async def handle(request):
        return httpx.Response(530, headers={"server": "cloudflare", "set-cookie": "SECRET"}, text="Error 1033 PRIVATE")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await diag.http_probe(client, "https://example.test/health")
    assert result == {"http_status": 530, "via_cloudflare": True, "cloudflare_error": 1033}