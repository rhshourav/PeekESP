"""Runs without Home Assistant: pairing.py and api.py import nothing from it."""
import asyncio, importlib.util, json, pathlib, sys, time
import aiohttp
from aiohttp import web

PKG = pathlib.Path(__file__).parent.parent / "custom_components" / "peekesp"

def load(name):
    spec = importlib.util.spec_from_file_location(name, PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod); return mod

pairing, api = load("pairing"), load("api")

def test_pairing_vector():
    # The vector pinned in PeekESP.ino, checked there against openssl.
    stream, token = pairing.derive("K7M2P4QX9R")
    assert stream == "4b907ba136d0a7f2"
    assert token == "ec3cb3699bd1284efb2fcfe056609e87edf4813b84e9ce84"

def test_code_normalisation():
    assert pairing.normalize_code("k7m2-p4qx-9r") == "K7M2P4QX9R"
    assert pairing.is_valid_code("K7M2P4QX9R")
    assert not pairing.is_valid_code("K7M2P4QX9")      # short
    assert not pairing.is_valid_code("K7M2P4QX9O")     # O is not in the alphabet
    assert not pairing.is_valid_code("K7M2P4QX90")     # nor is 0

def test_parse_multi_device_and_sentinels():
    machines, fw = api.parse_payload({
        "latest_fw": "1.3.0",
        "devices": [
            {"host": "desk", "cpu_percent": 12.5, "ram_percent": 43.2, "storage_percent": 61,
             "storage_total_gb": 117.9, "storage_free_gb": 46.0, "cpu_temp_c": 48.3,
             "uptime_seconds": 271830, "net_rx_kbps": 128.4, "net_tx_kbps": 12.9, "age_s": 3},
            # older agent: no capacity, no temperature, battery present
            {"host": "laptop", "cpu_percent": 5, "storage_total_gb": 0, "storage_free_gb": 0,
             "cpu_temp_c": -1, "battery_percent": 80, "battery_charging": False, "age_s": 400},
        ]})
    assert fw == "1.3.0" and list(machines) == ["desk", "laptop"]
    d, l = machines["desk"], machines["laptop"]
    assert d.storage_free_gb == 46.0 and d.cpu_temp_c == 48.3 and d.battery_percent is None
    # "0 GB free" would read as a full disk; the firmware treats it as unknown, so do we.
    assert l.storage_total_gb is None and l.storage_free_gb is None
    assert l.cpu_temp_c is None and l.battery_percent == 80 and l.battery_charging is False
    assert l.age_s == 400

def test_parse_flat_and_garbage():
    m, _ = api.parse_payload({"host": "dietpi", "cpu_percent": 1.0})
    assert list(m) == ["dietpi"]
    m, _ = api.parse_payload({"cpu_percent": "high", "ram_percent": True})
    assert m["dietpi"].cpu_percent is None and m["dietpi"].ram_percent is None
    m, _ = api.parse_payload({"devices": [{"host": f"h{i}"} for i in range(9)]})
    assert len(m) == api.MAX_DEVICES

async def _serve(handler):
    app = web.Application(); app.router.add_get("/telemetry/{s}", handler)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/telemetry/abc"

def test_client_against_fake_relay():
    seen = {}
    async def handler(req):
        seen["auth"], seen["ua"] = req.headers.get("Authorization"), req.headers.get("User-Agent")
        mode = req.app.get("mode")
        if mode == "401": return web.Response(status=401)
        if mode == "503": return web.Response(status=503)
        if mode == "500": return web.Response(status=500)
        return web.json_response({"devices": [{"host": "desk", "cpu_percent": 9}]})
    async def run():
        runner, url = await _serve(handler)
        async with aiohttp.ClientSession() as s:
            c = api.PeekRelayClient(s, url, "TOK")
            d = await c.fetch()
            assert d.machines["desk"].cpu_percent == 9 and d.latency_ms >= 0
            assert seen["auth"] == "Bearer TOK" and seen["ua"].startswith("PeekESP-HA")
            for mode, exc in (("401", api.PeekAuthError), ("500", api.PeekConnectionError)):
                runner.app["mode"] = mode
                try: await c.fetch(); raise AssertionError(mode)
                except exc: pass
            runner.app["mode"] = "503"
            assert (await c.fetch()).machines == {}      # "no data yet" is a state, not an outage
            await runner.cleanup()
            try: await api.PeekRelayClient(s, url, "x").fetch(); raise AssertionError("conn")
            except api.PeekConnectionError: pass
    asyncio.run(run())

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("ok ", n)
