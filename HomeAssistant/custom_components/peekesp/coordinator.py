"""One poll of the relay, shared by every entity."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PeekAuthError, PeekConnectionError, PeekRelayClient, RelayData
from .const import (
    CONF_OFFLINE_AFTER,
    CONF_SCAN_INTERVAL,
    DEFAULT_OFFLINE_AFTER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class PeekCoordinator(DataUpdateCoordinator[RelayData]):
    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: PeekRelayClient
    ) -> None:
        interval = max(
            MIN_SCAN_INTERVAL, int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self._client = client

    @property
    def offline_after(self) -> int:
        return int(self.config_entry.options.get(CONF_OFFLINE_AFTER, DEFAULT_OFFLINE_AFTER))

    async def _async_update_data(self) -> RelayData:
        try:
            return await self._client.fetch()
        except PeekAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PeekConnectionError as err:
            raise UpdateFailed(f"Cannot reach the relay: {err}") from err
