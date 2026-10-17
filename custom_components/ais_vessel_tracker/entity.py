"""Shared entity helpers for AIS Vessel Tracker."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_SEARXNG_URL, DOMAIN


def vessel_finder_url(mmsi: object) -> str | None:
    """Return the VesselFinder details URL for an MMSI."""
    value = str(mmsi).strip() if mmsi is not None else ""
    return f"https://www.vesselfinder.com/vessels/details/{value}" if value else None


def marine_traffic_url(vessel_id: object, mmsi: object = None) -> str | None:
    """Return the MarineTraffic details URL for a vessel.

    Prefers the internal shipid (resolved via a search lookup) since that is
    MarineTraffic's canonical link, but falls back to their MMSI-based deep
    link so the attribute is still populated before that lookup succeeds.
    """
    value = str(vessel_id).strip() if vessel_id is not None else ""
    if value.isdigit():
        return f"https://www.marinetraffic.com/en/ais/details/ships/shipid:{value}"
    mmsi_value = str(mmsi).strip() if mmsi is not None else ""
    return (
        f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi_value}"
        if mmsi_value.isdigit()
        else None
    )


def remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry, unique_ids: set[str]) -> None:
    """Remove entity-registry entries from the former global model."""
    registry = er.async_get(hass)
    for registry_entry in registry.entities.get_entries_for_config_entry_id(entry.entry_id):
        if registry_entry.unique_id in unique_ids:
            registry.async_remove(registry_entry.entity_id)


class AisVesselTrackerEntity(Entity):
    """Base entity for the AIS Vessel Tracker service device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
    ) -> None:
        """Initialize an AIS Vessel Tracker entity."""
        super().__init__()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="AIS Vessel Tracker",
            model="AIS Vessel Tracker",
            configuration_url=(
                entry.options.get(CONF_SEARXNG_URL) or entry.data.get(CONF_SEARXNG_URL)
            ),
        )
