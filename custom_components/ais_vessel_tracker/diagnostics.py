"""Diagnostics support for AIS Vessel Tracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AisVesselTrackerConfigEntry
from .areas import configured_areas
from .const import (
    CONF_AISHUB_USERNAME,
    CONF_API_KEY,
    CONF_ENABLE_MAP_ENTITIES,
    CONF_INCLUDE_CLASS_B,
    CONF_MAP_TIMEOUT_MINUTES,
    CONF_MAX_MAP_ENTITIES,
    CONF_SEARXNG_PASSWORD,
    CONF_SEARXNG_URL,
    CONF_SEARXNG_USERNAME,
    CONF_VESSEL_WATCHLIST,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    config_entry: AisVesselTrackerConfigEntry,
) -> dict[str, Any]:
    """Return safe diagnostics for an AIS Vessel Tracker config entry."""
    del hass
    runtime = config_entry.runtime_data
    tracker = runtime.tracker
    photos = runtime.photos
    settings = {**config_entry.data, **config_entry.options}
    photos = {
        area_id: {
            "available": photo.available,
            "attributes": photo.attributes,
            "image_size": len(photo.image or b""),
        }
        for area_id, photo in photos.items()
    }
    return {
        "entry": {
            "title": config_entry.title,
            "data": async_redact_data(
                dict(config_entry.data),
                {CONF_AISHUB_USERNAME, CONF_SEARXNG_PASSWORD},
            ),
            "options": async_redact_data(
                dict(config_entry.options),
                {CONF_AISHUB_USERNAME, CONF_SEARXNG_PASSWORD},
            ),
        },
        "settings": {
            CONF_API_KEY: "REDACTED" if settings.get(CONF_API_KEY) else None,
            CONF_AISHUB_USERNAME: ("REDACTED" if settings.get(CONF_AISHUB_USERNAME) else None),
            CONF_ENABLE_MAP_ENTITIES: settings.get(CONF_ENABLE_MAP_ENTITIES),
            CONF_INCLUDE_CLASS_B: settings.get(CONF_INCLUDE_CLASS_B),
            CONF_VESSEL_WATCHLIST: settings.get(CONF_VESSEL_WATCHLIST),
            CONF_MAP_TIMEOUT_MINUTES: settings.get(CONF_MAP_TIMEOUT_MINUTES),
            CONF_MAX_MAP_ENTITIES: settings.get(CONF_MAX_MAP_ENTITIES, 10),
            CONF_SEARXNG_URL: settings.get(CONF_SEARXNG_URL),
            CONF_SEARXNG_USERNAME: settings.get(CONF_SEARXNG_USERNAME),
            "areas": configured_areas(settings),
        },
        "tracker": {
            "connection_status": tracker.connection_status,
            "connection_error": tracker.connection_error,
            "source_status": tracker.source_status,
            "source_errors": tracker.source_errors,
            "source_last_message": tracker.source_last_message,
            "last_vessels": tracker.last_vessels,
            "map_vessel_count": len(tracker.vessels),
        },
        "photos": photos,
    }
