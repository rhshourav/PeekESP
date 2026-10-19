"""Binary sensors: is the machine reporting, is it charging."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PeekCoordinator
from .entity import PeekMachineEntity, is_online


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PeekCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover() -> None:
        new: list[BinarySensorEntity] = []
        for host, m in coordinator.data.machines.items():
            if f"{host}_online" not in known:
                known.add(f"{host}_online")
                new.append(PeekOnline(coordinator, host))
            if f"{host}_charging" not in known and m.battery_charging is not None:
                known.add(f"{host}_charging")
                new.append(PeekCharging(coordinator, host))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class PeekOnline(PeekMachineEntity, BinarySensorEntity):
    _attr_name = "Reporting"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: PeekCoordinator, host: str) -> None:
        super().__init__(coordinator, host, "online")

    @property
    def is_on(self) -> bool | None:
        m = self.machine
        return None if m is None else is_online(self.coordinator, m)


class PeekCharging(PeekMachineEntity, BinarySensorEntity):
    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: PeekCoordinator, host: str) -> None:
        super().__init__(coordinator, host, "charging")

    @property
    def is_on(self) -> bool | None:
        m = self.machine
        return None if m is None else m.battery_charging
