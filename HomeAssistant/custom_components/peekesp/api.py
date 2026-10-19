"""Client for the PeekESP Cloudflare relay.

No Home Assistant imports on purpose, so this can be tested on its own.

The wire format is taken from parse_payload()/parse_one() in PeekESP.ino:
either {"devices": [ {...}, ... ]} or, from an older relay, one flat object.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

# Cloudflare's edge rejects some generic client user agents with error 1010
# before the Worker runs, which looks like an opaque 403. The firmware sends a
# custom one for the same reason.
USER_AGENT = "PeekESP-HA/0.1"
MAX_DEVICES = 6  # the relay's own cap, mirrored from the firmware


class PeekError(Exception):
    """Base class."""


class PeekConnectionError(PeekError):
    """Network failure, timeout, or a non-auth HTTP error."""


class PeekAuthError(PeekError):
    """The relay rejected the read token (401/403/404)."""


@dataclass(slots=True)
class Machine:
    host: str
    cpu_percent: float | None = None
    ram_percent: float | None = None
    storage_percent: float | None = None
    storage_total_gb: float | None = None
    storage_free_gb: float | None = None
    cpu_temp_c: float | None = None
    battery_percent: int | None = None
    battery_minutes: int | None = None
    battery_charging: bool | None = None
    battery_ac: bool | None = None
    rx_kbps: float | None = None
    tx_kbps: float | None = None
    uptime_seconds: int | None = None
    age_s: int = 0  # seconds since the host last pushed, as the relay reports it


@dataclass(slots=True)
class RelayData:
    machines: dict[str, Machine] = field(default_factory=dict)
    latest_fw: str | None = None
    latency_ms: int = 0
    fetched_at: float = 0.0  # time.time() of the successful fetch


def _num(o: dict[str, Any], key: str, *, positive_only: bool = False) -> float | None:
    """Missing/invalid -> None. The firmware uses sentinels (-1, 0); we use None."""
    v = o.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if positive_only and v <= 0:
        return None
    return float(v)


def _int(o: dict[str, Any], key: str) -> int | None:
    v = _num(o, key)
    if v is None or v < 0:  # firmware: <0 means "not reported"
        return None
    return int(v)


def parse_machine(o: dict[str, Any]) -> Machine:
    host = o.get("host")
    return Machine(
        host=host if isinstance(host, str) and host else "dietpi",  # firmware default
        cpu_percent=_num(o, "cpu_percent"),
        ram_percent=_num(o, "ram_percent"),
        storage_percent=_num(o, "storage_percent"),
        # An older agent omits capacity. Firmware treats 0 as "unknown" rather
        # than "full disk", and so do we.
        storage_total_gb=_num(o, "storage_total_gb", positive_only=True),
        storage_free_gb=_num(o, "storage_free_gb")
        if _num(o, "storage_total_gb", positive_only=True)
        else None,
        cpu_temp_c=_num(o, "cpu_temp_c") if (_num(o, "cpu_temp_c") or 0) >= 0 else None,
        battery_percent=_int(o, "battery_percent"),
        battery_minutes=_int(o, "battery_minutes"),
        battery_charging=o.get("battery_charging")
        if isinstance(o.get("battery_charging"), bool)
        else None,
        battery_ac=o.get("battery_ac") if isinstance(o.get("battery_ac"), bool) else None,
        rx_kbps=_num(o, "net_rx_kbps"),
        tx_kbps=_num(o, "net_tx_kbps"),
        uptime_seconds=_int(o, "uptime_seconds"),
        age_s=_int(o, "age_s") or 0,
    )


def parse_payload(doc: Any) -> tuple[dict[str, Machine], str | None]:
    if not isinstance(doc, dict):
        raise PeekConnectionError("relay returned something that is not a JSON object")

    machines: dict[str, Machine] = {}
    devices = doc.get("devices")
    if isinstance(devices, list):
        for o in devices[:MAX_DEVICES]:
            if isinstance(o, dict):
                m = parse_machine(o)
                machines.setdefault(m.host, m)

    # Older relay / agent polled directly: one flat object.
    if not machines and ("cpu_percent" in doc or "host" in doc):
        m = parse_machine(doc)
        machines[m.host] = m

    fw = doc.get("latest_fw")
    return machines, fw if isinstance(fw, str) and fw else None


class PeekRelayClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url
        self._token = token

    async def fetch(self) -> RelayData:
        t0 = time.monotonic()
        try:
            async with self._session.get(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (401, 403, 404):
                    raise PeekAuthError(f"relay answered {resp.status}")
                if resp.status == 503:
                    # Worker is up, the host has not pushed anything yet. That
                    # is a valid state, not an outage - the device treats it
                    # the same way (see fetch_relay in the firmware).
                    return RelayData(fetched_at=time.time(), latency_ms=_ms(t0))
                if resp.status != 200:
                    raise PeekConnectionError(f"relay answered {resp.status}")
                doc = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise PeekConnectionError(str(err) or type(err).__name__) from err

        machines, fw = parse_payload(doc)
        return RelayData(
            machines=machines,
            latest_fw=fw,
            latency_ms=_ms(t0),
            fetched_at=time.time(),
        )


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
