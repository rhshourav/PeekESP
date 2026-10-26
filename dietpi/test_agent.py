"""
test_agent.py - tests for the sensors, messages and alert rules in peek-agent.py.

    python3 -m unittest test_agent -v

Everything runs against the real code on a real Linux: sysfs readers are
pointed at a fake /sys tree, and TestEndToEnd starts the actual agent as a
subprocess against a fake relay.
"""

import http.client
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_PATH = os.path.join(HERE, "peek-agent.py")
_spec = importlib.util.spec_from_file_location("peek_agent", AGENT_PATH)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def slurp(path):
    with open(path) as fh:
        return fh.read()


def put(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def fake_sys(root):
    """A /sys with an AMD GPU, an NVIDIA card (ignored), fans, an NVMe and an NPU."""
    d = "class/drm/card0/device/"
    put(root, d + "vendor", "0x1002\n")
    put(root, d + "gpu_busy_percent", "37\n")
    put(root, d + "mem_info_vram_used", str(4 * 1024 ** 3) + "\n")
    put(root, d + "mem_info_vram_total", str(16 * 1024 ** 3) + "\n")
    put(root, d + "hwmon/hwmon3/temp1_input", "61000\n")
    put(root, d + "hwmon/hwmon3/power1_average", "45000000\n")
    put(root, d + "hwmon/hwmon3/fan1_input", "1200\n")
    put(root, "class/drm/card1/device/vendor", "0x10de\n")
    put(root, "class/drm/card1/device/gpu_busy_percent", "99\n")      # must not be read
    put(root, "class/hwmon/hwmon0/name", "nvme\n")
    put(root, "class/hwmon/hwmon0/temp1_input", "42000\n")
    put(root, "class/hwmon/hwmon1/name", "nct6798\n")
    put(root, "class/hwmon/hwmon1/fan1_input", "900\n")
    put(root, "class/hwmon/hwmon1/fan2_input", "0\n")
    put(root, "class/hwmon/hwmon1/fan3_input", "1500\n")
    put(root, "class/hwmon/hwmon2/name", "amdgpu\n")
    put(root, "class/hwmon/hwmon2/fan1_input", "3000\n")               # a GPU fan, not a case fan
    put(root, "class/accel/accel0/device/npu_busy_time_us", "1000000\n")


class TestSensors(unittest.TestCase):
    def test_units_and_labels(self):
        self.assertEqual(A.unit_for_key("fan_rpm"), "rpm")
        self.assertEqual(A.unit_for_key("link_mbps"), "Mbps")          # not "_mb"
        self.assertEqual(A.unit_for_key("temp_c"), "\u00b0C")
        self.assertEqual(A.label_for_key("gpu_percent"), "GPU")
        self.assertEqual(A.label_for_key("custom_sensor_w"), "Custom Sensor")

    def test_nvidia(self):
        one = A.parse_nvidia("42, 61, 148.20, 8704, 12282, 35\n")
        self.assertEqual((one["gpu_percent"], one["gpu_temp_c"]), (42, 61))
        self.assertAlmostEqual(one["vram_used_gb"], 8.5)
        two = A.parse_nvidia("10, 50, 100, 1024, 8192, 30\n80, 70, 200, 1024, 8192, 60\n")
        self.assertEqual((two["gpu_percent"], two["gpu_temp_c"], two["gpu_power_w"]), (80, 70, 300))
        na = A.parse_nvidia("5, 40, [N/A], 100, 8000, [Not Supported]\n")
        self.assertNotIn("gpu_power_w", na)
        self.assertEqual(A.parse_nvidia("garbage"), {})
        self.assertEqual(A.collect_nvidia(which=lambda n: None), {})
        self.assertEqual(A.collect_nvidia(run=lambda a, t: "7, 30, 10, 1, 2, 3\n",
                                          which=lambda n: "/usr/bin/nvidia-smi")["gpu_percent"], 7)

    def test_amd_reads_only_amd(self):
        with tempfile.TemporaryDirectory() as root:
            fake_sys(root)
            r = A.collect_amd(root)
        self.assertEqual(r["gpu_percent"], 37)                         # not the NVIDIA card's 99
        self.assertEqual(r["gpu_temp_c"], 61)
        self.assertAlmostEqual(r["gpu_power_w"], 45.0)
        self.assertAlmostEqual(r["vram_used_gb"], 4.0)
        self.assertAlmostEqual(r["gpu_mem_percent"], 25.0)
        self.assertEqual(r["gpu_fan_rpm"], 1200)

    def test_hwmon_fans_and_disks(self):
        with tempfile.TemporaryDirectory() as root:
            fake_sys(root)
            r = A.collect_hwmon(root)
        self.assertEqual(r["fan_rpm"], 1500)                           # fastest case fan, 0 ignored
        self.assertEqual(r["disk_temp_c"], 42)
        self.assertEqual(len(r), 2)                                    # the amdgpu fan is not a case fan

    def test_nothing_there_is_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(A.collect_amd(root), {})
            self.assertEqual(A.collect_hwmon(root), {})
            n = A.NpuReader(root)
            self.assertEqual(n.read(), {})
            self.assertFalse(n.present)

    def test_npu_is_a_rate(self):
        with tempfile.TemporaryDirectory() as root:
            fake_sys(root)
            clock = [100.0]
            n = A.NpuReader(root, lambda: clock[0])
            self.assertEqual(n.read(), {})                             # baseline only
            self.assertTrue(n.present)                                 # ...but the device is there
            clock[0] += 10.0
            put(root, "class/accel/accel0/device/npu_busy_time_us", "6000000\n")   # +5 s busy in 10 s
            self.assertAlmostEqual(n.read()["npu_percent"], 50.0)
            clock[0] += 10.0
            self.assertAlmostEqual(n.read()["npu_percent"], 0.0)        # idle since
            clock[0] += 1.0
            put(root, "class/accel/accel0/device/npu_busy_time_us", "99000000\n")
            self.assertEqual(n.read()["npu_percent"], 100.0)           # clamped

    def test_npu_counter_reset_is_not_negative(self):
        with tempfile.TemporaryDirectory() as root:
            fake_sys(root)
            clock = [0.0]
            n = A.NpuReader(root, lambda: clock[0])
            n.read()
            clock[0] += 10
            put(root, "class/accel/accel0/device/npu_busy_time_us", "5\n")   # driver reloaded
            self.assertEqual(n.read(), {})

    def test_extract_number(self):
        self.assertEqual(A.extract_number("temp: 45,5 C"), 45.5)
        self.assertEqual(A.extract_number("time=12ms ttl=60", r"time[=<](\d+)"), 12)
        self.assertIsNone(A.extract_number("no digits"))
        self.assertIsNone(A.extract_number("x", r"("))
        self.assertEqual(A.json_lookup('{"a": {"b": [1, 7.5]}}', "a.b.1"), 7.5)
        self.assertIsNone(A.json_lookup('{"a": true}', "a"))

    def test_clean_custom(self):
        good, warns = A.clean_custom([
            {"key": "Ping_MS", "source": "command", "command": "ping -c 1 1.1.1.1",
             "regex": r"time=(\d+)", "unit": "ms"},
            {"key": "watts_w", "source": "file", "path": "/sys/x", "interval": 0.1},
            {"key": "cpu_percent", "source": "file", "path": "x"},
            {"key": "bad key", "source": "file", "path": "x"},
            {"key": "nosrc", "source": "counter"},                     # a Windows-only source
            {"key": "watts_w", "source": "file", "path": "dup"},
            {"key": "empty", "source": "command", "command": []},
            "nope"])
        self.assertEqual([g["key"] for g in good], ["ping_ms", "watts_w"])
        self.assertEqual(good[0]["command"], ["ping", "-c", "1", "1.1.1.1"])
        self.assertEqual(good[1]["interval"], 2.0)
        self.assertEqual(len(warns), 6)

    def test_read_custom_sources(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "t")
            put(d, "t", "48500\n")
            v, e = A.read_custom({"source": "file", "path": f, "scale": 0.001})
            self.assertAlmostEqual(v, 48.5)
        v, e = A.read_custom({"source": "file", "path": "/nonexistent/zz"})
        self.assertIsNone(v)
        v, e = A.read_custom({"source": "command", "command": ["echo", "a=42"], "regex": r"=(\d+)"})
        self.assertEqual(v, 42.0)
        v, e = A.read_custom({"source": "command", "command": ["/no/such/prog"]})
        self.assertIn("did not run", e)
        v, e = A.read_custom({"source": "http", "url": "u", "json_path": "d.w"},
                             fetch=lambda u: '{"d": {"w": 9}}')
        self.assertEqual(v, 9.0)

    def test_commands_never_inherit_the_credential(self):
        with mock.patch.dict(os.environ, {"PEEK_PAIR_CODE": "K7M2-P4QX-9R",
                                          "PEEK_PUSH_TOKEN": "tok", "KEEP_ME": "yes"}):
            out = A.run_process(["sh", "-c", 'echo "[${PEEK_PAIR_CODE:-none}][${PEEK_PUSH_TOKEN:-none}][$KEEP_ME]"'])
        self.assertEqual(out.strip(), "[none][none][yes]")

    def _x(self, **kw):
        clock = [1000.0]
        kw.setdefault("run", lambda a, t: None)
        kw.setdefault("which", lambda n: None)
        return A.ExtraSensors(clock=lambda: clock[0], **kw), clock

    def test_payload_shapes_priority_and_custom(self):
        with tempfile.TemporaryDirectory() as root:
            fake_sys(root)
            put(root, "w.txt", "812")
            x, _ = self._x(
                root=root, run=lambda a, t: "42, 70, 148.2, 8704, 12282, 35\n",
                which=lambda n: "/usr/bin/nvidia-smi",
                custom=[{"key": "solar_w", "source": "file", "path": os.path.join(root, "w.txt"),
                         "label": "Solar", "unit": "W", "interval": 5, "scale": 1.0, "max": 5000.0}])
            x.collect_once()
            plain, metrics = x.payload()
        self.assertEqual(plain["gpu_percent"], 42)                     # nvidia-smi outranks amdgpu's 37
        self.assertEqual(plain["gpu_temp_c"], 70)
        self.assertEqual(metrics["fan_rpm"]["value"], 1500)
        self.assertEqual(metrics["disk_temp_c"], {"value": 42.0, "label": "Disk temperature",
                                                  "unit": "\u00b0C"})
        self.assertEqual(metrics["solar_w"], {"value": 812.0, "label": "Solar", "unit": "W",
                                              "max": 5000.0})
        self.assertEqual(metrics["gpu_mem_percent"]["max"], 100)

    def test_stale_values_expire_and_removed_sensors_vanish(self):
        with tempfile.TemporaryDirectory() as root:
            put(root, "w", "5")
            spec = {"key": "a_w", "source": "file", "path": os.path.join(root, "w"),
                    "interval": 5, "scale": 1.0, "max": None, "label": "", "unit": ""}
            x, clock = self._x(root=root, custom=[spec])
            x.collect_once()
            self.assertIn("a_w", x.payload()[1])
            x.configure(True, 10.0, [])                                # removed from alerts.json
            self.assertEqual(x.payload()[1], {})
            x.configure(True, 10.0, [spec])
            x.collect_once(force=True)
            self.assertIn("a_w", x.payload()[1])
            clock[0] += 500                                            # the file stopped being updated
            self.assertEqual(x.payload(), ({}, {}))

    def test_backs_off_when_a_source_has_nothing(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            x, clock = self._x(root=root, which=lambda n: calls.append(1) or None)
            x.collect_once()
            n = len(calls)
            clock[0] += 5
            x.collect_once()
            self.assertEqual(len(calls), n)                            # inside the interval
            for _ in range(40):
                clock[0] += 400
                x.collect_once()
            self.assertLessEqual(x._due["nvidia"] - clock[0], 300)

    def test_an_npu_baseline_is_not_a_missing_source(self):
        with tempfile.TemporaryDirectory() as root:
            fake_sys(root)
            x, clock = self._x(root=root)
            x.collect_once()
            self.assertEqual(x._miss["npu"], 0)                        # present, just no rate yet
            clock[0] += 10
            put(root, "class/accel/accel0/device/npu_busy_time_us", "6000000\n")
            x.collect_once()
            self.assertAlmostEqual(x.payload()[0]["npu_percent"], 50.0)

    def test_describe_uses_the_names_you_gave(self):
        with tempfile.TemporaryDirectory() as root:
            put(root, "w", "812")
            x, _ = self._x(root=root, custom=[{
                "key": "inverter", "source": "file", "path": os.path.join(root, "w"),
                "label": "Solar", "unit": "W", "interval": 5, "scale": 1.0, "max": None}])
            x.collect_once()
            self.assertEqual(x.describe(), ["Solar                812 W   (custom)"])

    def test_cap_of_24(self):
        x, _ = self._x(enabled=False)
        x._vals.update(dict(("extra%02d_w" % i, {"hwmon": (1.0, 9e9)}) for i in range(40)))
        p, m = x.payload()
        self.assertEqual(len(p) + len(m), 24)

    def test_robust_scale(self):
        med, scale = A.robust_scale([10, 11, 9, 10, 12, 10, 11, 9, 1000])
        self.assertAlmostEqual(med, 10)
        self.assertLess(scale, 3)


class TestMessages(unittest.TestCase):
    def test_severity_words(self):
        for w in ("critical", "RED", "high", "alert", "emergency", "fatal", "error"):
            self.assertEqual(A.norm_severity(w), "critical", w)
        for w in ("warning", "yellow", "amber", "orange", "warn", "medium"):
            self.assertEqual(A.norm_severity(w), "warning", w)
        for w in ("info", "", None, "blue", "reed"):
            self.assertEqual(A.norm_severity(w), "info", w)

    def test_build_limits_and_defaults(self):
        m = A.build_message({"title": "x" * 200, "message": "y" * 900, "severity": "red",
                             "id": "bad id with spaces", "ts": 1760000000000}, 1760000100)
        self.assertEqual((len(m["title"]), len(m["body"])), (80, 400))
        self.assertEqual(m["ts"], 1760000000)
        self.assertRegex(m["id"], r"^[0-9a-f]{20}$")
        self.assertEqual(A.build_message({"body": "only a body"}, 1)["title"], "only a body")
        self.assertIsNone(A.build_message({"severity": "red"}, 1))
        self.assertEqual(A.build_message({"title": "t", "ts": 9e18}, 100)["ts"], 100)
        self.assertEqual(A.build_message({"title": "a\x00b\nc"}, 1)["title"], "ab c")

    def test_parse_payload(self):
        self.assertEqual(len(A.parse_payload({"title": "a"})), 1)
        self.assertEqual(len(A.parse_payload([{"title": "a"}, {"title": "b"}, 5])), 2)
        self.assertEqual(len(A.parse_payload({"messages": [{"title": "a"}]})), 1)
        self.assertEqual(A.parse_payload("x"), [])
        self.assertEqual(len(A.parse_payload([{"title": "a"}] * 40)), 10)

    def test_dedupe_ttl_order_and_host_prefix(self):
        t = [1000.0]
        ob = A.Outbox(ttl_hours=2, host="studio", clock=lambda: t[0])
        s, m = ob.add({"id": "fw-1", "title": "Intrusion", "severity": "red"})
        self.assertEqual((s, m["id"]), ("new", "studio:fw-1"))
        self.assertEqual(ob.add({"id": "fw-1", "title": "Intrusion"})[0], "duplicate")
        t[0] += 10
        ob.add({"id": "i-1", "title": "Info"})
        t[0] += 10
        ob.add({"id": "w-1", "title": "Warn", "severity": "warn"})
        self.assertEqual([x["severity"] for x in ob.active()], ["critical", "warning", "info"])
        t[0] += 3 * 3600
        self.assertEqual(ob.active(), [])

    def test_no_id_means_distinct_messages_and_ten_per_reading(self):
        ob = A.Outbox()
        self.assertEqual((ob.add({"title": "same"})[0], ob.add({"title": "same"})[0]), ("new", "new"))
        for i in range(30):
            ob.add({"id": "m%d" % i, "title": "t"}, internal=True)
        self.assertEqual(len(ob.active()), 10)

    def test_rate_limit_applies_to_outsiders_only(self):
        ob = A.Outbox()
        res = [ob.add({"title": "t%d" % i})[0] for i in range(25)]
        self.assertEqual((res.count("new"), res.count("rate")), (20, 5))
        self.assertEqual(ob.add({"title": "mine"}, internal=True)[0], "new")

    def test_listener_sees_new_only_and_a_broken_one_loses_nothing(self):
        seen = []
        ob = A.Outbox()
        ob.on_new(seen.append)
        ob.on_new(lambda m: 1 / 0)
        ob.add({"id": "a", "title": "x"})
        ob.add({"id": "a", "title": "x"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(ob.active()), 1)
        self.assertIn("ZeroDivisionError", ob.listener_error)

    def test_survives_restart_without_ringing_again(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "outbox.json")
            A.Outbox(p, host="h").add({"id": "a", "title": "x", "severity": "red"})
            seen = []
            again = A.Outbox(p, host="h")
            again.on_new(seen.append)
            self.assertEqual(len(again.active()), 1)
            self.assertEqual(again.add({"id": "a", "title": "x"})[0], "duplicate")
            self.assertEqual(seen, [])
            put(d, "outbox.json", "{not json")
            self.assertEqual(A.Outbox(p).active(), [])

    def test_unwritable_outbox_still_works_in_memory(self):
        ob = A.Outbox("/proc/nope/outbox.json")
        self.assertEqual(ob.add({"title": "x"})[0], "new")
        self.assertEqual(len(ob.active()), 1)
        self.assertTrue(ob.save_error)


class TestState(unittest.TestCase):
    def test_state_dir_order_and_none(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"PEEK_STATE_DIR": os.path.join(d, "a")}):
                self.assertEqual(A.state_dir(), os.path.join(d, "a"))
        with mock.patch.dict(os.environ, {"PEEK_STATE_DIR": "", "HOME": ""}), \
                mock.patch.object(os, "makedirs", side_effect=OSError("read-only")):
            self.assertIsNone(A.state_dir())                           # a unit with no StateDirectory=

    def test_key_is_stable_private_and_long(self):
        with tempfile.TemporaryDirectory() as d:
            k = A.load_or_create_key(d)
            self.assertEqual(len(k), 48)
            self.assertEqual(A.load_or_create_key(d), k)               # not remade on restart
            mode = stat.S_IMODE(os.stat(os.path.join(d, "notify.key")).st_mode)
            self.assertEqual(mode, 0o640)                              # group may read, world may not

    def test_outbox_dir_is_group_writable_and_setgid(self):
        with tempfile.TemporaryDirectory() as d:
            p = A.ensure_outbox_dir(d)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o2770)

    def test_drop_file_mode_and_atomic_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = A.drop_file(d, {"title": "x"})
            self.assertTrue(p.endswith(".json"))
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o660)
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])


class TestChannels(unittest.TestCase):
    def test_folder_watcher(self):
        with tempfile.TemporaryDirectory() as d:
            ob = A.Outbox()
            w = A.OutboxWatcher(ob, d)
            A.drop_file(d, {"title": "from a file", "severity": "yellow"})
            A.drop_file(d, [{"title": "a"}, {"title": "b"}])
            put(d, "huge.json", json.dumps({"title": "x" * 50000}))
            put(d, "empty.json", "[]")
            put(d, "ignored.txt", "{}")
            w.scan()
            self.assertEqual(len(ob.active()), 3)
            self.assertEqual(sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))),
                             ["ignored.txt"])
            self.assertEqual(len(os.listdir(os.path.join(d, "rejected"))), 2)

    def test_watcher_waits_for_a_half_written_file(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, "partial.json", '{"title": "half')
            w = A.OutboxWatcher(A.Outbox(), d)
            w.scan()
            self.assertTrue(os.path.exists(os.path.join(d, "partial.json")))
            old = time.time() - 60
            os.utime(os.path.join(d, "partial.json"), (old, old))
            w.scan()
            self.assertFalse(os.path.exists(os.path.join(d, "partial.json")))

    def test_watcher_ignores_symlinks(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            put(other, "real.json", '{"title": "via link"}')
            os.symlink(os.path.join(other, "real.json"), os.path.join(d, "link.json"))
            ob = A.Outbox()
            A.OutboxWatcher(ob, d).scan()
            self.assertEqual(ob.active(), [])

    def _server(self):
        ob = A.Outbox(host="studio")
        port = free_port()
        srv = A.NotifyServer(ob, "k" * 48, port)
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
        st, data = self._post(port, {"title": "Intrusion", "severity": "red", "id": "a1"},
                              {"Authorization": "Bearer " + "k" * 48})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["ids"], ["studio:a1"])
        self.assertEqual((ob.active()[0]["severity"], ob.active()[0]["source"]),
                         ("critical", "External app"))
        self.assertEqual(self._post(port, [{"title": "x"}], {"X-Peek-Key": "k" * 48})[0], 200)

    def test_endpoint_refusals(self):
        ob, port = self._server()
        good = {"Authorization": "Bearer " + "k" * 48}
        body = {"title": "x"}
        self.assertEqual(self._post(port, body)[0], 401)
        self.assertEqual(self._post(port, body, {"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self._post(port, body, {"Authorization": "Bearer " + "k" * 47})[0], 401)
        self.assertEqual(self._post(port, body, dict(good, Origin="http://evil.example"))[0], 403)
        self.assertEqual(self._post(port, body, good, host="evil.example")[0], 403)
        self.assertEqual(self._post(port, body, good, path="/other")[0], 404)
        self.assertEqual(self._post(port, "not json", good)[0], 400)
        self.assertEqual(self._post(port, {"severity": "red"}, good)[0], 400)
        self.assertEqual(self._post(port, b"x" * 20000, good)[0], 413)
        self.assertEqual(ob.active(), [])

    def test_endpoint_rate_limit_and_loopback_bind(self):
        _ob, port = self._server()
        good = {"Authorization": "Bearer " + "k" * 48}
        codes = [self._post(port, {"title": "t%d" % i}, good)[0] for i in range(23)]
        self.assertEqual((codes.count(200), codes.count(429)), (20, 3))
        self.assertEqual(self.srv._httpd.server_address[0], "127.0.0.1")

    def test_port_in_use_reports_and_does_not_raise(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        srv = A.NotifyServer(A.Outbox(), "k", s.getsockname()[1])
        self.assertFalse(srv.start())
        self.assertIn("port", srv.error)
        s.close()


class TestQuietAndHook(unittest.TestCase):
    Q = {"enabled": True, "start": "22:00", "end": "07:00",
         "days": [0, 1, 2, 3, 4, 5, 6], "allow_critical": True}

    def d(self, wd, h, m=0):
        return datetime(2026, 10, 19 + wd, h, m)          # 2026-10-19 is a Monday

    def test_quiet_window(self):
        self.assertTrue(A.in_quiet(self.Q, self.d(0, 23)))
        self.assertTrue(A.in_quiet(self.Q, self.d(1, 3)))
        self.assertFalse(A.in_quiet(self.Q, self.d(1, 7)))
        self.assertFalse(A.in_quiet(self.Q, self.d(1, 12)))
        q = dict(self.Q, days=[4])                         # begins Friday only
        self.assertTrue(A.in_quiet(q, self.d(5, 3)))       # Saturday 03:00 belongs to Friday's
        self.assertFalse(A.in_quiet(q, self.d(4, 3)))
        self.assertFalse(A.in_quiet(dict(self.Q, enabled=False), self.d(0, 23)))

    def test_hook_policy(self):
        night, noon = self.d(0, 23), self.d(0, 12)
        self.assertFalse(A.hook_policy("info", "warning", self.Q, noon))      # under the minimum
        self.assertTrue(A.hook_policy("warning", "warning", self.Q, noon))
        self.assertFalse(A.hook_policy("warning", "warning", self.Q, night))  # quiet
        self.assertTrue(A.hook_policy("critical", "warning", self.Q, night))  # allowed through
        self.assertFalse(A.hook_policy("critical", "warning",
                                       dict(self.Q, allow_critical=False), night))

    def test_hook_really_runs_with_the_message_in_the_environment_only(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            script = os.path.join(d, "hook.sh")
            put(d, "hook.sh", '#!/bin/sh\nprintf "%s|%s|%s|%s|%s|%s|argv=%s" "$PEEK_SEVERITY" '
                              '"$PEEK_TITLE" "$PEEK_BODY" "$PEEK_SOURCE" "$PEEK_HOST" '
                              '"${PEEK_PAIR_CODE:-none}" "$#" > "' + out + '"\n')
            os.chmod(script, 0o755)
            h = A.Hook("studio")
            h.configure([script], "warning", dict(self.Q, enabled=False))
            hostile = {"severity": "critical", "title": "$(touch %s/pwned); `id`" % d,
                       "body": "a'b\"c", "source": "S", "id": "x", "ts": int(time.time())}
            with mock.patch.dict(os.environ, {"PEEK_PAIR_CODE": "K7M2-P4QX-9R"}):
                self.assertTrue(h.fire(hostile))
                for _ in range(50):
                    if os.path.exists(out):
                        break
                    time.sleep(0.05)
            text = slurp(out)
            self.assertTrue(text.startswith("critical|$(touch "))      # arrived verbatim
            self.assertIn("|studio|none|argv=0", text)                 # no secret, no argv
            self.assertFalse(os.path.exists(os.path.join(d, "pwned"))) # never interpreted

    def test_hook_failure_and_missing_program_do_not_crash(self):
        h = A.Hook("h")
        h.configure(["/no/such/program"], "info", dict(self.Q, enabled=False))
        self.assertTrue(h.fire({"severity": "critical", "title": "t", "source": "s",
                                "id": "i", "ts": 1}))
        for _ in range(40):
            if h.last_error:
                break
            time.sleep(0.05)
        self.assertTrue(h.last_error)
        self.assertEqual(A.Hook("h").fire({"severity": "critical", "title": "t", "source": "s",
                                           "id": "i", "ts": 1}), False)   # no hook set


class FakeEnv:
    def __init__(self, rules, **vals):
        self.vals = dict({"cpu_percent": 10.0, "ram_percent": 40.0}, **vals)
        self.t = 100000.0
        self.out = A.Outbox(host="studio", clock=lambda: self.t)
        taken = set()
        self.eng = A.AlertEngine(lambda: A.flatten(self.vals), self.out, "studio",
                                 clock=lambda: self.t)
        self.eng.configure([A.clean_rule(r, i, taken)[0] for i, r in enumerate(rules)])

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
        self.assertEqual(e.out.active(), [])
        e.run(40)
        a = e.out.active()
        self.assertEqual((len(a), a[0]["severity"]), (1, "critical"))
        self.assertIn("CPU high on studio", a[0]["title"])
        self.assertIn("95%", a[0]["body"])

    def test_dip_resets_clock_and_one_message_per_episode(self):
        e = FakeEnv([CPU])
        e.run(50, cpu_percent=95.0)
        e.run(10, cpu_percent=50.0)
        e.run(50, cpu_percent=95.0)
        self.assertEqual(e.out.active(), [])
        e = FakeEnv([CPU])
        e.run(300, cpu_percent=95.0)
        self.assertEqual(len(e.out.active()), 1)
        e.run(60, cpu_percent=20.0)
        self.assertIn("CPU back to normal on studio", [m["title"] for m in e.out.active()])
        e.run(120, cpu_percent=96.0)
        self.assertEqual(len([m for m in e.out.active() if m["severity"] == "critical"]), 2)

    def test_brief_flap_while_active_does_not_recover_or_refire(self):
        e = FakeEnv([CPU])
        e.run(100, cpu_percent=95.0)
        e.run(10, cpu_percent=50.0)
        e.run(100, cpu_percent=95.0)
        self.assertEqual(len(e.out.active()), 1)

    def test_below_unplugged_and_no_sensor(self):
        rule = {"id": "bat", "metric": "battery_percent", "op": "below", "limit": 20,
                "for": 0, "unplugged": True}
        e = FakeEnv([rule], battery_percent=15.0, battery_ac=True, battery_charging=False)
        e.run(30)
        self.assertEqual(e.out.active(), [])
        e.run(10, battery_ac=False)
        self.assertEqual(len(e.out.active()), 1)
        hot = {"id": "t", "metric": "cpu_temp_c", "op": "above", "limit": 1, "for": 0}
        e = FakeEnv([hot], cpu_temp_c=-1.0)                            # the Pi sentinel for "none"
        e.run(60)
        self.assertEqual(e.out.active(), [])

    def test_extra_sensor_with_its_own_label_and_derived_network(self):
        rule = {"id": "v", "metric": "vram_gb", "op": "above", "limit": 8, "for": 0}
        e = FakeEnv([rule])
        e.vals["metrics"] = {"vram_gb": {"value": 9.5, "label": "Video memory", "unit": "GB"}}
        e.run(10)
        m = e.out.active()[0]
        self.assertIn("Video memory high", m["title"])
        self.assertIn("9.5 GB", m["body"])
        flat, _ = A.flatten({"net_rx_kbps": 300.0, "net_tx_kbps": 50.0})
        self.assertEqual(flat["net_kbps"], 350.0)

    def test_wording_placeholders_are_safe(self):
        rule = dict(CPU, title="{machine} is on fire", body="{label}={value} ({limit}, {duration}) {nope}")
        e = FakeEnv([rule])
        e.run(80, cpu_percent=95.0)
        m = e.out.active()[0]
        self.assertEqual((m["title"], m["body"]),
                         ("studio is on fire", "CPU=95% (90%, 1 min) {nope}"))
        self.assertEqual(A.fill("{value.__class__}", {"value": "x"}), "{value.__class__}")

    def test_unusual(self):
        rule = {"id": "net", "metric": "net_kbps", "op": "unusual", "min": 1000, "for": 0}
        e = FakeEnv([rule], net_rx_kbps=5000.0, net_tx_kbps=0.0)
        e.run(30)
        self.assertEqual(e.out.active(), [])                           # no history: says nothing
        e.eng.hist["net_kbps"] = [(int(e.t // 60) - 480 + i, 200.0 + (i % 7) * 10)
                                  for i in range(480)]
        e.run(10, net_rx_kbps=230.0)
        self.assertEqual(e.out.active(), [])
        e.run(10, net_rx_kbps=9000.0)
        self.assertIn("Unusual Network", e.out.active()[0]["title"])
        e = FakeEnv([dict(rule, min=5000)], net_rx_kbps=3000.0, net_tx_kbps=0.0)
        e.eng.hist["net_kbps"] = [(int(e.t // 60) - 480 + i, 10.0) for i in range(480)]
        e.run(30)
        self.assertEqual(e.out.active(), [])                           # under the floor

    def test_history_only_for_unusual_rules_and_persists(self):
        e = FakeEnv([CPU])
        e.run(200, cpu_percent=50.0)
        self.assertEqual(e.eng.hist, {})
        with tempfile.TemporaryDirectory() as d:
            e = FakeEnv([{"id": "n", "metric": "cpu_percent", "op": "unusual", "for": 0}])
            e.eng.history_path = os.path.join(d, "h.json")
            e.run(200, cpu_percent=40.0)
            self.assertGreaterEqual(len(e.eng.hist["cpu_percent"]), 2)
            e.eng.stop()
            again = A.AlertEngine(lambda: ({}, {}), e.out, "studio",
                                  history_path=e.eng.history_path, clock=lambda: e.t)
            self.assertEqual(again.hist["cpu_percent"], e.eng.hist["cpu_percent"])

    def test_reconfigure_mid_incident_does_not_refire(self):
        e = FakeEnv([CPU])
        e.run(100, cpu_percent=95.0)
        e.eng.configure([A.clean_rule(dict(CPU, limit=85), 0, set())[0]])    # the file was edited
        e.run(100)
        self.assertEqual(len(e.out.active()), 1)

    def test_disabled_rule_and_master_switch(self):
        e = FakeEnv([dict(CPU, enabled=False)])
        e.run(200, cpu_percent=99.0)
        self.assertEqual(e.out.active(), [])
        e = FakeEnv([CPU])
        e.eng.configure([A.clean_rule(CPU, 0, set())[0]], enabled=False)     # PEEK_ALERTS=0
        e.run(200, cpu_percent=99.0)
        self.assertEqual(e.out.active(), [])

    def test_a_failing_source_does_not_kill_the_engine(self):
        eng = A.AlertEngine(lambda: 1 / 0, A.Outbox(), "h", interval=0.01)
        eng.start()
        time.sleep(0.15)
        eng.stop()
        self.assertIn("ZeroDivisionError", eng.last_error)

    def test_describe_rule(self):
        self.assertEqual(A.describe_rule(A.default_rules()[0]),
                         "CPU above 90% for 5 min - warning")
        self.assertIn("on battery only", A.describe_rule(A.default_rules()[4]))


class TestConfigFile(unittest.TestCase):
    def test_defaults_when_missing(self):
        cfg, warns, status = A.load_config_file("/no/such/alerts.json")
        self.assertEqual((status, warns, len(cfg["rules"])), ("missing", [], 7))

    def test_hand_edited_mistakes_are_reported_not_fatal(self):
        cfg, warns = A.clean_config({
            "quiet": {"enabled": True, "start": "25:99"},
            "rules": [{"metric": "cpu_percent", "op": "above"},               # no limit
                      {"metric": "x y", "op": "above", "limit": 1},
                      {"metric": "ram_percent", "op": "sideways", "limit": 1},
                      {"metric": "ram_percent", "op": "above", "limit": 80,
                       "severity": "RED", "for": "300"}],
            "sensors": [{"key": "ok_w", "source": "file", "path": "p"}, {"key": ""}],
            "hook": 5, "hook_min_severity": "loud"})
        self.assertEqual(len(cfg["rules"]), 1)
        self.assertEqual((cfg["rules"][0]["severity"], cfg["rules"][0]["for"]), ("critical", 300))
        self.assertEqual(cfg["quiet"]["start"], "22:00")
        self.assertEqual(len(cfg["sensors"]), 1)
        self.assertGreaterEqual(len(warns), 6)
        self.assertEqual(A.clean_config({"rules": []})[0]["rules"], [])        # deleted all: stays empty
        self.assertEqual(A.clean_config({"hook": "/bin/echo hi there"})[0]["hook"],
                         ["/bin/echo", "hi", "there"])

    def test_reload_keeps_the_old_settings_when_the_file_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "alerts.json")
            put(d, "alerts.json", json.dumps({"rules": [dict(CPU, limit=77)]}))
            svc = A.Services("studio", alerts_file=path, state=False)
            svc._sig = None
            svc.load_config()
            self.assertEqual(svc.engine.rules[0]["limit"], 77)
            svc._sig = svc._file_sig()
            put(d, "alerts.json", "{ typo")                              # mid-edit
            lines = svc.load_config()
            self.assertTrue(any("not valid JSON" in l for l in lines))
            self.assertEqual(svc.engine.rules[0]["limit"], 77)           # rules did not vanish
            put(d, "alerts.json", json.dumps({"rules": [dict(CPU, limit=66)]}))
            svc.load_config()
            self.assertEqual(svc.engine.rules[0]["limit"], 66)

    def test_unreadable_file_says_how_to_fix_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "alerts.json")
            put(d, "alerts.json", "{}")
            with mock.patch("builtins.open", side_effect=PermissionError()):
                cfg, warns, status = A.load_config_file(path)
            self.assertEqual(status, "error")
            self.assertIn("sudo peekesp repair", warns[0])


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.path = os.path.join(self.d, "alerts.json")
        p = mock.patch.object(A, "ALERTS_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = A.run_subcommand(list(argv))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
            err.write(str(e.code) if not isinstance(e.code, int) else "")
        return rc, out.getvalue(), err.getvalue()

    def raw(self):
        return json.loads(slurp(self.path))

    def test_rules_lifecycle(self):
        rc, out, _ = self.run_cli("rules")
        self.assertIn("cpu_high", out)
        self.assertFalse(os.path.exists(self.path))                      # listing writes nothing
        self.assertEqual(self.run_cli("rules", "off", "cpu_high")[0], 0)
        self.assertFalse([r for r in self.raw()["rules"] if r["id"] == "cpu_high"][0]["enabled"])
        rc, out, _ = self.run_cli("rules", "add", "gpu_percent", "above", "95", "--for", "120",
                                  "--severity", "red", "--id", "gpu_hot")
        self.assertIn("GPU above 95% for 2 min - critical", out)
        self.assertEqual(self.run_cli("rules", "rm", "gpu_hot")[0], 0)
        self.assertNotIn("gpu_hot", [r["id"] for r in self.raw()["rules"]])
        self.assertEqual(self.run_cli("rules", "reset")[0], 0)
        self.assertEqual(len(self.raw()["rules"]), 7)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_rules_bad_input_is_refused_and_writes_nothing(self):
        self.assertNotEqual(self.run_cli("rules", "on", "nosuch")[0], 0)
        self.assertNotEqual(self.run_cli("rules", "add", "cpu_percent", "above")[0], 0)   # no limit
        self.assertNotEqual(self.run_cli("rules", "add", "cpu_percent", "above", "9", "--severity", "loud")[0], 0)
        self.assertNotEqual(self.run_cli("rules", "add", "Bad Metric", "above", "9")[0], 0)
        self.assertFalse(os.path.exists(self.path))

    def test_a_broken_file_is_never_overwritten(self):
        put(self.d, "alerts.json", "{ my careful notes")
        rc, _o, err = self.run_cli("rules", "off", "cpu_high")
        self.assertNotEqual(rc, 0)
        self.assertEqual(slurp(self.path), "{ my careful notes")

    def test_unknown_keys_are_preserved(self):
        put(self.d, "alerts.json", json.dumps({"my_note": "keep me", "rules": []}))
        self.run_cli("rules", "add", "cpu_percent", "above", "50")
        self.assertEqual(self.raw()["my_note"], "keep me")

    def test_quiet(self):
        self.assertIn("off", self.run_cli("quiet")[1])
        rc, out, _ = self.run_cli("quiet", "22:00", "07:00", "--days", "fri,sat", "--block-critical")
        self.assertIn("22:00-07:00, fri,sat, critical silenced too", out)
        q = self.raw()["quiet"]
        self.assertEqual((q["days"], q["allow_critical"]), ([4, 5], False))
        self.assertIn("weekdays" if False else "mon,tue,wed,thu,fri",
                      self.run_cli("quiet", "09:00", "17:00", "--days", "weekdays")[1])
        self.assertIn("off", self.run_cli("quiet", "off")[1])
        self.assertNotEqual(self.run_cli("quiet", "25:00", "07:00")[0], 0)
        self.assertNotEqual(self.run_cli("quiet", "22:00", "07:00", "--days", "funday")[0], 0)

    def test_hook(self):
        rc, out, _ = self.run_cli("hook", "set", "--min", "critical", "/bin/echo", "hi there")
        self.assertEqual(rc, 0)
        self.assertEqual(self.raw()["hook"], ["/bin/echo", "hi there"])
        self.assertEqual(self.raw()["hook_min_severity"], "critical")
        self.assertIn("from critical up", self.run_cli("hook")[1])
        self.assertIn("none", self.run_cli("hook", "off")[1])
        self.assertNotEqual(self.run_cli("hook", "set")[0], 0)

    def test_sensors_add_test_rm(self):
        f = os.path.join(self.d, "w")
        put(self.d, "w", "48500")
        rc, out, _ = self.run_cli("sensors", "add", "tank_c", "file", f, "--scale", "0.001",
                                  "--unit", "\u00b0C", "--label", "Tank")
        self.assertIn("reads 48.5", out)
        self.assertIn("48.5", self.run_cli("sensors", "test", "tank_c")[1])
        self.assertNotEqual(self.run_cli("sensors", "add", "cpu_percent", "file", f)[0], 0)
        self.assertEqual(self.run_cli("sensors", "rm", "tank_c")[0], 0)
        self.assertEqual(self.raw()["sensors"], [])
        self.assertNotEqual(self.run_cli("sensors", "rm", "tank_c")[0], 0)

    def test_reading_an_unreadable_file_does_not_show_defaults_as_the_real_thing(self):
        put(self.d, "alerts.json", "{ typo")
        for argv in (("rules",), ("quiet",), ("hook",), ("sensors", "test", "x")):
            rc, out, err = self.run_cli(*argv)
            self.assertNotEqual(rc, 0, argv)
            self.assertNotIn("cpu_high", out)
        self.assertIn("UNREADABLE", self.run_cli("alerts")[1])

    def test_alerts_check_summary_init(self):
        self.assertIn("defaults", self.run_cli("alerts")[1])
        self.assertEqual(self.run_cli("alerts", "init")[0], 0)
        self.assertEqual(len(self.raw()["rules"]), 7)
        self.assertEqual(self.run_cli("alerts", "check")[0], 0)
        put(self.d, "alerts.json", json.dumps({"rules": [{"metric": "x", "op": "nope"}]}))
        rc, out, _ = self.run_cli("alerts", "check")
        self.assertEqual(rc, 1)
        self.assertIn("problem:", out)
        put(self.d, "alerts.json", "{ typo")
        rc, out, _ = self.run_cli("alerts", "check")
        self.assertEqual(rc, 1)
        self.assertIn("NOT loaded", out)
        self.assertNotIn("7 rule", out)                                  # not the defaults, as if real

    def test_notify_cli_drops_a_file_the_watcher_picks_up(self):
        box = os.path.join(self.d, "ob")
        os.makedirs(box)
        rc, out, _ = self.run_cli("notify", "--title", "Hi", "--severity", "red", "--id", "cli-1",
                                  "--outbox", box)
        self.assertEqual(rc, 0)
        ob = A.Outbox(host="studio")
        A.OutboxWatcher(ob, box).scan()
        self.assertEqual(ob.active()[0]["id"], "studio:cli-1")
        self.assertNotEqual(self.run_cli("notify")[0], 0)
        rc, _o, err = self.run_cli("notify", "--title", "x", "--severity", "reed", "--outbox", box)
        self.assertIn("will show as info", err)

    def test_notify_without_a_writable_folder_says_what_to_do(self):
        with mock.patch.dict(os.environ, {"PEEK_STATE_DIR": "/nonexistent", "HOME": "/nonexistent"}), \
                mock.patch.object(os.path, "isdir", return_value=False):
            rc, _o, err = self.run_cli("notify", "--title", "x")
        self.assertEqual(rc, 1)
        self.assertIn("usermod -aG peekesp", err)

    def test_notify_http(self):
        ob = A.Outbox(host="studio")
        port = free_port()
        key_dir = os.path.join(self.d, "state")
        os.makedirs(key_dir)
        srv = A.NotifyServer(ob, A.load_or_create_key(key_dir), port)
        self.assertTrue(srv.start())
        self.addCleanup(srv.stop)
        with mock.patch.dict(os.environ, {"PEEK_STATE_DIR": key_dir}):
            rc, out, _ = self.run_cli("notify", "--http", "--port", str(port), "--title", "T",
                                      "--severity", "red", "--id", "h1")
        self.assertEqual(rc, 0)
        self.assertEqual(ob.active()[0]["id"], "studio:h1")


class TestReading(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        put(self.d, "w", "812")
        self.svc = A.Services("studio", alerts_file=os.path.join(self.d, "a.json"),
                              snapshot_fn=lambda: A.snapshot(messages=False), state=False)
        self.svc.extras.configure(False, 10.0, [{"key": "solar_w", "source": "file",
                                                 "path": os.path.join(self.d, "w"), "label": "Solar",
                                                 "unit": "W", "interval": 5, "scale": 1.0, "max": None}])
        self.svc.extras.collect_once(force=True)
        self.addCleanup(lambda: setattr(A, "_services", None))

    def test_without_services_it_is_the_standard_reading(self):
        A._services = None
        snap = A.snapshot()
        self.assertNotIn("messages", snap)
        self.assertNotIn("metrics", snap)
        self.assertIn("cpu_percent", snap)

    def test_extras_and_messages_ride_along_but_never_on_the_lan(self):
        A._services = self.svc
        self.svc.outbox.add({"id": "x", "title": "New login from 203.0.113.9", "severity": "red",
                             "source": "Defender"})
        snap = A.snapshot()
        self.assertEqual(snap["metrics"]["solar_w"]["value"], 812.0)
        self.assertEqual(snap["messages"][0]["source"], "Defender")
        lan = A.snapshot(messages=False)
        self.assertNotIn("messages", lan)
        self.assertIn("metrics", lan)

    def test_a_burst_stays_inside_what_a_device_can_hold(self):
        A._services = self.svc
        for i in range(30):
            self.svc.outbox.add({"id": "m%d" % i, "title": "t" * 80, "body": "b" * 400,
                                 "severity": "red", "source": "s" * 40}, internal=True)
        snap = A.snapshot()
        self.assertLessEqual(len(snap["messages"]), 10)
        self.assertLess(len(json.dumps(snap)), 8000)

    def test_a_hook_runs_for_a_new_message_and_not_for_a_resend(self):
        calls = []
        self.svc.hook.fire = lambda m: calls.append(m["id"])
        self.svc.outbox.add({"id": "a", "title": "x", "severity": "red"})
        self.svc.outbox.add({"id": "a", "title": "x", "severity": "red"})
        self.svc.outbox.add({"id": "old", "title": "x", "ts": time.time() - 7 * 3600})
        self.assertEqual(calls, ["studio:a"])

    def test_start_stop_start_does_not_pile_up_threads(self):
        svc = A.Services("studio", alerts_file=os.path.join(self.d, "a.json"), state=False)
        before = threading.active_count()
        with mock.patch("builtins.print"):
            for _ in range(4):
                svc.start()
                time.sleep(0.05)
        svc.stop()
        time.sleep(1.5)
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_starting_with_no_state_directory_says_so_and_still_runs(self):
        svc = A.Services("studio", alerts_file=os.path.join(self.d, "a.json"), state=False)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            svc.start()
        svc.stop()
        self.assertIn("StateDirectory=peekesp", buf.getvalue())
        self.assertIsNone(svc.server)                                    # no key, so no endpoint
        self.assertEqual(svc.outbox.add({"title": "x"})[0], "new")


class TestEndToEnd(unittest.TestCase):
    """The real agent, as a subprocess, pushing to a fake relay."""

    def test_the_whole_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        state = os.path.join(d, "state")
        os.makedirs(state)
        value = os.path.join(d, "solar")
        put(d, "solar", "50")
        hook_out = os.path.join(d, "hook_out")
        put(d, "hook.sh", '#!/bin/sh\necho "$PEEK_SEVERITY|$PEEK_TITLE|${PEEK_PAIR_CODE:-none}" >> "' + hook_out + '"\n')
        os.chmod(os.path.join(d, "hook.sh"), 0o755)
        alerts = os.path.join(d, "alerts.json")
        put(d, "alerts.json", json.dumps({
            "sensors": [{"key": "solar_w", "source": "file", "path": value, "label": "Solar",
                         "unit": "W", "interval": 2}],
            "rules": [{"id": "solar_hi", "metric": "solar_w", "op": "above", "limit": 100,
                       "for": 0, "severity": "critical"}],
            "hook": [os.path.join(d, "hook.sh")], "hook_min_severity": "info"}))

        pushes, lock = [], threading.Lock()

        class Relay(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    pushes.append(body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        relay = HTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        self.addCleanup(relay.shutdown)
        self.addCleanup(relay.server_close)

        nport = free_port()
        env = dict(os.environ, PEEK_STATE_DIR=state, PEEK_ALERTS_FILE=alerts,
                   PEEK_NOTIFY_PORT=str(nport), PEEK_INTERVAL="1",
                   PEEK_PAIR_CODE="K7M2-P4QX-9R", PEEK_PUSH_TOKEN="should-not-leak",
                   PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [sys.executable, AGENT_PATH, "--relay-base", "http://127.0.0.1:%d" % relay.server_port,
             "--no-serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(5), proc.stdout.close()))

        def wait_for(pred, secs, what):
            end = time.time() + secs
            while time.time() < end:
                with lock:
                    hit = [p for p in pushes if pred(p)]
                if hit:
                    return hit[-1]
                time.sleep(0.2)
            proc.terminate()
            out = proc.communicate(timeout=5)[0].decode()
            self.fail("timed out waiting for %s. agent said:\n%s" % (what, out))

        # 1. the custom sensor rides in the reading
        p = wait_for(lambda p: "solar_w" in p.get("metrics", {}), 20, "the custom sensor")
        self.assertEqual(p["metrics"]["solar_w"]["unit"], "W")
        self.assertIn("host", p)
        self.assertNotIn("messages", p)

        # 2. the key exists, private, and a program can send through the endpoint
        keyfile = os.path.join(state, "notify.key")
        for _ in range(50):
            if os.path.exists(keyfile):
                break
            time.sleep(0.1)
        self.assertEqual(stat.S_IMODE(os.stat(keyfile).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(state, "outbox")).st_mode), 0o2770)
        key = slurp(keyfile).strip()
        c = http.client.HTTPConnection("127.0.0.1", nport, timeout=3)
        c.request("POST", "/notify", json.dumps({"title": "Intrusion", "severity": "red",
                                                "id": "fw-1", "source": "Defender"}),
                  {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 200)
        p = wait_for(lambda p: any(m["id"].endswith(":fw-1") for m in p.get("messages", [])),
                     10, "the endpoint message")
        m = [m for m in p["messages"] if m["id"].endswith(":fw-1")][0]
        self.assertEqual((m["severity"], m["source"]), ("critical", "Defender"))

        # 3. the folder route, via the real CLI as a subprocess
        r = subprocess.run([sys.executable, AGENT_PATH, "notify", "--title", "From a script",
                            "--severity", "warning", "--id", "cli-1"],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0, r.stderr)
        wait_for(lambda p: any(m["id"].endswith(":cli-1") for m in p.get("messages", [])),
                 10, "the file-drop message")

        # 4. a rule fires on the custom sensor, within a few engine ticks
        put(d, "solar", "500")
        p = wait_for(lambda p: any("Solar high" in m["title"] for m in p.get("messages", [])),
                     25, "the rule to fire")
        rule_msg = [m for m in p["messages"] if "Solar high" in m["title"]][0]
        self.assertEqual((rule_msg["severity"], rule_msg["source"]), ("critical", "PeekESP rules"))
        # re-sent in later readings under the same id, so a missed push cannot lose it
        n0 = len(pushes)
        wait_for(lambda p: pushes.index(p) >= n0 and any(m["id"] == rule_msg["id"]
                                                         for m in p.get("messages", [])),
                 10, "the same id again")

        # 5. the hook ran for each, without the credential
        for _ in range(40):
            if os.path.exists(hook_out) and len(slurp(hook_out).splitlines()) >= 3:
                break
            time.sleep(0.2)
        lines = slurp(hook_out).splitlines()
        self.assertTrue(any(l.startswith("critical|Intrusion|none") for l in lines), lines)
        self.assertTrue(all(l.endswith("|none") for l in lines), lines)    # never saw the pair code

        # 6. editing alerts.json is picked up without a restart
        put(d, "alerts.json", json.dumps({"rules": [], "sensors": []}))
        end = time.time() + 15
        while time.time() < end:
            with lock:
                last = pushes[-1]
            if "solar_w" not in last.get("metrics", {}):
                break
            time.sleep(0.3)
        else:
            self.fail("removing the sensor from alerts.json did not stop it being reported")

    def test_once_prints_a_reading_with_custom_sensors(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        put(d, "t", "48500")
        put(d, "alerts.json", json.dumps({"sensors": [
            {"key": "tank_c", "source": "file", "path": os.path.join(d, "t"),
             "scale": 0.001, "unit": "\u00b0C"}]}))
        env = dict(os.environ, PEEK_STATE_DIR=d, PEEK_ALERTS_FILE=os.path.join(d, "alerts.json"))
        r = subprocess.run([sys.executable, AGENT_PATH, "--once"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.decode())
        self.assertEqual(out["metrics"]["tank_c"]["value"], 48.5)
        self.assertGreaterEqual(out["uptime_seconds"], 0)


class TestOnceHasNoSideEffects(unittest.TestCase):
    def test_once_creates_no_directories(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        env = dict(os.environ, HOME=home, PEEK_ALERTS_FILE=os.path.join(home, "none.json"))
        env.pop("PEEK_STATE_DIR", None)
        r = subprocess.run([sys.executable, AGENT_PATH, "--once"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(home), [])                           # nothing written anywhere


class TestShellScripts(unittest.TestCase):
    def test_scripts_parse(self):
        for name in ("peekesp", "install.sh"):
            p = os.path.join(HERE, name)
            if os.path.exists(p):
                r = subprocess.run(["sh", "-n", p], stderr=subprocess.PIPE)
                self.assertEqual(r.returncode, 0, "%s: %s" % (name, r.stderr.decode()))


if __name__ == "__main__":
    unittest.main()