"""Shared base for per-machine entities."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Machine
from .const import DOMAIN
from .coordinator import PeekCoordinator


def is_online(coordinator: PeekCoordinator, m: Machine) -> bool:
    """A machine is online if the relay heard from it recently.

    This asks about the machine, not the link: the relay can be perfectly
    healthy while one laptop has been shut for the night. (If the link itself
    is down the coordinator fails and every entity goes unavailable instead.)
    """
    return m.age_s <= coordinator.offline_after


class PeekMachineEntity(CoordinatorEntity[PeekCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PeekCoordinator, host: str, key: str) -> None:
        super().__init__(coordinator)
        self._host = host
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{host}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{host}")},
            name=host,
            manufacturer="PeekESP",
            model="Monitored machine",
            via_device=(DOMAIN, entry_id),
        )

    @property
    def machine(self) -> Machine | None:
        return self.coordinator.data.machines.get(self._host)

    @property
    def available(self) -> bool:
        return super().available and self.machine is not None
