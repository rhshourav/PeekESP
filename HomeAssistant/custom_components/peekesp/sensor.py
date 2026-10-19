"""Sensors: one set per machine, one 'Screen' summary the card reads, relay health."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import MATCH_ALL

from .api import Machine
from .const import DOMAIN
from .coordinator import PeekCoordinator
from .entity import PeekMachineEntity, is_online


@dataclass(frozen=True, kw_only=True)
class PeekSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Machine], StateType]


SENSORS: tuple[PeekSensorDescription, ...] = (
    PeekSensorDescription(
        key="cpu", name="CPU",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, icon="mdi:cpu-64-bit",
        value_fn=lambda m: m.cpu_percent,
    ),
    PeekSensorDescription(
        key="ram", name="RAM",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, icon="mdi:memory",
        value_fn=lambda m: m.ram_percent,
    ),
    PeekSensorDescription(
        key="storage", name="Storage used",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, icon="mdi:harddisk",
        value_fn=lambda m: m.storage_percent,
    ),
    PeekSensorDescription(
        key="storage_free", name="Storage free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
        value_fn=lambda m: m.storage_free_gb,
    ),
    PeekSensorDescription(
        key="storage_total", name="Storage total",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        entity_category=EntityCategory.DIAGNOSTIC, suggested_display_precision=1,
        value_fn=lambda m: m.storage_total_gb,
    ),
    PeekSensorDescription(
        key="temperature", name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
        value_fn=lambda m: m.cpu_temp_c,
    ),
    # Unit is taken from the field name (net_rx_kbps). No device_class on purpose:
    # that would let HA convert between units, and if the agent actually reports
    # KB/s the conversion would be silently wrong by a factor of eight.
    PeekSensorDescription(
        key="net_down", name="Download",
        native_unit_of_measurement="kbps", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0, icon="mdi:download-network",
        value_fn=lambda m: m.rx_kbps,
    ),
    PeekSensorDescription(
        key="net_up", name="Upload",
        native_unit_of_measurement="kbps", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0, icon="mdi:upload-network",
        value_fn=lambda m: m.tx_kbps,
    ),
    PeekSensorDescription(
        key="uptime", name="Uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.DAYS, suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.uptime_seconds,
    ),
    PeekSensorDescription(
        key="battery", name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.battery_percent,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PeekCoordinator = entry.runtime_data
    async_add_entities([PeekRelayLatency(coordinator), PeekLatestFirmware(coordinator)])

    known: set[str] = set()

    @callback
    def _discover() -> None:
        new: list[SensorEntity] = []
        for host, m in coordinator.data.machines.items():
            if f"{host}_screen" not in known:
                known.add(f"{host}_screen")
                new.append(PeekScreen(coordinator, host))
            for desc in SENSORS:
                uid = f"{host}_{desc.key}"
                # Create a sensor only once the machine has reported that field:
                # a desktop never grows a battery sensor it can't fill.
                if uid not in known and desc.value_fn(m) is not None:
                    known.add(uid)
                    new.append(PeekSensor(coordinator, host, desc))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class PeekSensor(PeekMachineEntity, SensorEntity):
    entity_description: PeekSensorDescription

    def __init__(self, coordinator: PeekCoordinator, host: str, desc: PeekSensorDescription):
        super().__init__(coordinator, host, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> StateType:
        m = self.machine
        return self.entity_description.value_fn(m) if m else None


class PeekScreen(PeekMachineEntity, SensorEntity):
    """Everything the screen shows, in one entity, for the Lovelace card.

    The card could look up ten entities by name; one entity with a full
    snapshot is a single subscription and no naming convention to break.
    Attributes are kept out of the recorder - the individual sensors above are
    what carry history.
    """

    _attr_name = "Screen"
    _attr_icon = "mdi:monitor-dashboard"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["online", "offline"]
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, coordinator: PeekCoordinator, host: str) -> None:
        super().__init__(coordinator, host, "screen")

    @property
    def native_value(self) -> str | None:
        m = self.machine
        return None if m is None else ("online" if is_online(self.coordinator, m) else "offline")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m, d = self.machine, self.coordinator.data
        if m is None:
            return {}
        return {
            "peekesp": True,  # lets the card's entity picker find these
            "host": m.host,
            "cpu_percent": m.cpu_percent,
            "ram_percent": m.ram_percent,
            "storage_percent": m.storage_percent,
            "storage_total_gb": m.storage_total_gb,
            "storage_free_gb": m.storage_free_gb,
            "cpu_temp_c": m.cpu_temp_c,
            "rx_kbps": m.rx_kbps,
            "tx_kbps": m.tx_kbps,
            "uptime_seconds": m.uptime_seconds,
            "age_s": m.age_s,
            "offline_after": self.coordinator.offline_after,
            "latency_ms": d.latency_ms,
            "fetched_at": d.fetched_at,
        }


class _PeekRelayEntity(CoordinatorEntity[PeekCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PeekCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_relay_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})


class PeekRelayLatency(_PeekRelayEntity):
    _attr_name = "Relay latency"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PeekCoordinator) -> None:
        super().__init__(coordinator, "latency")

    @property
    def native_value(self) -> int:
        return self.coordinator.data.latency_ms


class PeekLatestFirmware(_PeekRelayEntity):
    _attr_name = "Latest firmware"
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: PeekCoordinator) -> None:
        super().__init__(coordinator, "latest_fw")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.latest_fw
