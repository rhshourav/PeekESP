"""
test_synology.py - tests for the Synology agent.

    python3 -m unittest test_synology -v

Runs on any Linux, with no Synology and no root: every parser takes text, and the
collector is pointed at a fake /proc and /sys built in a temporary directory.
TestEndToEnd starts the real agent against a fake relay on localhost and flips
both switches while it runs.
"""

import http.client
import importlib.util
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The agent logs to stderr by default; a test run should not.
logging.getLogger("peekesp").addHandler(logging.NullHandler())
logging.getLogger("peekesp").propagate = False

spec = importlib.util.spec_from_file_location("peek_syn", HERE / "peek-agent-synology.py")
S = importlib.util.module_from_spec(spec)
sys.modules["peek_syn"] = S
spec.loader.exec_module(S)
S.setup_logging = lambda *a, **k: None       # main() would otherwise put stderr logging back


# ---------------------------------------------------------------- fixtures
MDSTAT_HEALTHY = """Personalities : [raid1] [raid6] [raid5] [raid4]
md2 : active raid5 sda3[0] sdb3[1] sdc3[2] sdd3[3]
      11701135296 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/4] [UUUU]

md1 : active raid1 sda2[0] sdb2[1] sdc2[2] sdd2[3]
      2097088 blocks [4/4] [UUUU]

md0 : active raid1 sda1[0] sdb1[1] sdc1[2] sdd1[3]
      2490176 blocks [4/4] [UUUU]

unused devices: <none>
"""

MDSTAT_DEGRADED = MDSTAT_HEALTHY.replace(
    "sda3[0] sdb3[1] sdc3[2] sdd3[3]\n      11701135296 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/4] [UUUU]",
    "sda3[0] sdb3[1] sdd3[3]\n      11701135296 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/3] [UU_U]")

MDSTAT_REBUILDING = MDSTAT_DEGRADED.replace(
    "[4/3] [UU_U]\n",
    "[4/3] [UU_U]\n      [==>..................]  recovery = 12.6% (493040384/3900378432) finish=412.9min speed=137510K/sec\n")

MDSTAT_SYNCING = MDSTAT_HEALTHY.replace(
    "[4/4] [UUUU]\n\nmd1",
    "[4/4] [UUUU]\n      [>....................]  check =  3.4% (132000000/3900378432) finish=700.0min speed=90000K/sec\n\nmd1")

# A spare bay's drive was pulled: only the DSM system partition and swap notice.
MDSTAT_SYSTEM_ONLY_DEGRADED = MDSTAT_HEALTHY.replace(
    "sda1[0] sdb1[1] sdc1[2] sdd1[3]\n      2490176 blocks [4/4] [UUUU]",
    "sda1[0] sdb1[1] sdc1[2]\n      2490176 blocks [4/3] [UUU_]")

MDSTAT_INACTIVE = """Personalities : [raid1]
md2 : active raid1 sda3[0]
      1000000 blocks super 1.2 [1/1] [U]

md3 : inactive sdb3[1](S)
      2930000 blocks super 1.2

unused devices: <none>
"""

MDSTAT_RAID0_AND_FAILED_MEMBER = """Personalities : [raid0] [raid1]
md2 : active raid0 sda3[0] sdb3[1]
      3900000000 blocks super 1.2 64k chunks

md3 : active (auto-read-only) raid1 sdc3[0] sdd3[1](F)
      1000000 blocks super 1.2 [2/1] [U_]
      bitmap: 0/8 pages [0KB], 65536KB chunk

unused devices: <none>
"""

MOUNTS = """/dev/md0 / ext4 rw,relatime 0 0
none /tmp tmpfs rw 0 0
/dev/mapper/cachedev_0 /volume1 btrfs rw,relatime,nospace_cache 0 0
/dev/mapper/cachedev_0 /volume1/@docker btrfs rw,relatime 0 0
/dev/mapper/cachedev_0 /volume1/@docker/btrfs/subvolumes/abc123 btrfs rw 0 0
overlay /volume1/@docker/overlay2/xyz/merged overlay rw 0 0
/dev/md3 /volume2 ext4 rw,relatime 0 0
/dev/md4 /volume10 ext4 rw,relatime 0 0
/dev/sdq1 /volumeUSB1/usbshare ext4 rw 0 0
/dev/mapper/cachedev_0 /volume1/@sharesnap/photo/GMT+00-2026.01.01 btrfs ro 0 0
//nas/share /volume3 cifs rw 0 0
/dev/md9 /volume 7 ext4 rw 0 0
"""

SMART_HDD = """smartctl 7.2 2020-12-30 r5155 [x86_64-linux-4.4.302+] (local build)
=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  1 Raw_Read_Error_Rate     0x000f   100   064   006    Pre-fail  Always       -       0
190 Airflow_Temperature_Cel 0x0022   066   057   045    Old_age   Always       -       31 (Min/Max 22/38)
194 Temperature_Celsius     0x0022   034   043   000    Old_age   Always       -       34 (0 22 0 0 0)
"""

SMART_HDD_PACKED = SMART_HDD.replace("34 (0 22 0 0 0)", "124554051619")   # 0x1D0 0000 0023 -> low byte 35
SMART_ONLY_190 = SMART_HDD.replace("194 Temperature_Celsius", "193 Load_Cycle_Count")
SMART_FAILED = SMART_HDD.replace("test result: PASSED", "test result: FAILED!")
SMART_NVME = """=== START OF SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED
SMART/Health Information (NVMe Log 0x02)
Critical Warning:                   0x00
Temperature:                        41 Celsius
Available Spare:                    100%
"""
SMART_SAS = """SMART Health Status: OK
Current Drive Temperature:     37 C
Drive Trip Temperature:        65 C
"""
SMART_SAS_BAD = SMART_SAS.replace("Status: OK", "Status: FAILURE PREDICTION THRESHOLD EXCEEDED")
SMART_STANDBY = "smartctl 7.2 ...\nDevice is in STANDBY mode, exit(2)\n"
SMART_SLEEP = "smartctl 7.2 ...\nDevice is in SLEEP mode, exit(2)\n"

UPSC = """battery.charge: 87
battery.runtime: 1740
ups.status: OL CHRG
ups.model: Back-UPS ES 700G
"""


def fake_system(root, stat="cpu  100 0 100 800 0 0 0 0 0 0\n", net=None, mounts=MOUNTS,
                mdstat=MDSTAT_HEALTHY, meminfo=None):
    """A fake /proc under root/proc and a fake /sys under root/sys."""
    p = Path(root)
    (p / "proc" / "net").mkdir(parents=True, exist_ok=True)
    (p / "sys").mkdir(exist_ok=True)
    (p / "proc" / "stat").write_text(stat)
    (p / "proc" / "net" / "dev").write_text(net or NET_DEV.format(e0rx=1000, e0tx=500, b0rx=0, b0tx=0))
    (p / "proc" / "mounts").write_text(mounts)
    (p / "proc" / "uptime").write_text("271830.12 1000000.00\n")
    if mdstat is not None:
        (p / "proc" / "mdstat").write_text(mdstat)
    (p / "proc" / "meminfo").write_text(meminfo or MEMINFO)
    return str(p / "proc"), str(p / "sys")


NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 99999999  100    0    0    0     0          0         0 99999999  100    0    0    0     0       0          0
  eth0: {e0rx}  100    0    0    0     0          0         0 {e0tx}  100    0    0    0     0       0          0
  eth1: 5000  100    0    0    0     0          0         0 6000  100    0    0    0     0       0          0
 bond0: {b0rx}  100    0    0    0     0          0         0 {b0tx}  100    0    0    0     0       0          0
docker0: 777777  10    0    0    0     0          0         0 888888  10    0    0    0     0       0          0
veth1a2b: 555  1    0    0    0     0          0         0 666  1    0    0    0     0       0          0
ovs_eth0: 123456  1    0    0    0     0          0         0 654321  1    0    0    0     0       0          0
eth0.100: 4242  1    0    0    0     0          0         0 2424  1    0    0    0     0       0          0
"""

MEMINFO = """MemTotal:        8000000 kB
MemFree:          200000 kB
MemAvailable:    6000000 kB
Buffers:          100000 kB
Cached:          5000000 kB
"""


# --------------------------------------------------------------- pairing
class TestPairing(unittest.TestCase):
    def test_vector_from_the_firmware(self):
        # The same vector PeekESP.ino pins in its comment and the Worker's tests assert.
        d = S.derive("K7M2P4QX9R")
        self.assertEqual(d["stream"], "4b907ba136d0a7f2")
        self.assertEqual(d["read"], "ec3cb3699bd1284efb2fcfe056609e87edf4813b84e9ce84")
        self.assertEqual(len(d["push"]), 48)
        self.assertNotEqual(d["push"], d["read"])

    def test_dashes_and_case_are_ignored(self):
        self.assertEqual(S.derive("k7m2-p4qx-9r"), S.derive("K7M2P4QX9R"))
        self.assertEqual(S.format_code("k7m2p4qx9r"), "K7M2-P4QX-9R")

    def test_bad_codes_are_refused_with_a_reason(self):
        for bad in ("", "SHORT", "K7M2P4QX9", "K7M2P4QX9RR", "K7M2P4QX0R", "I7M2P4QX9R"):
            with self.assertRaises(ValueError, msg=bad):
                S.derive(bad)


# --------------------------------------------------------------- parsers
class TestProcParsers(unittest.TestCase):
    def test_cpu_stat_counts_iowait_as_idle_and_skips_guest(self):
        # user nice system idle iowait irq softirq steal guest guest_nice
        busy, total = S.parse_cpu_stat("cpu  100 10 50 700 100 5 5 30 999 999\ncpu0 1 1 1 1 1 1 1 1 1 1\n")
        self.assertEqual(busy, 100 + 10 + 50 + 5 + 5 + 30)          # no iowait, no guest
        self.assertEqual(total, busy + 700 + 100)
        self.assertIsNone(S.parse_cpu_stat("nonsense"))
        self.assertIsNone(S.parse_cpu_stat("cpu  a b c"))

    def test_meminfo_uses_available_not_free(self):
        total, avail = S.parse_meminfo(MEMINFO)
        self.assertEqual((total, avail), (8000000, 6000000))
        old = "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 50 kB\nCached: 350 kB\n"
        self.assertEqual(S.parse_meminfo(old), (1000, 500))          # pre-3.14 kernels
        self.assertIsNone(S.parse_meminfo("garbage"))

    def test_net_dev(self):
        n = S.parse_net_dev(NET_DEV.format(e0rx=1000, e0tx=500, b0rx=0, b0tx=0))
        self.assertEqual(n["eth0"], (1000, 500))
        self.assertEqual(n["lo"], (99999999, 99999999))
        self.assertIn("docker0", n)

    def test_only_real_nics_and_never_a_slave_of_a_bond(self):
        with tempfile.TemporaryDirectory() as d:
            _proc, sysfs = fake_system(d)
            names = S.parse_net_dev(NET_DEV.format(e0rx=1, e0tx=1, b0rx=0, b0tx=0)).keys()
            # no bond file: eth0 and eth1 are independent NICs, bond0 stands alone
            self.assertEqual(S.select_ifaces(names, sysfs), ["bond0", "eth0", "eth1"])
            bond = Path(sysfs) / "class" / "net" / "bond0" / "bonding"
            bond.mkdir(parents=True)
            (bond / "slaves").write_text("eth0 eth1\n")
            # now bond0 carries what its slaves carry: counting both would double it
            self.assertEqual(S.select_ifaces(names, sysfs), ["bond0"])

    def test_counter_wrap_vs_reset(self):
        self.assertEqual(S.wrap_delta(1500, 1000), 500)
        self.assertEqual(S.wrap_delta(1000, 2 ** 32 - 1000), 2000)    # a 32-bit driver wrapped
        self.assertIsNone(S.wrap_delta(10, 10 ** 12))                 # 64-bit reset: drop it


class TestRaid(unittest.TestCase):
    def one(self, text, name):
        return next(a for a in S.parse_mdstat(text) if a["name"] == name)

    def test_healthy(self):
        r = S.summarise_raid(S.parse_mdstat(MDSTAT_HEALTHY))
        self.assertEqual((r["degraded"], r["status"]), (False, "OK"))
        self.assertEqual([a["name"] for a in r["arrays"]], ["md2"])
        self.assertEqual([a["name"] for a in r["system"]], ["md1", "md0"])
        a = self.one(MDSTAT_HEALTHY, "md2")
        self.assertEqual((a["level"], a["members"], a["working"]), ("raid5", 4, 4))

    def test_degraded(self):
        r = S.summarise_raid(S.parse_mdstat(MDSTAT_DEGRADED))
        self.assertEqual((r["degraded"], r["status"]), (True, "DEGRADED"))
        a = self.one(MDSTAT_DEGRADED, "md2")
        self.assertEqual((a["members"], a["working"]), (4, 3))

    def test_rebuilding_is_still_degraded(self):
        r = S.summarise_raid(S.parse_mdstat(MDSTAT_REBUILDING))
        self.assertEqual((r["degraded"], r["status"]), (True, "REBUILDING"))
        self.assertEqual(self.one(MDSTAT_REBUILDING, "md2")["progress"], "12.6%")

    def test_a_scheduled_check_is_not_an_alarm(self):
        r = S.summarise_raid(S.parse_mdstat(MDSTAT_SYNCING))
        self.assertEqual((r["degraded"], r["status"]), (False, "SYNCING"))

    def test_pulling_a_spare_bay_drive_does_not_ring_the_phone(self):
        arrays = S.parse_mdstat(MDSTAT_SYSTEM_ONLY_DEGRADED)
        self.assertEqual(self.one(MDSTAT_SYSTEM_ONLY_DEGRADED, "md0")["status"], "DEGRADED")
        self.assertFalse(S.summarise_raid(arrays)["degraded"])
        self.assertTrue(S.summarise_raid(arrays, include_system=True)["degraded"])

    def test_inactive_array_is_failed(self):
        r = S.summarise_raid(S.parse_mdstat(MDSTAT_INACTIVE))
        self.assertEqual((r["degraded"], r["status"]), (True, "FAILED"))

    def test_raid0_has_no_counts_and_a_failed_member_is_noticed(self):
        arrays = {a["name"]: a for a in S.parse_mdstat(MDSTAT_RAID0_AND_FAILED_MEMBER)}
        self.assertEqual(arrays["md2"]["status"], "OK")
        self.assertIsNone(arrays["md2"]["members"])
        self.assertEqual(arrays["md3"]["status"], "DEGRADED")
        self.assertEqual(arrays["md3"]["failed"], 1)

    def test_unreadable_and_empty(self):
        self.assertIsNone(S.parse_mdstat(None))
        self.assertEqual(S.summarise_raid(None)["status"], "UNKNOWN")
        none = S.summarise_raid(S.parse_mdstat("Personalities :\nunused devices: <none>\n"))
        self.assertEqual((none["status"], none["degraded"]), ("NONE", False))


class TestVolumes(unittest.TestCase):
    def test_only_volumeN_is_a_volume(self):
        got = S.find_volumes(S.parse_mounts(MOUNTS))
        # not /, /tmp, docker overlays, subvolumes, snapshots, USB shares, CIFS, or "/volume 7"
        self.assertEqual(got, ["/volume1", "/volume2", "/volume10"])      # numeric order, not text

    def test_override_wins(self):
        self.assertEqual(S.find_volumes(S.parse_mounts(MOUNTS), ["/mnt/x"]), ["/mnt/x"])

    def test_octal_escapes_in_mount_paths(self):
        self.assertEqual(S.parse_mounts("/dev/x /volume\\0407 ext4 rw 0 0")[0][1], "/volume 7")

    class FakeStat:
        def __init__(self, blocks, bfree, bavail, dev):
            self.f_blocks, self.f_bfree, self.f_bavail = blocks, bfree, bavail
            self.f_frsize = self.f_bsize = 4096
            self.st_dev = dev

    def test_usage_is_df_style_and_aggregates(self):
        gib = 1024 ** 3 // 4096
        fs = {"/volume1": self.FakeStat(1000 * gib, 400 * gib, 400 * gib, 1),
              "/volume2": self.FakeStat(1000 * gib, 900 * gib, 850 * gib, 2)}   # 50 GiB reserved
        u = S.volume_usage(list(fs), statvfs=lambda p: fs[p], stat=lambda p: fs[p])
        used = 600 + 100
        self.assertAlmostEqual(u["percent"], 100 * used / (used + 400 + 850), places=3)
        self.assertEqual(u["avail"], 1250 * 1024 ** 3)
        self.assertEqual(u["total"], 2000 * 1024 ** 3)
        self.assertAlmostEqual(u["volumes"][0]["percent"], 60.0)

    def test_the_same_filesystem_twice_counts_once_and_errors_are_skipped(self):
        fs = self.FakeStat(1000, 500, 500, 7)

        def statvfs(p):
            if p == "/gone":
                raise OSError("stale")
            return fs
        u = S.volume_usage(["/volume1", "/volume1b", "/gone"], statvfs=statvfs, stat=lambda p: fs)
        self.assertEqual(len(u["volumes"]), 1)
        self.assertIsNone(S.volume_usage([], statvfs=statvfs, stat=lambda p: fs))


class TestTemperature(unittest.TestCase):
    def sensor(self, root, rel, **files):
        d = Path(root) / rel
        d.mkdir(parents=True, exist_ok=True)
        for k, v in files.items():
            (d / k).write_text(str(v))

    def test_package_sensor_beats_cores_and_acpi(self):
        with tempfile.TemporaryDirectory() as d:
            self.sensor(d, "class/hwmon/hwmon0", name="coretemp", temp1_input=52000,
                        temp1_label="Package id 0", temp2_input=49000, temp2_label="Core 0")
            self.sensor(d, "class/thermal/thermal_zone0", type="acpitz", temp=27800)
            v, src = S.read_cpu_temp(d)
            self.assertEqual(v, 52.0)
            self.assertIn("coretemp", src)

    def test_hottest_core_when_there_is_no_package(self):
        with tempfile.TemporaryDirectory() as d:
            self.sensor(d, "class/hwmon/hwmon0", name="coretemp", temp1_input=49000,
                        temp1_label="Core 0", temp2_input=55000, temp2_label="Core 1")
            self.assertEqual(S.read_cpu_temp(d)[0], 55.0)

    def test_a_drive_or_chipset_is_never_reported_as_the_cpu(self):
        with tempfile.TemporaryDirectory() as d:
            self.sensor(d, "class/hwmon/hwmon0", name="nvme", temp1_input=61000)
            self.sensor(d, "class/hwmon/hwmon1", name="drivetemp", temp1_input=38000)
            self.sensor(d, "class/thermal/thermal_zone0", type="iwlwifi_1", temp=45000)
            self.assertEqual(S.read_cpu_temp(d), (-1.0, ""))

    def test_acpi_zone_is_the_last_resort_and_garbage_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            self.sensor(d, "class/thermal/thermal_zone0", type="acpitz", temp=41000)
            self.assertEqual(S.read_cpu_temp(d)[0], 41.0)
        with tempfile.TemporaryDirectory() as d:
            self.sensor(d, "class/thermal/thermal_zone0", type="x86_pkg_temp", temp=-1)
            self.sensor(d, "class/thermal/thermal_zone1", type="cpu-thermal", temp=200000)
            self.assertEqual(S.read_cpu_temp(d)[0], -1.0)

    def test_x86_pkg_temp_zone_and_degrees_vs_millidegrees(self):
        with tempfile.TemporaryDirectory() as d:
            self.sensor(d, "class/thermal/thermal_zone0", type="x86_pkg_temp", temp=58000)
            self.assertEqual(S.read_cpu_temp(d)[0], 58.0)
        self.assertEqual(S._temp_c("47"), 47.0)
        self.assertEqual(S._temp_c("47000"), 47.0)


class TestDrives(unittest.TestCase):
    def test_sata_temperature_prefers_194_over_190(self):
        got = S.parse_smart(SMART_HDD)
        self.assertEqual((got["temp"], got["health"]), (34.0, "PASSED"))

    def test_190_alone_is_enough_and_packed_raw_values_are_unpacked(self):
        self.assertEqual(S.parse_smart(SMART_ONLY_190)["temp"], 31.0)
        self.assertEqual(S.parse_smart(SMART_HDD_PACKED)["temp"], 35.0)

    def test_nvme_and_sas(self):
        self.assertEqual(S.parse_smart(SMART_NVME)["temp"], 41.0)
        self.assertEqual(S.parse_smart(SMART_SAS), {"temp": 37.0, "health": "PASSED"})

    def test_failure_is_recognised(self):
        self.assertEqual(S.parse_smart(SMART_FAILED)["health"], "FAILED")
        self.assertEqual(S.parse_smart(SMART_SAS_BAD)["health"], "FAILED")
        self.assertIsNone(S.parse_smart("nothing useful")["health"])

    def test_standby_is_detected_from_the_message_not_the_exit_code(self):
        self.assertTrue(S.smart_standby(SMART_STANDBY))
        self.assertTrue(S.smart_standby(SMART_SLEEP))
        self.assertFalse(S.smart_standby(SMART_HDD))
        self.assertFalse(S.smart_standby("Smartctl open device: /dev/sda failed: Permission denied"))

    def test_a_sleeping_drive_is_asked_once_with_dash_n_standby_and_never_retried(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return 2, SMART_STANDBY
        r = S.probe_drive("sda", "/usr/bin/smartctl", run)
        self.assertEqual(r, {"state": "standby"})
        self.assertEqual(len(calls), 1)
        # The whole hibernation guarantee is these two arguments, adjacent and first.
        self.assertEqual(calls[0][:3], ["/usr/bin/smartctl", "-n", "standby"])

    def test_every_smartctl_call_carries_dash_n_standby(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return 1, "Please specify device type with the -d option."
        S.probe_drive("sda", "/usr/bin/smartctl", run)
        self.assertEqual(len(calls), 2)                 # auto, then -d sat
        self.assertTrue(all(c[1:3] == ["-n", "standby"] for c in calls))
        self.assertIn("sat", calls[1])

    def test_awake_drive_and_errors(self):
        r = S.probe_drive("sda", "smartctl", lambda a, t: (0, SMART_HDD))
        self.assertEqual((r["state"], r["temp"]), ("ok", 34.0))
        r = S.probe_drive("sda", "smartctl", lambda a, t: (2, "Smartctl open device: /dev/sda failed: Permission denied"))
        self.assertEqual(r["state"], "error")
        self.assertIn("root", r["error"])
        self.assertEqual(S.probe_drive("sda", "smartctl", lambda a, t: None)["state"], "error")

    def test_disk_listing_skips_usb_empty_bays_and_partitions(self):
        with tempfile.TemporaryDirectory() as d:
            for name, size in (("sda", 100), ("sdb", 0), ("sdc", 100), ("nvme0n1", 100),
                               ("md2", 100), ("loop0", 100), ("synoboot", 100)):
                p = Path(d) / "block" / name
                p.mkdir(parents=True)
                (p / "size").write_text(str(size))
            # sdc hangs off a USB bridge
            usb = Path(d) / "devices" / "pci0" / "usb1" / "host" / "sdc"
            usb.mkdir(parents=True)
            shutil.rmtree(Path(d) / "block" / "sdc")
            os.symlink(usb, Path(d) / "block" / "sdc")
            (usb / "size").write_text("100")
            self.assertEqual(S.list_disks(d), ["nvme0n1", "sda"])


class TestUps(unittest.TestCase):
    def test_nut_to_battery_fields(self):
        self.assertEqual(S.ups_reading(S.parse_upsc(UPSC)),
                         {"battery_percent": 87, "battery_charging": True,
                          "battery_ac": True, "battery_minutes": 29})
        on_batt = S.parse_upsc(UPSC.replace("OL CHRG", "OB DISCHRG"))
        r = S.ups_reading(on_batt)
        self.assertEqual((r["battery_ac"], r["battery_charging"]), (False, False))
        self.assertIsNone(S.ups_reading(S.parse_upsc("Error: Data stale")))


# ---------------------------------------------------------------- config
class TestConfig(unittest.TestCase):
    def test_defaults_have_both_switches_on_and_the_widget_feed_private(self):
        cfg, warns = S.validate({})
        self.assertEqual(warns, [])
        self.assertTrue(cfg["push"] and cfg["serve"])
        self.assertEqual(cfg["serve_allow"], ["private"])
        self.assertEqual(cfg["serve_port"], 8080)
        self.assertEqual(cfg["relay_base"], "https://peek-relay-b9a8.onrender.com")

    def test_mistakes_are_reported_by_name_and_do_not_stop_the_rest(self):
        cfg, warns = S.validate({"interval": "banana", "serve_port": 99999, "push": "off",
                                 "serve_allow": ["not-a-network"], "serve_bind": "nope",
                                 "pair_code": "BAD", "volumes": ["relative/path"], "ups": "maybe"})
        self.assertEqual(cfg["interval"], 5.0)
        self.assertEqual(cfg["serve_port"], 8080)
        self.assertFalse(cfg["push"])                         # the good one survived
        self.assertEqual(cfg["serve_allow"], ["private"])     # fails closed, not open
        self.assertGreaterEqual(len(warns), 6)

    def test_save_is_0600_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, _ = S.validate({})
            cfg.update(pair_code="K7M2P4QX9R", serve=False, volumes=["/volume1"])
            path = S.save_config(cfg, os.path.join(d, "etc"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
            back, warns = S.load_config(os.path.join(d, "etc"))
            self.assertEqual((back["pair_code"], back["serve"], back["volumes"]),
                             ("K7M2P4QX9R", False, ["/volume1"]))
            self.assertEqual(warns, [])

    def test_a_world_readable_config_is_tightened(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, _ = S.validate({})
            path = S.save_config(cfg, d)
            os.chmod(path, 0o644)
            _cfg, warns = S.load_config(d)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue(any("0600" in w for w in warns))

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "config.json").write_text("{not json")
            cfg, warns = S.load_config(d)
            self.assertTrue(cfg["push"])
            self.assertTrue(warns)

    def test_env_overrides_the_file_for_containers(self):
        cfg, _ = S.validate({"push": True})
        warns = S.apply_env(cfg, {"PEEK_PAIR": "k7m2-p4qx-9r", "PEEK_PUSH": "0",
                                  "PEEK_PORT": "9000", "PEEK_INTERVAL": "nope"})
        self.assertEqual((cfg["pair_code"], cfg["push"], cfg["serve_port"]), ("K7M2P4QX9R", False, 9000))
        self.assertEqual(len(warns), 1)

    def test_target_from_pairing_or_by_hand(self):
        cfg, _ = S.validate({"pair_code": "K7M2P4QX9R"})
        url, token, problem = S.resolve_target(cfg)
        self.assertEqual(url, "https://peek-relay-b9a8.onrender.com/ingest/4b907ba136d0a7f2")
        self.assertEqual(token, S.derive("K7M2P4QX9R")["push"])
        self.assertIsNone(problem)
        by_hand, _ = S.validate({"relay_url": "https://r.example/ingest/alice", "token": "t",
                                 "pair_code": "K7M2P4QX9R"})
        self.assertEqual(S.resolve_target(by_hand)[:2], ("https://r.example/ingest/alice", "t"))
        self.assertIn("not paired", S.resolve_target(S.validate({})[0])[2])
        self.assertIn("token", S.resolve_target(S.validate({"relay_url": "https://r/ingest/x"})[0])[2])

    def test_every_settable_name_points_at_a_real_key(self):
        for name, key in S.SETTABLE.items():
            self.assertIn(key, S.DEFAULTS, name)
            self.assertIn(key, S.PARSERS, name)


class TestAllowList(unittest.TestCase):
    def test_private_means_a_lan_a_loopback_and_a_tailnet(self):
        allow = S.parse_allow(["private"])
        for ok in ("192.168.1.20", "10.0.0.5", "172.20.1.1", "127.0.0.1", "100.101.102.103",
                   "169.254.1.1", "::1", "fd12:3456::1", "::ffff:192.168.1.5"):
            self.assertTrue(S.allowed(ok, allow), ok)
        for bad in ("8.8.8.8", "203.0.113.9", "172.32.0.1", "100.128.0.1", "2001:db8::1",
                    "::ffff:8.8.8.8", "garbage"):
            self.assertFalse(S.allowed(bad, allow), bad)

    def test_any_and_networks(self):
        self.assertTrue(S.allowed("8.8.8.8", S.parse_allow(["any"])))
        only = S.parse_allow(["192.168.1.0/24"])
        self.assertTrue(S.allowed("192.168.1.9", only))
        self.assertFalse(S.allowed("192.168.2.9", only))
        self.assertFalse(S.allowed("127.0.0.1", only))


# ----------------------------------------------------------- the collector
class TestCollector(unittest.TestCase):
    def collector(self, d, **kw):
        proc, sysfs = fake_system(d, **kw)
        t = [100.0]
        cfg, _ = S.validate({})
        c = S.Collector(cfg, proc=proc, sysfs=sysfs, clock=lambda: t[0])
        return c, proc, t

    def test_rates_from_two_samples(self):
        with tempfile.TemporaryDirectory() as d:
            c, proc, t = self.collector(d)
            c.sample_fast()                                       # primes
            self.assertFalse(c.primed.is_set())
            Path(proc, "stat").write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")    # +100 busy of +200
            Path(proc, "net", "dev").write_text(NET_DEV.format(e0rx=1000 + 2048 * 4, e0tx=500 + 1024 * 4, b0rx=0, b0tx=0))
            t[0] += 4.0
            c.sample_fast()
            self.assertTrue(c.primed.is_set())
            snap, _ = c.reading()
            self.assertAlmostEqual(snap["cpu_percent"], 50.0, places=1)
            # eth0 only: eth1 did not move, and neither lo, docker0, veth, ovs_eth0 nor eth0.100 count
            self.assertAlmostEqual(snap["net_rx_kbps"], 2.0, places=1)       # 2048*4 B / 1024 / 4 s
            self.assertAlmostEqual(snap["net_tx_kbps"], 1.0, places=1)

    def test_two_samples_microseconds_apart_do_not_invent_a_spike(self):
        with tempfile.TemporaryDirectory() as d:
            c, proc, t = self.collector(d)
            c.sample_fast()
            Path(proc, "net", "dev").write_text(NET_DEV.format(e0rx=10 ** 7, e0tx=10 ** 7, b0rx=0, b0tx=0))
            t[0] += 0.0001
            c.sample_fast()
            self.assertFalse(c.primed.is_set())
            self.assertEqual(c.reading()[0]["net_rx_kbps"], 0.0)

    def test_a_counter_reset_drops_a_sample_instead_of_spiking(self):
        with tempfile.TemporaryDirectory() as d:
            c, proc, t = self.collector(d)
            Path(proc, "net", "dev").write_text(NET_DEV.format(e0rx=10 ** 12, e0tx=10 ** 12, b0rx=0, b0tx=0))
            c.sample_fast()
            Path(proc, "net", "dev").write_text(NET_DEV.format(e0rx=5, e0tx=5, b0rx=0, b0tx=0))
            t[0] += 2.0
            c.sample_fast()
            self.assertEqual(c.reading()[0]["net_rx_kbps"], 0.0)

    def test_ram_uptime_and_raid_come_straight_from_proc(self):
        with tempfile.TemporaryDirectory() as d:
            c, _proc, _t = self.collector(d, mdstat=MDSTAT_DEGRADED)
            snap, _ = c.reading()
            self.assertAlmostEqual(snap["ram_percent"], 25.0)
            self.assertEqual(snap["uptime_seconds"], 271830)
            self.assertEqual((snap["raid_degraded"], snap["raid_status"]), (True, "DEGRADED"))

    def test_no_mdstat_is_unknown_not_an_alarm(self):
        with tempfile.TemporaryDirectory() as d:
            c, _p, _t = self.collector(d, mdstat=None)
            snap, _ = c.reading()
            self.assertEqual((snap["raid_degraded"], snap["raid_status"]), (False, "UNKNOWN"))

    def test_first_push_waits_for_the_volumes_but_not_forever(self):
        with tempfile.TemporaryDirectory() as d:
            c, _p, t = self.collector(d)
            c.sample_fast()
            t[0] += 2
            c.sample_fast()
            c.sample_slow()                                   # fake /proc has /volume1, but statvfs fails
            self.assertFalse(c.ready())                       # not mounted yet: do not report "0% full"
            t[0] += S.FIRST_PUSH_GRACE
            self.assertTrue(c.ready())

    def test_drive_temperature_is_the_hottest_fresh_one_and_goes_stale(self):
        cfg, _ = S.validate({})
        c = S.Collector(cfg)
        now = time.time()
        c._drives = {"sda": {"state": "ok", "temp": 38.0, "temp_at": now},
                     "sdb": {"state": "ok", "temp": 44.0, "temp_at": now},
                     "sdc": {"state": "standby", "temp": 51.0, "temp_at": now - 10 ** 6}}
        t, drives = c.drive_view()
        self.assertEqual(t, 44.0)                             # the long-asleep 51 has expired
        self.assertEqual(len(drives), 3)
        c._drives = {"sdc": {"state": "standby", "temp": 51.0, "temp_at": now - 10 ** 6}}
        self.assertEqual(c.drive_view()[0], -1.0)

    def test_the_drive_sampler_never_wakes_a_sleeping_drive(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return 2, SMART_STANDBY
        with tempfile.TemporaryDirectory() as d:
            for n in ("sda", "sdb"):
                (Path(d) / "block" / n).mkdir(parents=True)
                (Path(d) / "block" / n / "size").write_text("100")
            cfg, _ = S.validate({})
            c = S.Collector(cfg, sysfs=d, run=run)
            orig = S.find_exe
            S.find_exe = lambda name, extra=(): "/usr/bin/smartctl"
            try:
                c.sample_drives()
            finally:
                S.find_exe = orig
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c_[1:3] == ["-n", "standby"] for c_ in calls))
        self.assertTrue(all(v["state"] == "standby" for v in c.drive_view()[1].values()))

    def test_drive_temp_off_never_launches_anything(self):
        calls = []
        cfg, _ = S.validate({"drive_temp": False})
        c = S.Collector(cfg, run=lambda a, t: calls.append(a))
        c.sample_drives()
        self.assertEqual(calls, [])

    def test_a_failing_sampler_is_recorded_and_the_loop_survives(self):
        errors, stop = {}, threading.Event()
        n = []

        def boom():
            n.append(1)
            if len(n) >= 3:
                stop.set()
            raise RuntimeError("sensor exploded")
        S.run_loop("x", boom, lambda: 0.01, stop, lambda k, m: errors.__setitem__(k, m))
        self.assertGreaterEqual(len(n), 3)
        self.assertIn("RuntimeError", errors["x"])


# --------------------------------------------------------------- messages
class TestEpisodes(unittest.TestCase):
    COND = {"raid-md2": {"sev": "critical", "title": "RAID degraded on nas", "body": "md2 has 3 of 4",
                         "ok_title": "RAID back to normal on nas", "ok_body": "md2 is whole"}}

    def make(self, path=None):
        t = [1000.0]
        return S.Episodes("nas", 6, path, confirm_s=60, clock=lambda: t[0]), t

    def test_a_blip_is_not_an_incident(self):
        e, t = self.make()
        e.update(self.COND, t[0])
        t[0] += 30
        e.update({}, t[0])                                    # gone before it was confirmed
        t[0] += 120
        e.update({}, t[0])
        self.assertEqual(e.messages(t[0]), [])

    def test_confirmed_problem_is_one_stable_critical_message(self):
        e, t = self.make()
        e.update(self.COND, t[0])
        self.assertEqual(e.messages(t[0]), [])                # not yet
        t[0] += 61
        e.update(self.COND, t[0])
        first = e.messages(t[0])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["severity"], "critical")
        self.assertEqual(first[0]["source"], "PeekESP NAS")
        for _ in range(5):
            t[0] += 5
            e.update(self.COND, t[0])
        self.assertEqual(e.messages(t[0])[0]["id"], first[0]["id"])      # same id every reading

    def test_recovery_posts_an_all_clear_that_expires(self):
        e, t = self.make()
        e.update(self.COND, t[0])
        t[0] += 61
        e.update(self.COND, t[0])
        t[0] += 10
        e.update({}, t[0])
        self.assertEqual(len(e.messages(t[0])), 1)            # still critical: not clear for 60 s yet
        t[0] += 61
        e.update({}, t[0])
        msgs = e.messages(t[0])
        self.assertEqual([(m["severity"], m["title"]) for m in msgs],
                         [("info", "RAID back to normal on nas")])
        self.assertTrue(msgs[0]["id"].endswith("-ok"))
        t[0] += 7 * 3600
        e.update({}, t[0])
        self.assertEqual(e.messages(t[0]), [])

    def test_a_second_failure_next_month_is_a_new_alert(self):
        e, t = self.make()
        ids = []
        for _ in range(2):
            e.update(self.COND, t[0])
            t[0] += 61
            e.update(self.COND, t[0])
            ids.append(e.messages(t[0])[0]["id"])
            t[0] += 10
            e.update({}, t[0])
            t[0] += 61
            e.update({}, t[0])
            t[0] += 30 * 86400
        self.assertNotEqual(ids[0], ids[1])

    def test_a_restart_in_the_middle_of_an_incident_does_not_ring_again(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            e, t = self.make(path)
            e.update(self.COND, t[0])
            t[0] += 61
            e.update(self.COND, t[0])
            before = e.messages(t[0])[0]
            again = S.Episodes("nas", 6, path, confirm_s=60, clock=lambda: t[0] + 500)
            self.assertEqual(again.messages(t[0] + 500)[0]["id"], before["id"])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_conditions_from_raid_and_smart(self):
        arrays = S.summarise_raid(S.parse_mdstat(MDSTAT_REBUILDING))
        c = S.raid_conditions(arrays, "nas")
        self.assertEqual(list(c), ["raid-md2"])
        self.assertIn("12.6%", c["raid-md2"]["body"])
        self.assertEqual(S.raid_conditions(S.summarise_raid(S.parse_mdstat(MDSTAT_HEALTHY)), "nas"), {})
        self.assertIn("offline", S.raid_conditions(
            S.summarise_raid(S.parse_mdstat(MDSTAT_INACTIVE)), "nas")["raid-md3"]["title"])
        s = S.smart_conditions({"sda": {"health": "FAILED"}, "sdb": {"health": "PASSED"}}, "nas")
        self.assertEqual(list(s), ["smart-sda"])

    def test_messages_fit_what_the_phone_reads(self):
        e, t = self.make()
        conds = {"raid-md%d" % i: dict(self.COND["raid-md2"], title="t" * 200, body="b" * 900)
                 for i in range(2, 30)}
        e.update(conds, t[0])
        t[0] += 61
        e.update(conds, t[0])
        msgs = e.messages(t[0])
        self.assertEqual(len(msgs), 10)
        self.assertTrue(all(len(m["title"]) <= 80 and len(m["body"]) <= 400 for m in msgs))
        self.assertTrue(all(len(m["id"]) <= 64 for m in msgs))


# ---------------------------------------------------- the reading's contract
class TestContract(unittest.TestCase):
    """The keys and types PeekESP.ino's parse_one() reads. Everything is optional
    there, so a missing key is not a crash - it is a silently wrong dashboard."""

    def agent(self, d, **cfg):
        S.save_config(S.validate(dict({"volumes": []}, **cfg))[0], d)
        a = S.Agent(d, environ={})
        proc, sysfs = fake_system(d, mdstat=MDSTAT_HEALTHY)
        a.collector = S.Collector(a.cfg, proc=proc, sysfs=sysfs)
        a.collector.sample_fast()
        a.collector._usage = {"volumes": [{"path": "/volume1", "percent": 61.0},
                                          {"path": "/volume2", "percent": 20.0}],
                              "total": 10 * 1024 ** 4, "avail": 4 * 1024 ** 4, "percent": 61.0}
        a.collector._cpu_temp = (47.2, "coretemp")
        a.collector._drives = {"sda": {"state": "ok", "temp": 38.0, "temp_at": time.time(), "health": "PASSED"}}
        return a

    def test_snapshot_has_every_field_the_firmware_reads_with_the_right_type(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.agent(d, name="DiskStation").snapshot()
        numbers = ("cpu_percent", "ram_percent", "storage_percent", "storage_total_gb",
                   "storage_free_gb", "cpu_temp_c", "net_rx_kbps", "net_tx_kbps")
        for k in numbers:
            self.assertIsInstance(snap[k], float, k)
        for k in ("battery_percent", "battery_minutes", "uptime_seconds"):
            self.assertIsInstance(snap[k], int, k)
        for k in ("battery_charging", "battery_ac", "raid_degraded"):
            self.assertIsInstance(snap[k], bool, k)
        self.assertEqual(snap["host"], "DiskStation")
        self.assertEqual(snap["battery_percent"], -1)                  # no UPS: "no battery"
        self.assertEqual(snap["storage_free_gb"], 4096.0)
        self.assertEqual(snap["raid_status"], "OK")
        json.dumps(snap)                                               # and it is real JSON

    def test_host_fits_the_sketch_buffer(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.agent(d, name="x" * 19).snapshot()
        self.assertEqual(len(snap["host"]), 19)
        self.assertEqual(len(S._name("y" * 50)), 19)

    def test_extras_follow_protocol_md(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.agent(d).snapshot()
        m = snap["metrics"]
        self.assertEqual(m["drive_temp_c"], {"value": 38.0, "label": "Drive temperature", "unit": "\u00b0C"})
        self.assertEqual(m["volume1_percent"]["value"], 61.0)           # two volumes: per-volume too
        self.assertEqual(m["volume2_percent"]["max"], 100)
        self.assertLessEqual(len(m), 24)
        import re
        self.assertTrue(all(re.match(r"^[a-z0-9_]{1,32}$", k) for k in m))
        for k in m:
            self.assertNotIn(k, snap)                                    # never shadows a standard key

    def test_one_volume_gets_no_redundant_per_volume_metric(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.agent(d)
            a.collector._usage["volumes"] = a.collector._usage["volumes"][:1]
            self.assertNotIn("volume1_percent", a.snapshot().get("metrics", {}))

    def test_lan_reading_never_carries_messages_but_the_relay_one_does(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.agent(d)
            a.episodes.active["raid-md2"] = {"since": time.time(), "sev": "critical", "title": "RAID degraded",
                                             "body": "x", "ok_title": "", "ok_body": ""}
            self.assertNotIn("messages", a.snapshot_lan())
            self.assertEqual(a.snapshot()["messages"][0]["severity"], "critical")
            a.cfg["messages"] = False
            self.assertNotIn("messages", a.snapshot())

    def test_size_is_small_enough_for_the_relay_and_the_device(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.agent(d)
            self.assertLess(len(json.dumps(a.snapshot())), 1500)
            for i in range(10):
                a.episodes.active["raid-md%d" % (i + 2)] = {
                    "since": time.time(), "sev": "critical", "title": "t" * 80, "body": "b" * 400,
                    "ok_title": "", "ok_body": ""}
            self.assertLess(len(json.dumps(a.snapshot())), 8000)

    def test_healthy_nas_with_no_extras_stays_compact(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.agent(d)
            a.collector._drives = {}
            a.collector._usage["volumes"] = a.collector._usage["volumes"][:1]
            snap = a.snapshot()
            self.assertNotIn("metrics", snap)
            self.assertNotIn("messages", snap)


class TestInstanceLock(unittest.TestCase):
    def test_a_second_agent_is_refused_and_the_lock_dies_with_the_process(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "peekesp.lock")
            self.assertTrue(S.acquire_instance_lock(path))
            try:
                # A second open file description on the same file conflicts, even in-process.
                import fcntl
                fd = os.open(path, os.O_RDWR)
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.close(fd)
            finally:
                S.release_instance_lock()
            self.assertTrue(S.acquire_instance_lock(path))     # free again once released
            S.release_instance_lock()

    def test_run_exits_3_when_another_agent_holds_the_lock(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PEEK_RUN_DIR=d, PEEK_CONFIG_DIR=d)
            first = subprocess.Popen([sys.executable, str(HERE / "peek-agent-synology.py"), "--no-serve", "--no-push"],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(wait_for(lambda: os.path.exists(os.path.join(d, "peekesp.status.json")), 10))
                second = subprocess.run([sys.executable, str(HERE / "peek-agent-synology.py"), "--no-serve", "--no-push"],
                                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                self.assertEqual(second.returncode, 3)
            finally:
                first.terminate()
                first.wait(10)


class TestCliAndStatus(unittest.TestCase):
    def test_set_validates_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(S.main(["--config-dir", d, "--set", "interval=10", "--set", "serve=off",
                                     "--set", "allow=192.168.1.0/24,private"]), 0)
            cfg, _ = S.load_config(d)
            self.assertEqual((cfg["interval"], cfg["serve"]), (10.0, False))
            self.assertEqual(cfg["serve_allow"], ["192.168.1.0/24", "private"])
            self.assertEqual(S.main(["--config-dir", d, "--set", "interval=0"]), 2)
            self.assertEqual(S.main(["--config-dir", d, "--set", "bogus=1"]), 2)
            cfg, _ = S.load_config(d)
            self.assertEqual(cfg["interval"], 10.0)            # a refused change changes nothing

    def test_pair_stores_the_code_and_switches_push_on(self):
        with tempfile.TemporaryDirectory() as d:
            S.main(["--config-dir", d, "--set", "push=off"])
            self.assertEqual(S.main(["--config-dir", d, "--pair", "k7m2-p4qx-9r"]), 0)
            cfg, _ = S.load_config(d)
            self.assertEqual((cfg["pair_code"], cfg["push"]), ("K7M2P4QX9R", True))
            self.assertEqual(S.main(["--config-dir", d, "--pair", "NOPE"]), 2)

    def test_show_hides_secrets(self):
        cfg, _ = S.validate({"pair_code": "K7M2P4QX9R", "token": "abcdef1234567890"})
        shown = S.mask(cfg)
        self.assertNotIn("P4QX", json.dumps(shown))
        self.assertNotIn("1234567890", json.dumps(shown))

    def test_healthcheck_states(self):
        now = 1000.0
        good = {"updated": now, "pid": os.getpid(), "push": {"enabled": True, "state": "ok", "last_ok": now},
                "serve": {"enabled": True, "state": "listening", "port": 8080}}
        self.assertEqual(S.check_status(good, now)[0], 0)
        self.assertEqual(S.check_status(None, now)[0], 1)
        self.assertEqual(S.check_status(dict(good, updated=now - 60), now)[0], 1)
        self.assertEqual(S.check_status(dict(good, pid=2 ** 22 + 1), now)[0], 1)
        self.assertEqual(S.check_status(dict(good, push={"enabled": True, "state": "unpaired"}), now)[0], 2)
        self.assertEqual(S.check_status(dict(good, push={"enabled": True, "state": "error", "detail": "HTTP 401"}), now)[0], 1)
        self.assertEqual(S.check_status(dict(good, serve={"enabled": True, "state": "error", "detail": "in use"}), now)[0], 1)
        both_off = dict(good, push={"enabled": False}, serve={"enabled": False})
        self.assertEqual(S.check_status(both_off, now)[0], 0)
        self.assertIn("RAID", S.format_status(dict(good, started=now - 100, host="nas", version="x",
                                                   raid={"status": "OK", "arrays": []},
                                                   reading={}), now))

    def test_push_backoff(self):
        self.assertEqual(S.push_delay(5, 200, 0), 5)
        self.assertEqual(S.push_delay(5, 0, 1), 10)
        self.assertEqual(S.push_delay(5, 0, 50), 60)                    # capped
        self.assertEqual(S.push_delay(5, 401, 3), 60)                   # will not fix itself


# ------------------------------------------------------------ end to end
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeRelay:
    def __init__(self, push_token):
        self.got, self.status = [], 200
        relay = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                ok = self.headers.get("Authorization") == "Bearer " + push_token
                relay.got.append({"path": self.path, "auth_ok": ok, "ua": self.headers.get("User-Agent"),
                                  "ctype": self.headers.get("Content-Type"), "body": json.loads(body)})
                self.send_response(relay.status if ok else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_for(fn, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def get(port, path="/telemetry"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.run_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.run_dir, ignore_errors=True))
        os.environ["PEEK_RUN_DIR"] = self.run_dir
        self.addCleanup(lambda: os.environ.pop("PEEK_RUN_DIR", None))
        self.relay = FakeRelay(S.derive("K7M2P4QX9R")["push"])
        self.addCleanup(self.relay.close)
        self.port = free_port()
        self.save(push=True, serve=True)
        self.agent = S.Agent(self.dir, environ={})
        self.thread = threading.Thread(target=self.agent.run, kwargs={"install_signals": False}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def save(self, **kw):
        cfg = dict({"pair_code": "K7M2P4QX9R", "relay_base": "http://127.0.0.1:%d" % self.relay.port,
                    "interval": 1.0, "serve_port": self.port, "serve_bind": "127.0.0.1",
                    "volumes": ["/"], "drive_temp": False, "ups": "off", "name": "TestNAS"}, **kw)
        S.save_config(S.validate(cfg)[0], self.dir)

    def reload(self):
        self.agent.reload_ev.set()
        time.sleep(0.8)

    def stop(self):
        self.agent.stop_ev.set()
        self.thread.join(5)

    def test_push_widget_feed_and_both_switches(self):
        # --- the push arrives, correctly addressed and authenticated
        self.assertTrue(wait_for(lambda: self.relay.got), "no push reached the relay")
        first = self.relay.got[0]
        self.assertEqual(first["path"], "/ingest/4b907ba136d0a7f2")
        self.assertTrue(first["auth_ok"])
        self.assertEqual(first["ua"], S.USER_AGENT)
        self.assertEqual(first["ctype"], "application/json")
        self.assertEqual(first["body"]["host"], "TestNAS")
        self.assertIn("raid_degraded", first["body"])

        # --- the widget feed answers on the LAN
        status, body = get(self.port)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["host"], "TestNAS")
        self.assertEqual(get(self.port, "/nope")[0], 404)
        self.assertEqual(get(self.port, "/")[0], 200)

        # --- status file and healthcheck agree it is healthy
        self.assertTrue(wait_for(lambda: S.check_status(S.read_status())[0] == 0), "never became healthy")

        # --- relay push OFF: the pushes stop, the widget feed carries on
        self.save(push=False, serve=True)
        self.reload()
        time.sleep(1.2)
        n = len(self.relay.got)
        time.sleep(2.5)
        self.assertEqual(len(self.relay.got), n, "still pushing after push was switched off")
        self.assertEqual(get(self.port)[0], 200)
        self.assertEqual(S.read_status()["push"]["state"], "disabled")

        # --- relay push back ON
        self.save(push=True, serve=True)
        self.reload()
        self.assertTrue(wait_for(lambda: len(self.relay.got) > n), "push did not resume")

        # --- widget feed OFF: nothing is listening any more, push carries on
        self.save(push=True, serve=False)
        self.reload()
        with self.assertRaises(OSError):
            get(self.port)
        n = len(self.relay.got)
        self.assertTrue(wait_for(lambda: len(self.relay.got) > n))
        self.assertEqual(S.read_status()["serve"]["state"], "disabled")

        # --- widget feed ON again, but the LAN allow-list excludes this client: 403
        self.save(push=True, serve=True, serve_allow=["10.99.0.0/16"])
        self.reload()
        self.assertEqual(get(self.port)[0], 403)
        self.save(push=True, serve=True, serve_allow=["private"])
        self.reload()
        self.assertEqual(get(self.port)[0], 200)

    def test_a_rejected_token_is_reported_and_does_not_stop_the_widget_feed(self):
        self.save(pair_code="ABCDEFGHJK")                    # a different code: the relay will 401
        self.reload()
        self.assertTrue(wait_for(lambda: S.read_status()["push"]["state"] == "error"))
        self.assertIn("401", S.read_status()["push"]["detail"])
        self.assertEqual(S.check_status(S.read_status())[0], 1)
        self.assertEqual(get(self.port)[0], 200)

    def test_unpaired_still_serves_and_says_so(self):
        self.save(pair_code="")
        self.reload()
        self.assertTrue(wait_for(lambda: S.read_status()["push"]["state"] == "unpaired"))
        self.assertEqual(S.check_status(S.read_status())[0], 2)
        self.assertEqual(get(self.port)[0], 200)

    def test_a_busy_port_is_reported_and_retried_and_does_not_block_the_push(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        self.addCleanup(s.close)
        self.save(serve_port=busy)
        self.reload()
        self.assertTrue(wait_for(lambda: S.read_status()["serve"]["state"] == "error"))
        n = len(self.relay.got)
        self.assertTrue(wait_for(lambda: len(self.relay.got) > n), "push stalled behind a busy port")
        self.assertEqual(S.check_status(S.read_status())[0], 1)

    def test_hand_edits_are_noticed_without_a_signal(self):
        # The file is the interface: edit it, and the agent follows within a few seconds.
        self.assertTrue(wait_for(lambda: S.read_status() is not None))
        self.save(push=False)
        self.assertTrue(wait_for(lambda: (S.read_status() or {}).get("push", {}).get("state") == "disabled",
                                 S.CONFIG_POLL_SECONDS + 4))

    def test_the_status_file_is_world_readable_and_holds_no_secret(self):
        self.assertTrue(wait_for(lambda: S.read_status() is not None))
        path = S.status_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        text = Path(path).read_text()
        self.assertNotIn("K7M2P4QX9R", text)
        self.assertNotIn(S.derive("K7M2P4QX9R")["push"], text)

    def test_clean_shutdown_removes_the_status_file(self):
        self.assertTrue(wait_for(lambda: S.read_status() is not None))
        self.agent.stop_ev.set()
        self.thread.join(5)
        self.assertIsNone(S.read_status())


if __name__ == "__main__":
    unittest.main()
