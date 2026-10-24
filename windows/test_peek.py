"""
test_peek.py - tests for the sensors, messages and alert rules.

    python -m unittest test_peek -v

Runs on any OS: nothing here touches Win32 except TestAgentSnapshot, which
stands a fake in for ctypes' Windows DLLs so the real snapshot() can be
exercised end to end.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import peek_alerts as A
import peek_config as C
import peek_messages as M
import peek_sensors as S


# ---------------------------------------------------------------- sensors
LHM = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
    {"Text": "NVIDIA GeForce RTX 4070", "ImageURL": "images_icon/nvidia.png", "Children": [
        {"Text": "Load", "Children": [
            {"Text": "GPU Core", "Value": "42,5 %", "SensorId": "/gpu-nvidia/0/load/0"},
            {"Text": "GPU Memory Controller", "Value": "10 %", "SensorId": "/gpu-nvidia/0/load/1"},
            {"Text": "GPU Memory", "Value": "33.0 %", "SensorId": "/gpu-nvidia/0/load/4"}]},
        {"Text": "Temperatures", "Children": [
            {"Text": "GPU Core", "Value": "61.0 \u00b0C", "SensorId": "/gpu-nvidia/0/temperature/0"},
            {"Text": "GPU Hot Spot", "Value": "70.0 \u00b0C", "SensorId": "/gpu-nvidia/0/temperature/1"}]},
        {"Text": "Powers", "Children": [
            {"Text": "GPU Package", "Value": "148.2 W", "SensorId": "/gpu-nvidia/0/power/0"}]},
        {"Text": "Fans", "Children": [
            {"Text": "GPU Fan 1", "Value": "1450 RPM", "SensorId": "/gpu-nvidia/0/fan/0"}]}]},
    {"Text": "AMD Ryzen 7", "Children": [{"Text": "Powers", "Children": [
        {"Text": "CPU Package", "Value": "65.1 W", "SensorId": "/amdcpu/0/power/0"}]}]},
    {"Text": "Nuvoton", "Children": [{"Text": "Fans", "Children": [
        {"Text": "Fan #1", "Value": "900 RPM", "SensorId": "/lpc/nct6798d/0/fan/0"},
        {"Text": "Fan #2", "Value": "1200 RPM", "SensorId": "/lpc/nct6798d/0/fan/1"},
        {"Text": "Fan #3", "Value": "0 RPM", "SensorId": "/lpc/nct6798d/0/fan/2"},
        {"Text": "Fan #4", "Value": "-", "SensorId": "/lpc/nct6798d/0/fan/3"}]}]},
]}]}


class TestSensors(unittest.TestCase):
    def test_units_and_labels(self):
        self.assertEqual(S.unit_for_key("fan_rpm"), "rpm")
        self.assertEqual(S.unit_for_key("link_mbps"), "Mbps")      # not "_mb"
        self.assertEqual(S.unit_for_key("temp_c"), "\u00b0C")
        self.assertEqual(S.unit_for_key("plain"), "")
        self.assertEqual(S.label_for_key("gpu_percent"), "GPU")
        self.assertEqual(S.label_for_key("custom_sensor_w"), "Custom Sensor")

    def test_nvidia_single_and_multi(self):
        one = S.parse_nvidia("42, 61, 148.20, 8704, 12282, 35\n")
        self.assertEqual(one["gpu_percent"], 42)
        self.assertEqual(one["gpu_temp_c"], 61)
        self.assertAlmostEqual(one["vram_used_gb"], 8.5)
        self.assertAlmostEqual(one["gpu_mem_percent"], 100 * 8704 / 12282)
        two = S.parse_nvidia("10, 50, 100, 1024, 8192, 30\n80, 70, 200, 1024, 8192, 60\n")
        self.assertEqual(two["gpu_percent"], 80)             # the busiest
        self.assertEqual(two["gpu_temp_c"], 70)              # the hottest
        self.assertEqual(two["gpu_power_w"], 300)            # power adds

    def test_nvidia_not_available_values_are_skipped(self):
        r = S.parse_nvidia("5, 40, [N/A], 100, 8000, [Not Supported]\n")
        self.assertEqual(r["gpu_percent"], 5)
        self.assertNotIn("gpu_power_w", r)
        self.assertNotIn("gpu_fan_percent", r)
        self.assertEqual(S.parse_nvidia("garbage\n"), {})
        self.assertEqual(S.parse_nvidia(None), {})

    def test_lhm(self):
        r = S.parse_lhm(LHM)
        self.assertEqual(r["gpu_percent"], 42.5)             # comma decimal, GPU Core
        self.assertEqual(r["gpu_mem_percent"], 33.0)         # not "Memory Controller"
        self.assertEqual(r["gpu_temp_c"], 61.0)              # core, not hot spot
        self.assertEqual(r["gpu_power_w"], 148.2)
        self.assertEqual(r["cpu_power_w"], 65.1)
        self.assertEqual(r["gpu_fan_rpm"], 1450)
        self.assertEqual(r["fan_rpm"], 1200)                 # fastest case fan, 0 ignored

    def test_lhm_without_sensor_ids(self):
        """Older builds have no SensorId: fall back to names and units."""
        tree = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
            {"Text": "NVIDIA GeForce GTX 1060", "Children": [
                {"Text": "Load", "Children": [{"Text": "GPU Core", "Value": "17 %"}]},
                {"Text": "Temperatures", "Children": [{"Text": "GPU Core", "Value": "55 \u00b0C"}]}]}]}]}
        r = S.parse_lhm(tree)
        self.assertEqual(r["gpu_percent"], 17)
        self.assertEqual(r["gpu_temp_c"], 55)

    def test_lhm_nothing(self):
        self.assertEqual(S.parse_lhm({"Text": "Sensor", "Children": []}), {})
        self.assertEqual(S.parse_lhm(None), {})

    def test_engine_gpu_and_npu(self):
        p = S.EngineParser()
        rows = [
            "pid_1_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D|30",
            "pid_2_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D|25",   # sums to 55
            "pid_2_luid_0x00000000_0x0000AAAA_phys_0_eng_1_engtype_Copy|5",
            "pid_9_luid_0x00000000_0x0000BBBB_phys_0_eng_0_engtype_Compute|12",
            "junk line",
        ]
        r = p.parse(rows)
        self.assertEqual(r["gpu_percent"], 55)
        self.assertEqual(r["npu_percent"], 12)
        # The NPU goes idle and its instances disappear: remembered, reads 0.
        r = p.parse(["pid_1_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D|2"])
        self.assertEqual(r["gpu_percent"], 2)
        self.assertEqual(r["npu_percent"], 0)

    def test_engine_clamps(self):
        r = S.EngineParser().parse(
            ["pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D|80",
             "pid_2_luid_0x0_0x1_phys_0_eng_0_engtype_3D|70"])
        self.assertEqual(r["gpu_percent"], 100)

    def test_extract_number(self):
        self.assertEqual(S.extract_number("temp: 45,5 C"), 45.5)
        self.assertEqual(S.extract_number("time=12ms ttl=60", r"time[=<](\d+)"), 12)
        self.assertIsNone(S.extract_number("no digits"))
        self.assertIsNone(S.extract_number("x", r"("))               # bad regex is not a crash
        self.assertEqual(S.json_lookup('{"a": {"b": [1, 7.5]}}', "a.b.1"), 7.5)
        self.assertIsNone(S.json_lookup('{"a": true}', "a"))
        self.assertIsNone(S.json_lookup("not json", "a"))

    def test_clean_custom(self):
        good, warns = S.clean_custom([
            {"key": "Ping_MS", "source": "command", "command": "ping -n 1 1.1.1.1",
             "regex": r"time[=<](\d+)", "unit": "ms"},
            {"key": "watts_w", "source": "file", "path": "C:/w.txt", "interval": 0.1},
            {"key": "cpu_percent", "source": "file", "path": "x"},        # reserved
            {"key": "bad key", "source": "file", "path": "x"},
            {"key": "nosrc", "source": "ftp"},
            {"key": "watts_w", "source": "file", "path": "dup"},          # duplicate
            {"key": "empty", "source": "command", "command": []},
            "not a dict",
        ])
        self.assertEqual([g["key"] for g in good], ["ping_ms", "watts_w"])
        self.assertEqual(good[0]["command"][0], "ping")
        self.assertEqual(good[1]["interval"], 2.0)                        # floor
        self.assertEqual(len(warns), 6)

    def test_read_custom(self):
        ok = lambda out: (lambda argv, t: out)
        v, e = S.read_custom({"source": "command", "command": ["x"], "regex": r"=(\d+)"},
                             run=ok("a=42"))
        self.assertEqual((v, e), (42.0, None))
        v, e = S.read_custom({"source": "command", "command": ["x"], "scale": 0.001},
                             run=ok("1500"))
        self.assertAlmostEqual(v, 1.5)
        v, e = S.read_custom({"source": "command", "command": ["x"]}, run=ok(None))
        self.assertIsNone(v)
        self.assertIn("did not run", e)
        v, e = S.read_custom({"source": "http", "url": "u", "json_path": "d.w"},
                             fetch=lambda u: '{"d": {"w": 9}}')
        self.assertEqual(v, 9.0)
        v, e = S.read_custom({"source": "file", "path": "/nonexistent/zzz"})
        self.assertIsNone(v)
        typeperf = ('"(PDH-CSV 4.0)","\\\\H\\Processor(_Total)\\% Processor Time"\n'
                    '"01/01/2026 10:00:00.000","0.000000"\n'
                    '"01/01/2026 10:00:01.000","17.250000"\n\nThe command completed.\n')
        v, e = S.read_custom({"source": "counter", "counter": "x"}, run=ok(typeperf))
        self.assertEqual(v, 17.25)                                        # last sample

    def test_file_source(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.txt"
            f.write_text("load 321.5 W\n")
            v, e = S.read_custom({"source": "file", "path": str(f)})
            self.assertEqual(v, 321.5)

    def _extras(self, **kw):
        clock = [1000.0]
        x = S.ExtraSensors(clock=lambda: clock[0], **kw)
        return x, clock

    def test_payload_shapes_and_priority(self):
        nv = lambda argv, t: "42, 61, 148.2, 8704, 12282, 35\n"
        x, clock = self._extras(
            run=nv, which=lambda n: "nvidia-smi", ps=lambda c, t: "", lhm=lambda: LHM,
            custom=[{"key": "solar_w", "source": "file", "path": "p", "label": "Solar",
                     "unit": "W", "interval": 5, "scale": 1.0, "max": 5000.0}])
        x._fetch = lambda u: None
        x.collect_once()
        plain, metrics = x.payload()
        # nvidia-smi outranks LibreHardwareMonitor for the keys both have.
        self.assertEqual(plain["gpu_percent"], 42)
        self.assertEqual(plain["gpu_temp_c"], 61)
        # Not one of the three the phone names itself: sent with a label.
        self.assertEqual(metrics["gpu_power_w"]["label"], "GPU power")
        self.assertEqual(metrics["gpu_power_w"]["unit"], "W")
        self.assertEqual(metrics["vram_used_gb"]["unit"], "GB")
        self.assertEqual(metrics["gpu_mem_percent"]["max"], 100)
        # A key only LibreHardwareMonitor has still arrives.
        self.assertEqual(metrics["cpu_power_w"]["value"], 65.1)
        self.assertNotIn("solar_w", metrics)          # its file does not exist

    def test_custom_in_payload(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.txt"
            f.write_text("1234")
            x, _ = self._extras(
                enabled=False, run=lambda a, t: None, ps=lambda c, t: "", lhm=lambda: None,
                custom=[{"key": "solar_w", "source": "file", "path": str(f), "label": "Solar",
                         "unit": "W", "interval": 5, "scale": 1.0, "max": 5000.0}])
            x.collect_once()
            _plain, metrics = x.payload()
            self.assertEqual(metrics["solar_w"], {"value": 1234.0, "label": "Solar",
                                                  "unit": "W", "max": 5000.0})

    def test_stale_values_expire(self):
        x, clock = self._extras(run=lambda a, t: "42, 61, 148.2, 8704, 12282, 35\n",
                                which=lambda n: "nvidia-smi",
                                ps=lambda c, t: "", lhm=lambda: None)
        x.collect_once()
        self.assertIn("gpu_percent", x.payload()[0])
        clock[0] += 500                      # the source stopped answering
        self.assertEqual(x.payload(), ({}, {}))

    def test_backs_off_when_a_source_has_nothing(self):
        calls = []
        x, clock = self._extras(run=lambda a, t: calls.append(1) or "", ps=lambda c, t: "",
                                lhm=lambda: None)
        x.collect_once()
        n = len(calls)
        clock[0] += 5
        x.collect_once()                     # inside the interval: not asked again
        self.assertEqual(len(calls), n)
        for _ in range(40):                  # many misses: asked ever less often
            clock[0] += 400
            x.collect_once()
        self.assertLessEqual(x._due["nvidia"] - clock[0], 300)

    def test_cap_of_24(self):
        x, _ = self._extras(enabled=False, run=lambda a, t: "1", ps=lambda c, t: "",
                            lhm=lambda: None,
                            custom=[{"key": f"s{i:02d}_w", "source": "command", "command": ["x"],
                                     "interval": 5, "scale": 1.0, "max": None}
                                    for i in range(16)])
        x.collect_once()
        x._vals.update({f"extra{i}_w": {"lhm": (1.0, 9e9)} for i in range(20)})
        p, m = x.payload()
        self.assertEqual(len(p) + len(m), 24)

    def test_robust_scale_ignores_one_spike(self):
        med, scale = S.robust_scale([10, 11, 9, 10, 12, 10, 11, 9, 1000])
        self.assertAlmostEqual(med, 10)
        self.assertLess(scale, 3)


# --------------------------------------------------------------- messages
class TestMessages(unittest.TestCase):
    def test_severity_words(self):
        for w in ("critical", "RED", "high", "alert", "emergency", "fatal", "error"):
            self.assertEqual(M.norm_severity(w), "critical", w)
        for w in ("warning", "yellow", "amber", "orange", "warn", "medium"):
            self.assertEqual(M.norm_severity(w), "warning", w)
        for w in ("info", "", None, "blue", "reed"):
            self.assertEqual(M.norm_severity(w), "info", w)
        self.assertFalse(M.is_known_severity("reed"))
        self.assertTrue(M.is_known_severity("red"))

    def test_build_limits_and_defaults(self):
        m = M.build({"title": "x" * 200, "message": "y" * 900, "severity": "red",
                     "id": "bad id with spaces", "ts": 1760000000000}, 1760000100)
        self.assertEqual(len(m["title"]), 80)
        self.assertEqual(len(m["body"]), 400)
        self.assertEqual(m["severity"], "critical")
        self.assertEqual(m["ts"], 1760000000)                       # milliseconds accepted
        self.assertRegex(m["id"], r"^[0-9a-f]{20}$")                # unsafe id hashed, not dropped
        self.assertEqual(M.build({"body": "only a body"}, 1)["title"], "only a body")
        self.assertIsNone(M.build({"severity": "red"}, 1))
        self.assertIsNone(M.build("nope", 1))
        self.assertEqual(M.build({"title": "t", "ts": 9e18}, 100)["ts"], 100)   # future ts refused
        self.assertEqual(M.build({"title": "a\x00b\nc"}, 1)["title"], "ab c")

    def test_parse_payload_shapes(self):
        self.assertEqual(len(M.parse_payload({"title": "a"})), 1)
        self.assertEqual(len(M.parse_payload([{"title": "a"}, {"title": "b"}, 5])), 2)
        self.assertEqual(len(M.parse_payload({"messages": [{"title": "a"}]})), 1)
        self.assertEqual(M.parse_payload("x"), [])
        self.assertEqual(len(M.parse_payload([{"title": "a"}] * 40)), 10)

    def test_dedupe_and_ttl_and_order(self):
        t = [1000.0]
        ob = M.Outbox(ttl_hours=2, host="studio", clock=lambda: t[0])
        s, m = ob.add({"id": "fw-1", "title": "Intrusion", "severity": "red"})
        self.assertEqual((s, m["id"]), ("new", "studio:fw-1"))
        self.assertEqual(ob.add({"id": "fw-1", "title": "Intrusion"})[0], "duplicate")
        t[0] += 10
        ob.add({"id": "i-1", "title": "Info"})
        t[0] += 10
        ob.add({"id": "w-1", "title": "Warn", "severity": "warn"})
        order = [x["severity"] for x in ob.active()]
        self.assertEqual(order, ["critical", "warning", "info"])    # worst first
        t[0] += 3 * 3600                                            # past the window
        self.assertEqual(ob.active(), [])

    def test_no_id_means_distinct_messages(self):
        ob = M.Outbox()
        a = ob.add({"title": "same"})
        b = ob.add({"title": "same"})
        self.assertEqual((a[0], b[0]), ("new", "new"))              # a repeat is not swallowed

    def test_at_most_ten_in_a_reading(self):
        ob = M.Outbox()
        for i in range(30):
            ob.add({"id": f"m{i}", "title": "t"}, internal=True)
        self.assertEqual(len(ob.active()), 10)

    def test_rate_limit_applies_to_outsiders_only(self):
        ob = M.Outbox()
        res = [ob.add({"title": f"t{i}"})[0] for i in range(25)]
        self.assertEqual(res.count("new"), 20)
        self.assertEqual(res.count("rate"), 5)
        self.assertEqual(ob.add({"title": "mine"}, internal=True)[0], "new")

    def test_listener_sees_new_not_resends(self):
        seen = []
        ob = M.Outbox()
        ob.on_new(seen.append)
        ob.add({"id": "a", "title": "x"})
        ob.add({"id": "a", "title": "x"})
        self.assertEqual(len(seen), 1)

    def test_a_broken_listener_does_not_lose_the_message(self):
        ob = M.Outbox()
        ob.on_new(lambda m: 1 / 0)
        self.assertEqual(ob.add({"title": "x"})[0], "new")
        self.assertEqual(len(ob.active()), 1)
        self.assertIn("ZeroDivisionError", ob.listener_error)

    def test_survives_restart_without_ringing_again(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "outbox.json"
            M.Outbox(p, host="h").add({"id": "a", "title": "x", "severity": "red"})
            seen = []
            again = M.Outbox(p, host="h")
            again.on_new(seen.append)
            self.assertEqual(len(again.active()), 1)
            self.assertEqual(again.add({"id": "a", "title": "x"})[0], "duplicate")
            self.assertEqual(seen, [])

    def test_corrupt_outbox_file_is_survivable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "outbox.json"
            p.write_text("{not json")
            self.assertEqual(M.Outbox(p).active(), [])


class TestChannels(unittest.TestCase):
    def test_folder_watcher(self):
        with tempfile.TemporaryDirectory() as d:
            ob = M.Outbox()
            w = M.OutboxWatcher(ob, d)
            M.drop_file(d, {"title": "from a file", "severity": "yellow"})
            M.drop_file(d, [{"title": "a"}, {"title": "b"}])
            (Path(d) / "huge.json").write_text(json.dumps({"title": "x" * 50000}))
            (Path(d) / "empty.json").write_text("[]")
            (Path(d) / "ignored.txt").write_text("{}")
            w.scan()
            self.assertEqual(len(ob.active()), 3)
            left = sorted(p.name for p in Path(d).iterdir() if p.is_file())
            self.assertEqual(left, ["ignored.txt"])                # processed ones removed
            self.assertEqual(len(list((Path(d) / "rejected").iterdir())), 2)

    def test_watcher_waits_for_a_file_still_being_written(self):
        with tempfile.TemporaryDirectory() as d:
            ob = M.Outbox()
            p = Path(d) / "partial.json"
            p.write_text('{"title": "half')
            M.OutboxWatcher(ob, d).scan()
            self.assertTrue(p.exists())                            # fresh: given time
            old = time.time() - 60
            os.utime(p, (old, old))
            M.OutboxWatcher(ob, d).scan()
            self.assertFalse(p.exists())                           # stale: rejected

    def test_watcher_file_ids_are_stable_across_a_retry(self):
        with tempfile.TemporaryDirectory() as d:
            ob = M.Outbox()
            p = M.drop_file(d, [{"title": "a"}, {"title": "b"}])
            w = M.OutboxWatcher(ob, d)
            w.scan()
            self.assertEqual(len(ob.active()), 2)
            (Path(d) / p.name).write_text(p.read_text() if p.exists() else json.dumps(
                [{"title": "a"}, {"title": "b"}]))
            w.scan()                                               # same file name again
            self.assertEqual(len(ob.active()), 2)                  # not 4

    def _server(self):
        ob = M.Outbox(host="studio")
        with tempfile.TemporaryDirectory():
            pass
        port = 46000 + (os.getpid() % 1000)
        srv = M.NotifyServer(ob, "k" * 48, port)
        self.assertTrue(srv.start(), srv.error)
        self.addCleanup(srv.stop)
        self.srv = srv
        return ob, port

    def _post(self, port, body, headers=None, path="/notify", host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        if host:
            h["Host"] = host
        c.request("POST", path, body if isinstance(body, (bytes, str)) else json.dumps(body), h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_endpoint_accepts_the_right_key(self):
        ob, port = self._server()
        key = {"Authorization": "Bearer " + "k" * 48}
        st, data = self._post(port, {"title": "Intrusion", "severity": "red", "id": "a1"}, key)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["ids"], ["studio:a1"])
        self.assertEqual(ob.active()[0]["severity"], "critical")
        self.assertEqual(ob.active()[0]["source"], "External app")
        st, _ = self._post(port, [{"title": "x"}, {"title": "y"}], {"X-Peek-Key": "k" * 48})
        self.assertEqual(st, 200)                                  # the other header works too

    def test_endpoint_refusals(self):
        ob, port = self._server()
        good = {"Authorization": "Bearer " + "k" * 48}
        body = {"title": "x"}
        self.assertEqual(self._post(port, body)[0], 401)                              # no key
        self.assertEqual(self._post(port, body, {"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self._post(port, body, {"Authorization": "Bearer " + "k" * 47})[0], 401)
        self.assertEqual(self._post(port, body, {**good, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self._post(port, body, good, host="evil.example")[0], 403)   # rebinding
        self.assertEqual(self._post(port, body, good, path="/other")[0], 404)
        self.assertEqual(self._post(port, "not json", good)[0], 400)
        self.assertEqual(self._post(port, {"severity": "red"}, good)[0], 400)         # says nothing
        self.assertEqual(self._post(port, b"x" * 20000, good)[0], 413)
        self.assertEqual(ob.active(), [])                                             # nothing got in

    def test_endpoint_rate_limit(self):
        _ob, port = self._server()
        good = {"Authorization": "Bearer " + "k" * 48}
        codes = [self._post(port, {"title": f"t{i}"}, good)[0] for i in range(23)]
        self.assertEqual(codes.count(200), 20)
        self.assertEqual(codes.count(429), 3)

    def test_endpoint_is_loopback_only(self):
        _ob, port = self._server()
        # Bound to loopback, not to every interface.
        self.assertEqual(self.srv._httpd.server_address[0], "127.0.0.1")

    def test_port_in_use_reports_and_does_not_raise(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        srv = M.NotifyServer(M.Outbox(), "k", s.getsockname()[1])
        self.assertFalse(srv.start())
        self.assertIn("port", srv.error)
        s.close()


# ----------------------------------------------------------------- alerts
class TestQuietHours(unittest.TestCase):
    Q = {"enabled": True, "start": "22:00", "end": "07:00",
         "days": [0, 1, 2, 3, 4, 5, 6], "allow_critical": True}

    def d(self, wd, h, m=0):
        # 2026-10-19 is a Monday
        return datetime(2026, 10, 19 + wd, h, m)

    def test_crossing_midnight(self):
        self.assertTrue(A.in_quiet(self.Q, self.d(0, 23)))
        self.assertTrue(A.in_quiet(self.Q, self.d(1, 3)))
        self.assertTrue(A.in_quiet(self.Q, self.d(1, 6, 59)))
        self.assertFalse(A.in_quiet(self.Q, self.d(1, 7)))
        self.assertFalse(A.in_quiet(self.Q, self.d(1, 12)))
        self.assertTrue(A.in_quiet(self.Q, self.d(1, 22)))

    def test_days_are_the_days_the_window_begins(self):
        q = dict(self.Q, days=[4])                    # begins Friday only
        self.assertTrue(A.in_quiet(q, self.d(4, 23)))        # Friday night
        self.assertTrue(A.in_quiet(q, self.d(5, 3)))         # Saturday 03:00 is still it
        self.assertFalse(A.in_quiet(q, self.d(4, 3)))        # Friday 03:00 belongs to Thursday's
        self.assertFalse(A.in_quiet(q, self.d(5, 23)))       # Saturday night: not a begin day

    def test_same_day_window_and_disabled(self):
        q = dict(self.Q, start="13:00", end="14:30")
        self.assertTrue(A.in_quiet(q, self.d(2, 14)))
        self.assertFalse(A.in_quiet(q, self.d(2, 14, 30)))
        self.assertFalse(A.in_quiet(dict(self.Q, enabled=False), self.d(0, 23)))
        self.assertFalse(A.in_quiet(dict(self.Q, start="08:00", end="08:00"), self.d(0, 8)))

    def test_policy(self):
        al = A.default_alerts()
        al["quiet"] = dict(self.Q)
        night = self.d(0, 23)
        self.assertEqual(A.local_policy("warning", al, night), (False, False))
        self.assertEqual(A.local_policy("critical", al, night), (True, True))   # allowed through
        al["quiet"]["allow_critical"] = False
        self.assertEqual(A.local_policy("critical", al, night), (False, False))
        self.assertEqual(A.local_policy("critical", al, self.d(0, 12)), (True, True))
        self.assertEqual(A.local_policy("warning", al, self.d(0, 12)), (True, False))
        al["local_ring"] = False
        self.assertEqual(A.local_policy("critical", al, self.d(0, 12)), (True, False))


class FakeEnv:
    """A fake reading source, a fake clock and a real outbox."""

    def __init__(self, rules, **vals):
        self.vals = dict({"cpu_percent": 10.0, "ram_percent": 40.0}, **vals)
        self.t = 100000.0
        self.out = M.Outbox(host="studio", clock=lambda: self.t)
        al = A.default_alerts()
        al["rules"] = [A.clean_rule(r, i, set())[0] for i, r in enumerate(rules)]
        self.eng = A.AlertEngine(lambda: A.flatten(self.vals), self.out, al, "studio",
                                 clock=lambda: self.t)

    def run(self, seconds, **vals):
        self.vals.update(vals)
        for _ in range(int(seconds // 5)):
            self.t += 5
            self.eng.tick()


CPU = {"id": "cpu", "metric": "cpu_percent", "op": "above", "limit": 90, "for": 60,
       "severity": "critical"}


class TestRules(unittest.TestCase):
    def test_fires_only_after_it_holds(self):
        e = FakeEnv([CPU])
        e.run(30, cpu_percent=95.0)
        self.assertEqual(e.out.active(), [])                  # not yet a minute
        e.run(40)
        a = e.out.active()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["severity"], "critical")
        self.assertIn("CPU high on studio", a[0]["title"])
        self.assertIn("95%", a[0]["body"])

    def test_a_dip_resets_the_clock(self):
        e = FakeEnv([CPU])
        e.run(50, cpu_percent=95.0)
        e.run(10, cpu_percent=50.0)
        e.run(50, cpu_percent=95.0)
        self.assertEqual(e.out.active(), [])

    def test_one_message_per_episode_then_recovery_then_new_episode(self):
        e = FakeEnv([CPU])
        e.run(300, cpu_percent=95.0)
        self.assertEqual(len(e.out.active()), 1)              # not one per tick
        e.run(60, cpu_percent=20.0)
        titles = [m["title"] for m in e.out.active()]
        self.assertIn("CPU back to normal on studio", titles)
        info = [m for m in e.out.active() if "back to normal" in m["title"]][0]
        self.assertEqual(info["severity"], "info")
        e.run(120, cpu_percent=96.0)
        self.assertEqual(len([m for m in e.out.active() if m["severity"] == "critical"]), 2)

    def test_brief_flap_while_active_does_not_recover_or_refire(self):
        e = FakeEnv([CPU])
        e.run(100, cpu_percent=95.0)
        e.run(10, cpu_percent=50.0)                           # under CLEAR_HOLD
        e.run(100, cpu_percent=95.0)
        self.assertEqual(len(e.out.active()), 1)

    def test_below_and_unplugged(self):
        rule = {"id": "bat", "metric": "battery_percent", "op": "below", "limit": 20,
                "for": 0, "severity": "warning", "unplugged": True}
        e = FakeEnv([rule], battery_percent=15.0, battery_ac=True, battery_charging=False)
        e.run(30)
        self.assertEqual(e.out.active(), [])                  # on AC: no alert
        e.run(10, battery_ac=False)
        self.assertEqual(len(e.out.active()), 1)

    def test_no_sensor_never_fires(self):
        rule = {"id": "t", "metric": "cpu_temp_c", "op": "above", "limit": 1, "for": 0}
        e = FakeEnv([rule], cpu_temp_c=-1.0)
        e.run(60)
        self.assertEqual(e.out.active(), [])
        e = FakeEnv([rule])                                   # key absent entirely
        e.run(60)
        self.assertEqual(e.out.active(), [])

    def test_extra_sensor_from_metrics_object_with_its_own_label(self):
        rule = {"id": "v", "metric": "vram_gb", "op": "above", "limit": 8, "for": 0}
        e = FakeEnv([rule])
        e.vals["metrics"] = {"vram_gb": {"value": 9.5, "label": "Video memory", "unit": "GB"}}
        e.run(10)
        m = e.out.active()[0]
        self.assertIn("Video memory high", m["title"])
        self.assertIn("9.5 GB", m["body"])

    def test_derived_network_total(self):
        flat, _ = A.flatten({"net_rx_kbps": 300.0, "net_tx_kbps": 50.0})
        self.assertEqual(flat["net_kbps"], 350.0)
        flat, _ = A.flatten({"net_rx_kbps": -1, "net_tx_kbps": 50.0})
        self.assertNotIn("net_kbps", flat)

    def test_wording_placeholders(self):
        rule = dict(CPU, title="{machine} is on fire", body="{label}={value} (limit {limit}, {duration}) {nope}")
        e = FakeEnv([rule])
        e.run(80, cpu_percent=95.0)
        m = e.out.active()[0]
        self.assertEqual(m["title"], "studio is on fire")
        self.assertEqual(m["body"], "CPU=95% (limit 90%, 1 min) {nope}")   # unknown left alone

    def test_template_cannot_reach_attributes(self):
        self.assertEqual(A.fill("{value.__class__}", {"value": "x"}), "{value.__class__}")
        self.assertEqual(A.fill("{machine}", {"machine": "a"}), "a")

    def test_unusual_needs_history_and_ignores_normal(self):
        rule = {"id": "net", "metric": "net_kbps", "op": "unusual", "min": 1000, "for": 0,
                "severity": "warning"}
        e = FakeEnv([rule], net_rx_kbps=5000.0, net_tx_kbps=0.0)
        e.run(30)
        self.assertEqual(e.out.active(), [])                  # no history yet: says nothing
        # Eight hours of a steady 200 kbps, with realistic wobble.
        e.eng.hist["net_kbps"] = [(int(e.t // 60) - 480 + i, 200.0 + (i % 7) * 10)
                                  for i in range(480)]
        e.run(10, net_rx_kbps=230.0)
        self.assertEqual(e.out.active(), [])                  # within the usual range
        e.run(10, net_rx_kbps=9000.0)
        m = e.out.active()
        self.assertEqual(len(m), 1)
        self.assertIn("Unusual Network", m[0]["title"])
        e.run(60, net_rx_kbps=100.0)                          # quiet again

    def test_unusual_floor_ignores_small_spikes(self):
        rule = {"id": "net", "metric": "net_kbps", "op": "unusual", "min": 5000, "for": 0}
        e = FakeEnv([rule], net_rx_kbps=3000.0, net_tx_kbps=0.0)
        e.eng.hist["net_kbps"] = [(int(e.t // 60) - 480 + i, 10.0) for i in range(480)]
        e.run(30)
        self.assertEqual(e.out.active(), [])                  # unusual, but under the floor

    def test_history_is_kept_only_for_unusual_rules(self):
        e = FakeEnv([CPU])
        e.run(200, cpu_percent=50.0)
        self.assertEqual(e.eng.hist, {})

    def test_history_buckets_by_minute_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            rule = {"id": "n", "metric": "cpu_percent", "op": "unusual", "for": 0}
            e = FakeEnv([rule])
            e.eng.history_path = Path(d) / "h.json"
            e.run(200, cpu_percent=40.0)
            self.assertGreaterEqual(len(e.eng.hist["cpu_percent"]), 2)
            e.eng.stop()
            again = A.AlertEngine(lambda: ({}, {}), e.out, e.eng.alerts, "studio",
                                  history_path=e.eng.history_path, clock=lambda: e.t)
            self.assertEqual(again.hist["cpu_percent"], e.eng.hist["cpu_percent"])

    def test_reconfigure_mid_incident_does_not_refire(self):
        e = FakeEnv([CPU])
        e.run(100, cpu_percent=95.0)
        self.assertEqual(len(e.out.active()), 1)
        al = A.default_alerts()
        al["rules"] = [A.clean_rule(dict(CPU, limit=85), 0, set())[0]]
        e.eng.configure(al)                                   # user saved the settings window
        e.run(100)
        self.assertEqual(len(e.out.active()), 1)              # the phone is not rung twice

    def test_master_switch_and_disabled_rule(self):
        e = FakeEnv([dict(CPU, enabled=False)])
        e.run(200, cpu_percent=99.0)
        self.assertEqual(e.out.active(), [])
        al = A.default_alerts()
        al["enabled"] = False
        e.eng.configure(al)
        e.run(200, cpu_percent=99.0)
        self.assertEqual(e.out.active(), [])

    def test_engine_survives_a_failing_reading_source(self):
        out = M.Outbox()
        al = A.default_alerts()
        eng = A.AlertEngine(lambda: 1 / 0, out, al, "h", interval=0.01)
        eng.start()
        time.sleep(0.15)
        eng.stop()
        self.assertIn("ZeroDivisionError", eng.last_error)


class TestConfig(unittest.TestCase):
    def test_defaults_are_independent_copies(self):
        a, b = C.validate(C.copy.deepcopy(C.DEFAULTS)), C.validate(C.copy.deepcopy(C.DEFAULTS))
        a["alerts"]["rules"][0]["limit"] = 1
        self.assertNotEqual(b["alerts"]["rules"][0]["limit"], 1)
        self.assertEqual(C.DEFAULTS["alerts"]["rules"][0]["limit"], 90.0)

    def test_old_config_file_still_loads_with_defaults_for_the_new_keys(self):
        old = {"mode": "push", "relay_url": "https://r/ingest/x", "token": "t",
               "interval": 5.0, "serve_port": 6595, "autostart": False}
        cfg = C.validate(dict(C.copy.deepcopy(C.DEFAULTS), **old))
        self.assertTrue(cfg["alerts"]["enabled"])
        self.assertEqual(len(cfg["alerts"]["rules"]), 7)
        self.assertEqual(cfg["notify_port"], 6598)
        self.assertEqual(cfg["_warnings"], [])

    def test_hand_edited_mistakes_are_reported_not_fatal(self):
        cfg = C.validate(dict(C.copy.deepcopy(C.DEFAULTS), **{
            "alerts": {"quiet": {"enabled": True, "start": "25:99"},
                       "rules": [{"metric": "cpu_percent", "op": "above"},        # no limit
                                 {"metric": "x y", "op": "above", "limit": 1},
                                 {"metric": "ram_percent", "op": "sideways", "limit": 1},
                                 {"metric": "ram_percent", "op": "above", "limit": 80,
                                  "severity": "RED", "for": "300"}]},
            "sensors": [{"key": "ok_w", "source": "file", "path": "p"}, {"key": ""}],
            "notify_port": "banana", "message_ttl_hours": 9999, "extras_interval": "x"}))
        self.assertEqual(len(cfg["alerts"]["rules"]), 1)
        r = cfg["alerts"]["rules"][0]
        self.assertEqual((r["severity"], r["for"]), ("critical", 300))
        self.assertEqual(cfg["alerts"]["quiet"]["start"], "22:00")
        self.assertEqual(len(cfg["sensors"]), 1)
        self.assertEqual(cfg["notify_port"], 6598)
        self.assertEqual(cfg["message_ttl_hours"], 72.0)
        self.assertGreaterEqual(len(cfg["_warnings"]), 5)

    def test_an_empty_rules_list_means_the_user_deleted_them(self):
        cfg = C.validate(dict(C.copy.deepcopy(C.DEFAULTS), alerts={"rules": []}))
        self.assertEqual(cfg["alerts"]["rules"], [])

    def test_notify_port_cannot_collide_with_the_lan_port(self):
        cfg = C.validate(dict(C.copy.deepcopy(C.DEFAULTS), notify_port=6595, serve_port=6595))
        self.assertEqual(cfg["notify_port"], 6598)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["APPDATA"] = d
            cfg = C.load()
            cfg["alerts"]["rules"][0]["limit"] = 77
            cfg["alerts"]["quiet"]["enabled"] = True
            cfg["sensors"] = [{"key": "a_w", "source": "file", "path": "p"}]
            C.save(cfg)
            raw = json.loads(C.config_path().read_text())
            self.assertNotIn("_warnings", raw)
            back = C.load()
            self.assertEqual(back["alerts"]["rules"][0]["limit"], 77)
            self.assertTrue(back["alerts"]["quiet"]["enabled"])
            self.assertEqual(back["sensors"][0]["key"], "a_w")


def _import_agent():
    """The real Windows agent, imported anywhere, with ctypes' Windows DLLs faked."""
    import ctypes
    from unittest import mock
    if "peek_agent_win" in sys.modules:
        return sys.modules["peek_agent_win"]
    if os.name == "nt":
        import peek_agent_win
        return peek_agent_win
    ctypes.WinDLL = lambda *a, **k: mock.MagicMock()
    with mock.patch.object(os, "name", "nt"):
        import peek_agent_win
    return peek_agent_win


# ---------------------------------------------------- the real snapshot()
class TestAgentSnapshot(unittest.TestCase):
    """Imports the real Windows agent with ctypes' Windows DLLs faked, so what
    goes out on the wire is what the real snapshot() builds."""

    @classmethod
    def setUpClass(cls):
        agent = cls.agent = _import_agent()
        for name, val in {"_storage": (61.0, 10 ** 12, 4 * 10 ** 11),
                          "_battery": (-1, False, True, -1),
                          "_ram_percent": 58.0, "_cpu_temp_c": 54.0,
                          "_uptime_seconds": 86400}.items():
            setattr(agent, name, (lambda v: lambda *a: v)(val))

    @classmethod
    def tearDownClass(cls):
        cls.agent._services = None

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        cfg = C.validate(C.copy.deepcopy(C.DEFAULTS))
        cfg["sensors_enabled"] = False
        cfg["sensors"] = [{"key": "solar_w", "source": "file", "path": str(Path(self.dir) / "w"),
                           "label": "Solar", "unit": "W", "interval": 5, "scale": 1.0, "max": None}]
        (Path(self.dir) / "w").write_text("812")
        self.svc = self.agent_services(cfg)

    def agent_services(self, cfg):
        import peek_services
        svc = peek_services.Services(cfg, lambda: self.agent.snapshot(messages=False),
                                     "studio", Path(self.dir))
        self.agent._services = svc
        svc.extras.collect_once(force=True)
        return svc

    def test_reading_without_services_is_the_standard_one(self):
        self.agent._services = None
        snap = self.agent.snapshot()
        self.assertNotIn("messages", snap)
        self.assertNotIn("metrics", snap)
        self.assertEqual(snap["cpu_temp_c"], 54.0)

    def test_extras_and_messages_ride_along(self):
        self.svc.outbox.add({"id": "x", "title": "Intrusion", "severity": "red",
                             "source": "Defender"})
        snap = self.agent.snapshot()
        self.assertEqual(snap["metrics"]["solar_w"]["value"], 812.0)
        self.assertEqual(snap["messages"][0]["severity"], "critical")
        self.assertEqual(snap["messages"][0]["source"], "Defender")
        self.assertEqual(snap["ram_percent"], 58.0)             # standard keys untouched

    def test_the_lan_endpoint_never_carries_messages(self):
        self.svc.outbox.add({"id": "x", "title": "New login from 203.0.113.9", "severity": "red"})
        lan = self.agent.snapshot(messages=False)
        self.assertNotIn("messages", lan)
        self.assertIn("metrics", lan)                           # numbers are fine

    def test_what_actually_goes_over_the_wire(self):
        got = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got["auth"] = self.headers.get("Authorization")
                got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.svc.outbox.add({"id": "m1", "title": "Disk noisy", "severity": "warning"})

        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/ingest/x",
            data=json.dumps(self.agent.snapshot()).encode(), method="POST",
            headers={"Authorization": "Bearer t"})
        urllib.request.urlopen(req, timeout=3).read()
        body = got["body"]
        self.assertEqual(body["messages"][0]["id"], "studio:m1")
        self.assertEqual(body["metrics"]["solar_w"]["unit"], "W")
        # The size matters: the relay and the device both have to take it.
        self.assertLess(len(json.dumps(body)), 2000)

    def test_message_bursts_stay_inside_what_a_device_can_hold(self):
        for i in range(30):
            self.svc.outbox.add({"id": f"m{i}", "title": "t" * 80, "body": "b" * 400,
                                 "severity": "red", "source": "s" * 40}, internal=True)
        size = len(json.dumps(self.agent.snapshot()))
        self.assertLessEqual(len(self.agent.snapshot()["messages"]), 10)
        self.assertLess(size, 8000)

    def test_services_start_stop_start_leaves_one_of_each(self):
        svc = self.svc
        svc.cfg["notify_port"] = 46999
        svc.cfg["notify_enabled"] = True
        svc.cfg["notify_key"] = "k" * 48
        before = threading.active_count()
        for _ in range(4):
            svc.start()
            time.sleep(0.05)
        svc.stop()
        time.sleep(1.5)
        self.assertLessEqual(threading.active_count(), before + 1)    # no pile-up of threads
        self.assertTrue(svc.engine._thread is not None)

    def test_notify_cli_drops_a_file_that_the_watcher_picks_up(self):
        os.environ["APPDATA"] = self.dir
        rc = self.agent.notify_cli(["--title", "Hi", "--severity", "red", "--id", "cli-1",
                                    "--outbox", str(Path(self.dir) / "ob")])
        self.assertEqual(rc, 0)
        M.OutboxWatcher(self.svc.outbox, Path(self.dir) / "ob").scan()
        self.assertEqual(self.svc.outbox.active()[0]["id"], "studio:cli-1")
        with self.assertRaises(SystemExit):
            self.agent.notify_cli([])                           # nothing to send is an error


# ------------------------------------------------------------ the tray app
class TestTrayRunner(unittest.TestCase):
    """The new behaviour in peek_tray.Runner, with a fake tray icon."""

    @classmethod
    def setUpClass(cls):
        _import_agent()
        import peek_tray
        cls.tray = peek_tray

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        os.environ["APPDATA"] = d
        self.r = self.tray.Runner()
        self.r.services.dir = Path(d)
        self.toasts, self.rings = [], []

        class Icon:
            def notify(_s, message, title=None):
                self.toasts.append((title, message))
        self.r.icon = Icon()
        self.r.alarm.ring = lambda: self.rings.append(1)
        self.addCleanup(self.r.services.stop)

    def test_every_message_toasts_and_critical_rings(self):
        ob = self.r.services.outbox
        ob.add({"id": "a", "title": "Disk noisy", "severity": "warning", "source": "SMART"})
        self.assertEqual(len(self.toasts), 1)
        self.assertEqual(self.rings, [])
        ob.add({"id": "b", "title": "Intrusion", "body": "New login", "severity": "red",
                "source": "Defender"})
        self.assertEqual(self.toasts[-1][0], "CRITICAL: Intrusion")
        self.assertIn("Defender", self.toasts[-1][1])
        self.assertEqual(self.rings, [1])
        ob.add({"id": "b", "title": "Intrusion", "severity": "red"})      # a re-send
        self.assertEqual((len(self.toasts), len(self.rings)), (2, 1))

    def test_a_rule_and_a_security_tool_take_the_same_path(self):
        from peek_alerts import AlertEngine
        self.r.services.outbox.add({"title": "x", "severity": "red", "source": "Some AV"})
        self.assertEqual(self.rings, [1])

    def test_old_messages_are_filed_quietly(self):
        self.r.services.outbox.add({"id": "old", "title": "Yesterday", "severity": "red",
                                    "ts": time.time() - 7 * 3600})
        self.assertEqual((self.toasts, self.rings), ([], []))
        self.assertEqual(len(self.r.services.outbox.active()), 1)         # still goes to the phone

    def test_quiet_hours_silence_this_pc_but_not_the_phone(self):
        now = datetime.now()
        self.r.cfg["alerts"]["quiet"] = {
            "enabled": True, "start": "00:00", "end": "23:59",
            "days": list(range(7)), "allow_critical": False}
        ob = self.r.services.outbox
        ob.add({"id": "w", "title": "w", "severity": "warning"})
        ob.add({"id": "c", "title": "c", "severity": "critical"})
        self.assertEqual((self.toasts, self.rings), ([], []))
        self.assertEqual(len(ob.active()), 2)                  # both still reach the phone
        self.r.cfg["alerts"]["quiet"]["allow_critical"] = True
        ob.add({"id": "c2", "title": "c2", "severity": "critical"})
        self.assertEqual(self.rings, [1])

    def test_link_loss_is_local_only_and_needs_three_failures_and_thirty_seconds(self):
        clock = [1000.0]

        class T:
            time = staticmethod(lambda: clock[0])
            sleep = staticmethod(lambda s: None)
            monotonic = staticmethod(lambda: clock[0])
        results = iter([(0, "down")] * 3 + [(0, "down")] + [(200, "OK")] * 50 + [(200, "OK")])

        def fake_push(url, token, timeout=10):
            clock[0] += 12
            return next(results, (200, "OK"))
        self.r.cfg.update({"mode": "push", "relay_url": "https://r/ingest/x", "token": "t",
                           "interval": 1.0})
        self.tray.push_once, real = fake_push, self.tray.push_once
        self.tray.time, real_time = T, self.tray.time
        self.addCleanup(lambda: (setattr(self.tray, "push_once", real),
                                 setattr(self.tray, "time", real_time)))
        real_time.sleep(0)
        th = threading.Thread(target=self.r._push_forever, daemon=True)
        th.start()
        for _ in range(100):
            real_time.sleep(0.05)
            if any("back online" in t[0] for t in self.toasts):
                break
        self.r._stop.set()
        titles = [t[0] for t in self.toasts]
        self.assertIn("PeekESP cannot reach the relay", titles)
        self.assertIn("PeekESP is back online", titles)
        self.assertEqual(self.rings, [])                       # never rings for this
        self.assertEqual(self.r.services.outbox.active(), [])  # and never goes to the phone

    def test_a_rejected_token_is_not_reported_as_the_link_being_down(self):
        results = iter([(401, "token rejected")] * 20)
        self.r.cfg.update({"mode": "push", "relay_url": "https://r/ingest/x", "token": "t",
                           "interval": 1.0})
        self.tray.push_once, real = (lambda *a, **k: next(results, (401, "x"))), self.tray.push_once
        self.addCleanup(lambda: setattr(self.tray, "push_once", real))
        th = threading.Thread(target=self.r._push_forever, daemon=True)
        th.start()
        time.sleep(2.5)
        self.r._stop.set()
        self.assertEqual(self.toasts, [])

    def test_alarm_stops_when_silenced(self):
        a = self.tray.Alarm()
        self.assertFalse(a.ringing)
        a.silence()
        self.assertFalse(a.ringing)


if __name__ == "__main__":
    unittest.main()