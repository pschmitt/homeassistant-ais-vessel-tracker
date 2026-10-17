"""Sensor platform for AIS Vessel Tracker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change

from . import AisVesselTrackerConfigEntry
from .areas import area_id, area_name, area_slug, configured_areas
from .coordinator import VesselPhotoCoordinator
from .entity import (
    AisVesselTrackerEntity,
    marine_traffic_url,
    remove_legacy_entities,
    vessel_finder_url,
)
from .sources import source_label
from .tracker import AisTrackerCoordinator

_LOGGER = logging.getLogger(__name__)


def _add_source_attributes(attributes: dict[str, Any]) -> None:
    """Add readable source metadata without changing normalized source IDs."""
    if source := source_label(attributes.get("source")):
        attributes["source_name"] = source
    sources_seen = attributes.get("sources_seen")
    if isinstance(sources_seen, list):
        labels = [label for item in sources_seen if (label := source_label(item))]
        if labels:
            attributes["sources_seen_names"] = labels


def _trigger_map_photo_lookup(
    hass: HomeAssistant,
    entry: AisVesselTrackerConfigEntry,
    mmsi: str,
    vessel: dict[str, Any],
) -> None:
    """Kick off a one-shot background photo lookup for a new map vessel.

    Map vessels otherwise never get a photo: VesselPhotoCoordinator.async_refresh
    is normally only triggered for whichever vessel becomes an area's
    last-passing-vessel. Any one area's coordinator is enough since map sensors
    check every area's cache for a match (see AisMapVesselSensor._photo).
    """
    photos = tuple(entry.runtime_data.photos.values())
    if not photos:
        return
    photo = photos[0]
    if photo.photo_for_mmsi(mmsi) is not None:
        return
    entry.async_create_background_task(
        hass,
        photo.async_refresh(vessel_override=vessel),
        f"ais_vessel_tracker_map_photo_{mmsi}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AisVesselTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the persistent and optional map sensors."""
    tracker = entry.runtime_data.tracker
    areas = configured_areas(tracker.settings)
    remove_legacy_entities(hass, entry, {"last_passing_vessel", "ais_connection_status"})
    remove_legacy_entities(
        hass,
        entry,
        {
            f"last_passing_vessel_photo_{area_id(area, index)}"
            for index, area in enumerate(areas, 1)
        },
    )
    photos = tuple(entry.runtime_data.photos.values())
    entities: list[SensorEntity] = []
    entities.extend(
        LastPassingVesselSensor(entry, area, index, photos) for index, area in enumerate(areas, 1)
    )
    count_sensors = [
        VesselCountSensor(entry, area, index, period="day") for index, area in enumerate(areas, 1)
    ]
    count_sensors.extend(
        VesselCountSensor(entry, area, index, period="hour") for index, area in enumerate(areas, 1)
    )
    entities.extend(count_sensors)
    known: dict[str, AisMapVesselSensor] = {}
    if tracker.map_entities_enabled:
        for mmsi, vessel in tracker.vessels.items():
            known[mmsi] = AisMapVesselSensor(
                entry, tracker, mmsi, vessel, tuple(entry.runtime_data.photos.values())
            )
            _trigger_map_photo_lookup(hass, entry, mmsi, vessel)
        entities.extend(known.values())
    async_add_entities(entities)

    @callback
    def counter_time_changed(_now: Any) -> None:
        """Refresh the rolling counters as their windows move."""
        for entity in count_sensors:
            entity.async_write_ha_state()

    entry.async_on_unload(async_track_time_change(hass, counter_time_changed, second=0))
    entry.async_create_background_task(
        hass,
        _async_remove_orphaned_map_entities(hass, entry, set(known)),
        "ais_vessel_tracker_cleanup_map_entities",
    )

    @callback
    def tracker_updated() -> None:
        """Add newly observed vessels and remove expired sensors."""
        if not tracker.map_entities_enabled:
            return
        for mmsi in set(known) - set(tracker.vessels):
            entity = known.pop(mmsi)
            entry.async_create_background_task(
                hass,
                _async_remove_map_entity(hass, entity),
                "ais_vessel_tracker_remove_map_entity",
            )
        new_entities = []
        for mmsi, vessel in tracker.vessels.items():
            if mmsi not in known:
                known[mmsi] = AisMapVesselSensor(
                    entry,
                    tracker,
                    mmsi,
                    vessel,
                    tuple(entry.runtime_data.photos.values()),
                )
                new_entities.append(known[mmsi])
                _trigger_map_photo_lookup(hass, entry, mmsi, vessel)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(tracker.async_add_listener(tracker_updated))


async def _async_remove_orphaned_map_entities(
    hass: HomeAssistant,
    entry: AisVesselTrackerConfigEntry,
    active_mmsis: set[str],
) -> None:
    """Remove dynamic vessel entities left by a previous runtime."""
    await asyncio.sleep(0)
    registry = er.async_get(hass)
    removed = 0
    for registry_entry in list(registry.entities.get_entries_for_config_entry_id(entry.entry_id)):
        if (
            registry_entry.platform == entry.domain
            and (registry_entry.unique_id or "").startswith("ais_vessel_")
            and (registry_entry.unique_id or "").removeprefix("ais_vessel_") not in active_mmsis
        ):
            registry.async_remove(registry_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.info("Removed %d stale AIS vessel entities", removed)


async def _async_remove_map_entity(hass: HomeAssistant, entity: AisMapVesselSensor) -> None:
    """Remove an expired vessel entity from state and the entity registry."""
    entity_id = entity.entity_id
    await entity.async_remove(force_remove=True)
    if entity_id:
        er.async_get(hass).async_remove(entity_id)


class LastPassingVesselSensor(AisVesselTrackerEntity, SensorEntity):
    """Expose the last vessel detected by a configured AIS source."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:ferry"
    _attr_name = "Last Passing Vessel"

    def __init__(
        self,
        entry: AisVesselTrackerConfigEntry,
        area: dict[str, Any],
        index: int,
        photos: tuple[VesselPhotoCoordinator, ...],
    ) -> None:
        """Initialize the last vessel sensor."""
        super().__init__(entry)
        self.area_id = area_id(area, index)
        self._attr_name = f"{area_name(area, index)} Last Passing Vessel"
        self._attr_unique_id = f"last_passing_vessel_{self.area_id}"
        self._attr_suggested_object_id = f"{area_slug(area, index)}_last_passing_vessel"
        self.coordinator = entry.runtime_data.tracker
        self._photos = photos

    def _photo_coordinator(self) -> VesselPhotoCoordinator | None:
        """Return the photo coordinator holding the current vessel image."""
        mmsi = self.coordinator.last_vessels.get(self.area_id, {}).get("mmsi")
        for photo in self._photos:
            if photo.image_for_mmsi(mmsi) is not None:
                return photo
        return None

    @property
    def entity_picture(self) -> str | None:
        """Return the HA URL for the current vessel photo."""
        if self._photo_coordinator() is None:
            return None
        mmsi = self.coordinator.last_vessels.get(self.area_id, {}).get("mmsi")
        return f"/api/ais_vessel_tracker/photo/{self.coordinator.entry_id}/{mmsi}"

    @property
    def available(self) -> bool:
        """Return whether a vessel has been detected or restored."""
        return self.area_id in self.coordinator.last_vessels

    @property
    def native_value(self) -> str | None:
        """Return the latest vessel name."""
        return self.coordinator.last_vessels.get(self.area_id, {}).get("vessel_name")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the latest vessel details."""
        attributes = dict(self.coordinator.last_vessels.get(self.area_id, {}))
        _add_source_attributes(attributes)
        url = vessel_finder_url(attributes.get("mmsi"))
        if url:
            attributes["vessel_finder_url"] = url
        url = marine_traffic_url(attributes.get("marine_traffic_vessel_id"), attributes.get("mmsi"))
        if url:
            attributes["marinetraffic_url"] = url
        photo_coordinator = self._photo_coordinator()
        if photo_coordinator is not None:
            mmsi = str(attributes.get("mmsi") or "")
            attributes["picture_url"] = self.entity_picture
            if photo := photo_coordinator.photo_for_mmsi(mmsi):
                attributes["photo_source_url"] = photo.get("photo_url")
                attributes["photo_origin"] = photo.get("provider")
                attributes["photo_author"] = photo.get("photo_author")
                attributes["photo_credit_url"] = photo.get("photo_credit_url")
                attributes["photo_last_updated"] = photo.get("last_updated")
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to tracker updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
        for photo in self._photos:
            self.async_on_remove(photo.async_add_listener(self.async_write_ha_state))


class VesselCountSensor(AisVesselTrackerEntity, SensorEntity):
    """Count distinct vessels detected during a local calendar period."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:ferry"
    _attr_native_unit_of_measurement = "vessels"

    def __init__(
        self,
        entry: AisVesselTrackerConfigEntry,
        area: dict[str, Any],
        index: int,
        *,
        period: str,
    ) -> None:
        """Initialize a vessel counter."""
        super().__init__(entry)
        self.coordinator = entry.runtime_data.tracker
        self.area_id = area_id(area, index)
        self.period = period
        suffix = "vessels_today" if period == "day" else "vessels_this_hour"
        self._attr_translation_key = suffix
        self._attr_name = (
            f"{area_name(area, index)} Vessels Today"
            if period == "day"
            else f"{area_name(area, index)} Vessels in Last Hour"
        )
        self._attr_unique_id = f"{suffix}_{self.area_id}"
        self._attr_suggested_object_id = f"{area_slug(area, index)}_{suffix}"

    @property
    def native_value(self) -> int:
        """Return the number of distinct vessels in the current period."""
        return self.coordinator.count_vessel_sightings(self.area_id, period=self.period)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the distinct vessels (MMSI + name) counted this period."""
        return {"vessels": self.coordinator.vessels_seen(self.area_id, period=self.period)}

    async def async_added_to_hass(self) -> None:
        """Subscribe to tracker updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class AisMapVesselSensor(AisVesselTrackerEntity, SensorEntity):
    """Expose one currently visible vessel for map cards and automations."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:ferry"
    _attr_native_unit_of_measurement = "kn"

    def __init__(
        self,
        entry: AisVesselTrackerConfigEntry,
        coordinator: AisTrackerCoordinator,
        mmsi: str,
        vessel: dict[str, Any],
        photos: tuple[VesselPhotoCoordinator, ...],
    ) -> None:
        """Initialize a vessel map sensor."""
        super().__init__(entry)
        self.coordinator = coordinator
        self.mmsi = mmsi
        self._attr_unique_id = f"ais_vessel_{mmsi}"
        self._attr_suggested_object_id = f"ais_vessel_{mmsi}"
        self._vessel = vessel
        self._photos = photos

    def _photo(self) -> dict[str, Any] | None:
        """Return the first collected photo matching this vessel."""
        for photo in self._photos:
            if photo_data := photo.photo_for_mmsi(self.mmsi):
                return photo_data
        return None

    def _photo_coordinator(self) -> VesselPhotoCoordinator | None:
        """Return the photo coordinator holding this vessel's image."""
        for photo in self._photos:
            if photo.image_for_mmsi(self.mmsi) is not None:
                return photo
        return None

    @property
    def entity_picture(self) -> str | None:
        """Return the authenticated HA URL for the collected map photo."""
        if self._photo_coordinator() is None:
            return None
        return f"/api/ais_vessel_tracker/photo/{self.coordinator.entry_id}/{self.mmsi}"

    @property
    def name(self) -> str:
        """Return the current vessel name."""
        return str(self._vessel.get("vessel_name") or f"AIS Vessel {self.mmsi}")

    @property
    def available(self) -> bool:
        """Return whether this vessel is still inside the map timeout."""
        return self.mmsi in self.coordinator.vessels

    @property
    def native_value(self) -> float | None:
        """Return the vessel speed in knots."""
        value = self._vessel.get("speed_knots")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return vessel position and AIS details."""
        attributes = {key: value for key, value in self._vessel.items() if not key.startswith("_")}
        _add_source_attributes(attributes)
        url = vessel_finder_url(attributes.get("mmsi"))
        if url:
            attributes["vessel_finder_url"] = url
        url = marine_traffic_url(attributes.get("marine_traffic_vessel_id"), attributes.get("mmsi"))
        if url:
            attributes["marinetraffic_url"] = url
        if photo := self._photo():
            if self._photo_coordinator() is not None:
                attributes["picture_url"] = self.entity_picture
            attributes["photo_source_url"] = photo.get("photo_url")
            attributes["photo_origin"] = photo.get("provider")
            attributes["photo_author"] = photo.get("photo_author")
            attributes["photo_credit_url"] = photo.get("photo_credit_url")
            attributes["photo_last_updated"] = photo.get("last_updated")
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to tracker updates."""
        await super().async_added_to_hass()
        if self.mmsi not in self.coordinator.vessels:
            await _async_remove_map_entity(self.hass, self)
            return
        self.async_on_remove(self.coordinator.async_add_listener(self._updated))
        for photo in self._photos:
            self.async_on_remove(photo.async_add_listener(self._photo_updated))

    @callback
    def _photo_updated(self) -> None:
        """Refresh this vessel when a matching photo lookup completes."""
        self.async_write_ha_state()

    @callback
    def _updated(self) -> None:
        """Refresh this vessel from the coordinator."""
        if self.mmsi in self.coordinator.vessels:
            self._vessel = self.coordinator.vessels[self.mmsi]
        self.async_write_ha_state()
