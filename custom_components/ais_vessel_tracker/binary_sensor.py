"""Binary sensor platform for AIS Vessel Tracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AisVesselTrackerConfigEntry
from .entity import AisVesselTrackerEntity, remove_legacy_entities
from .sources import SOURCE_AISHUB, SOURCE_AISSTREAM, SOURCE_LOCAL_MQTT, source_label
from .tracker import AisTrackerCoordinator

_SOURCE_ENABLED = {
    SOURCE_AISSTREAM: "aisstream_enabled",
    SOURCE_LOCAL_MQTT: "local_mqtt_enabled",
    SOURCE_AISHUB: "aishub_enabled",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AisVesselTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one connectivity binary sensor per configured AIS source."""
    tracker = entry.runtime_data.tracker
    remove_legacy_entities(hass, entry, {"ais_connection_status"})
    async_add_entities(
        SourceConnectionSensor(entry, tracker, source)
        for source, attribute in _SOURCE_ENABLED.items()
        if getattr(tracker, attribute)
    )


class SourceConnectionSensor(AisVesselTrackerEntity, BinarySensorEntity):
    """Report whether one configured AIS source is currently connected."""

    _attr_has_entity_name = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: AisVesselTrackerConfigEntry,
        tracker: AisTrackerCoordinator,
        source: str,
    ) -> None:
        """Initialize the source connection sensor."""
        super().__init__(entry)
        self.coordinator = tracker
        self.source = source
        label = source_label(source) or source
        self._attr_name = f"{label} Connection"
        self._attr_unique_id = f"source_connection_{source}"
        self._attr_suggested_object_id = f"ais_source_{source}"

    @property
    def is_on(self) -> bool:
        """Return whether the source is currently connected."""
        return self.coordinator.source_status.get(self.source) == "Connected"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw status, last error, and source-specific debug data."""
        attributes: dict[str, Any] = {
            "status": self.coordinator.source_status.get(self.source),
            "error": self.coordinator.source_errors.get(self.source),
            "last_message": self.coordinator.source_last_message.get(self.source),
            "vessel_count": sum(
                1
                for vessel in self.coordinator.vessels.values()
                if vessel.get("source") == self.source
            ),
        }
        if self.source == SOURCE_LOCAL_MQTT:
            attributes["topic"] = self.coordinator.local_mqtt_topic
        elif self.source == SOURCE_AISHUB:
            attributes["poll_interval_seconds"] = self.coordinator.aishub_poll_interval_seconds
        elif self.source == SOURCE_AISSTREAM and self.coordinator.vessel_watchlist:
            attributes["vessel_watchlist"] = self.coordinator.vessel_watchlist
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to tracker updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
