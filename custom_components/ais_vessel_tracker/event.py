"""Event platform for AIS Vessel Tracker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AisVesselTrackerConfigEntry
from .areas import area_id, area_name, area_slug, configured_areas
from .entity import (
    AisVesselTrackerEntity,
    marine_traffic_url,
    remove_legacy_entities,
    vessel_finder_url,
)

_LOGGER = logging.getLogger(__name__)
_PHOTO_LOOKUP_TIMEOUT = 45


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AisVesselTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one last-vessel-updated event entity per area."""
    remove_legacy_entities(hass, entry, {"last_vessel_updated"})
    tracker = entry.runtime_data.tracker
    async_add_entities(
        LastVesselUpdatedEvent(entry, area, index)
        for index, area in enumerate(configured_areas(tracker.settings), 1)
    )


class LastVesselUpdatedEvent(AisVesselTrackerEntity, EventEntity):
    """Fire when the integration records a new last passing vessel."""

    _attr_event_types = ["vessel_updated"]
    _attr_icon = "mdi:ferry"
    _attr_translation_key = "last_vessel_updated"

    def __init__(
        self, entry: AisVesselTrackerConfigEntry, area: dict[str, Any], index: int
    ) -> None:
        """Initialize the event entity."""
        runtime = entry.runtime_data
        assert runtime is not None
        self.coordinator = runtime.tracker
        self.area_id = area_id(area, index)
        self.area_name = area_name(area, index)
        self._attr_name = f"{self.area_name} Last Vessel Updated"
        self._attr_unique_id = f"last_vessel_updated_{self.area_id}"
        self._attr_suggested_object_id = f"{area_slug(area, index)}_last_vessel_updated"
        super().__init__(entry)
        self.entry = entry
        self._last_mmsi: str | None = None
        self._event_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Subscribe to the tracked vessel entity."""
        await super().async_added_to_hass()
        last_vessel = self.coordinator.last_vessels.get(self.area_id)
        if last_vessel is not None:
            self._last_mmsi = self._mmsi(last_vessel)
        self.async_on_remove(self.coordinator.async_add_listener(self._tracker_updated))

    @callback
    def _tracker_updated(self) -> None:
        """Trigger for a newly recorded vessel."""
        vessel = self.coordinator.last_vessels.get(self.area_id)
        if vessel is None:
            return
        mmsi = self._mmsi(vessel)
        if not mmsi or mmsi == self._last_mmsi:
            return
        self._last_mmsi = mmsi
        self.entry.async_create_background_task(
            self.hass,
            self._async_trigger_event(dict(vessel), mmsi),
            "ais_vessel_tracker_vessel_updated_event",
        )

    async def _async_trigger_event(self, vessel: dict[str, Any], mmsi: str) -> None:
        """Emit the event after the matching photo lookup has finished."""
        async with self._event_lock:
            photo = self.entry.runtime_data.photos.get(self.area_id)
            if photo is not None:
                try:
                    await asyncio.wait_for(
                        photo.async_refresh(force=True, vessel_override=vessel),
                        timeout=_PHOTO_LOOKUP_TIMEOUT,
                    )
                except TimeoutError:
                    _LOGGER.warning(
                        "Timed out waiting for the photo lookup for MMSI %s",
                        mmsi,
                    )
                except Exception:  # noqa: BLE001
                    # The photo lookup is a best-effort enhancement; firing
                    # this event is the entity's actual job and must not be
                    # skipped just because that lookup broke.
                    _LOGGER.exception(
                        "Photo lookup failed for MMSI %s while updating the "
                        "last-passing-vessel event",
                        mmsi,
                    )
            marine_vessel_id = vessel.get("marine_traffic_vessel_id")
            if photo is not None:
                marine_vessel_id = photo.marine_traffic_vessel_id or marine_vessel_id
            self._trigger_event(
                "vessel_updated",
                {
                    "vessel_name": vessel.get("vessel_name"),
                    "mmsi": mmsi,
                    "latitude": vessel.get("latitude"),
                    "longitude": vessel.get("longitude"),
                    "speed_knots": vessel.get("speed_knots"),
                    "course": vessel.get("course"),
                    "heading": vessel.get("heading"),
                    "navigational_status": vessel.get("navigational_status"),
                    "vessel_class": vessel.get("vessel_class"),
                    "destination": vessel.get("destination"),
                    "eta": vessel.get("eta"),
                    "vessel_type": vessel.get("vessel_type"),
                    "spotted_time": vessel.get("spotted_time"),
                    "area_id": self.area_id,
                    "area_name": self.area_name,
                    "source": vessel.get("source"),
                    "sources_seen": vessel.get("sources_seen", []),
                    "vessel_finder_url": vessel_finder_url(mmsi),
                    "marine_traffic_vessel_id": marine_vessel_id,
                    "marinetraffic_url": marine_traffic_url(marine_vessel_id, mmsi),
                },
            )

    @staticmethod
    def _mmsi(attributes: dict[str, Any]) -> str | None:
        """Return a normalized MMSI from sensor attributes."""
        mmsi = attributes.get("mmsi")
        return str(mmsi) if mmsi else None
