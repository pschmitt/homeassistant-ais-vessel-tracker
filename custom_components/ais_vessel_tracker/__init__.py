"""Home Assistant integration for AIS vessel tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from homeassistant.components.http import KEY_HASS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.helpers.issue_registry import IssueSeverity

from .areas import area_id, area_name, configured_areas
from .const import (
    ATTR_MMSI,
    CONF_AREAS,
    CONF_LATITUDE_NORTH,
    CONF_LATITUDE_SOUTH,
    CONF_LONGITUDE_EAST,
    CONF_LONGITUDE_WEST,
    CONF_SEARXNG_PASSWORD,
    CONF_SEARXNG_URL,
    CONF_SEARXNG_USERNAME,
    CONF_ZONE_ENTITY,
    CONF_ZONE_RADIUS,
    DOMAIN,
)
from .coordinator import VesselPhotoCoordinator
from .services import async_setup_services
from .tracker import AisTrackerCoordinator


@dataclass(slots=True)
class AisVesselTrackerRuntime:
    """Runtime objects shared by the integration platforms."""

    tracker: AisTrackerCoordinator
    photos: dict[str, VesselPhotoCoordinator]


type AisVesselTrackerConfigEntry = ConfigEntry[AisVesselTrackerRuntime]


class AisVesselPhotoView(HomeAssistantView):
    """Serve collected vessel photos to Home Assistant frontend image tags."""

    url = "/api/ais_vessel_tracker/photo/{entry_id}/{mmsi}"
    name = "api:ais_vessel_tracker:photo"
    # Map markers use CSS background images and cannot attach the HA bearer
    # header.  The endpoint serves only already-downloaded public provider
    # photos, addressed by an opaque config-entry ID and MMSI.
    requires_auth = False

    async def get(
        self, request: web.Request, entry_id: str, mmsi: str
    ) -> web.Response:
        """Return the locally cached photo for one vessel."""
        entry = request.app[KEY_HASS].config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.runtime_data is None:
            raise web.HTTPNotFound

        for photo in entry.runtime_data.photos.values():
            if image := photo.image_for_mmsi(mmsi):
                image_bytes, content_type = image
                return web.Response(
                    body=image_bytes,
                    content_type=content_type,
                    headers={"Cache-Control": "private, max-age=3600"},
                )
        raise web.HTTPNotFound


def _platforms_for_entry(entry: AisVesselTrackerConfigEntry) -> list[str]:
    """Return only platforms that were forwarded for this config entry."""
    del entry
    return ["sensor", "event", "binary_sensor"]


async def async_migrate_entry(
    hass: HomeAssistant, entry: AisVesselTrackerConfigEntry
) -> bool:
    """Migrate legacy single-area entries to the multi-area format."""
    if entry.version < 3:
        settings = {**entry.data, **entry.options}
        data = dict(entry.data)
        data[CONF_AREAS] = configured_areas(settings)
        hass.config_entries.async_update_entry(entry, data=data, version=3)
    if entry.version < 4:
        legacy_area_keys = (
            CONF_LONGITUDE_WEST,
            CONF_LATITUDE_SOUTH,
            CONF_LONGITUDE_EAST,
            CONF_LATITUDE_NORTH,
            CONF_ZONE_RADIUS,
        )
        data = dict(entry.data)
        data_areas = []
        for area in configured_areas({**entry.data, **entry.options}):
            migrated_area = dict(area)
            if migrated_area.get(CONF_ZONE_ENTITY):
                for key in legacy_area_keys:
                    migrated_area.pop(key, None)
            data_areas.append(migrated_area)
        data[CONF_AREAS] = data_areas

        options = dict(entry.options)
        if CONF_AREAS in options:
            options[CONF_AREAS] = [dict(area) for area in data_areas]
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=4
        )
    return True


def _valid_url(value: str) -> bool:
    """Return whether a value is an HTTP(S) URL."""
    parsed_url = urlparse(value)
    return parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)


def _update_config_issues(
    hass: HomeAssistant, entry: ConfigEntry, settings: dict[str, Any]
) -> None:
    """Create or clear actionable configuration issues."""
    url_issue_id = f"invalid_searxng_url_{entry.entry_id}"
    searxng_url = settings.get(CONF_SEARXNG_URL, "")
    if not searxng_url or _valid_url(searxng_url):
        ir.async_delete_issue(hass, DOMAIN, url_issue_id)
    else:
        ir.async_create_issue(
            hass,
            DOMAIN,
            url_issue_id,
            data={"entry_id": entry.entry_id},
            is_fixable=True,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=IssueSeverity.ERROR,
            translation_key="invalid_searxng_url",
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration domain."""
    del config
    hass.http.register_view(AisVesselPhotoView())
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: AisVesselTrackerConfigEntry
) -> bool:
    """Set up AIS Vessel Tracker from a config entry."""
    settings = {**entry.data, **entry.options}
    _update_config_issues(hass, entry, settings)
    tracker = AisTrackerCoordinator(
        hass,
        async_get_clientsession(hass),
        settings,
        entry.entry_id,
    )
    photos: dict[str, VesselPhotoCoordinator] = {
        area_id(area, index): VesselPhotoCoordinator(
            hass,
            async_get_clientsession(hass),
            str(settings.get(CONF_SEARXNG_URL) or ""),
            tracker,
            settings.get(CONF_SEARXNG_USERNAME),
            settings.get(CONF_SEARXNG_PASSWORD),
            entry.entry_id,
            area_id(area, index),
            area_name(area, index),
        )
        for index, area in enumerate(configured_areas(settings), 1)
    }
    entry.runtime_data = AisVesselTrackerRuntime(tracker=tracker, photos=photos)
    await tracker.async_start()
    for photo in photos.values():
        await photo.async_restore()

    source_zones = {
        str(area[CONF_ZONE_ENTITY])
        for area in configured_areas(settings)
        if area.get(CONF_ZONE_ENTITY)
    }

    @callback
    def source_zone_changed(event: Any) -> None:
        """Restart AIS sources when a source zone moves or resizes."""
        # A zone's state and attributes also change whenever someone enters or
        # leaves it; only its geometry matters for the subscription.
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if old_state is not None and new_state is not None:
            geometry = ("latitude", "longitude", "radius")
            if all(
                old_state.attributes.get(key) == new_state.attributes.get(key)
                for key in geometry
            ):
                return
        entry.async_create_background_task(
            hass,
            tracker.async_restart(),
            "ais_vessel_tracker_zone_changed",
        )

    if source_zones:
        entry.async_on_unload(
            async_track_state_change_event(hass, source_zones, source_zone_changed)
        )

    await hass.config_entries.async_forward_entry_setups(
        entry, _platforms_for_entry(entry)
    )

    @callback
    def tracker_updated() -> None:
        """Refresh configuration diagnostics when tracker data changes."""
        _update_config_issues(hass, entry, settings)

    entry.async_on_unload(tracker.async_add_listener(tracker_updated))

    for photo in photos.values():
        current_vessel = tracker.last_vessels.get(photo.area_id)
        current_mmsi = (
            str(current_vessel.get(ATTR_MMSI) or "") if current_vessel else ""
        )
        if current_mmsi and photo.image_for_mmsi(current_mmsi) is not None:
            continue
        entry.async_create_background_task(
            hass, photo.async_refresh(), "ais_vessel_tracker_initial_refresh"
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AisVesselTrackerConfigEntry
) -> bool:
    """Unload AIS Vessel Tracker."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, _platforms_for_entry(entry)
    )
    if entry.runtime_data is not None:
        await entry.runtime_data.tracker.async_stop()
    return unload_ok
