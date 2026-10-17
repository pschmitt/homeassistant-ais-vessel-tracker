"""Services for AIS Vessel Tracker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_IGNORE_CACHE,
    ATTR_MMSI,
    DOMAIN,
    SERVICE_PURGE_VESSEL_PHOTOS,
    SERVICE_REFRESH_VESSEL_PHOTO,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_REFRESH_VESSEL_PHOTO_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_MMSI): vol.All(cv.string, vol.Match(r"^\d{9}$")),
        vol.Optional(ATTR_IGNORE_CACHE, default=False): cv.boolean,
    }
)

SERVICE_PURGE_VESSEL_PHOTOS_SCHEMA = vol.Schema({})


def _known_vessels(tracker: Any) -> dict[str, dict[str, Any]]:
    """Return currently known vessels, keyed by MMSI."""
    vessels: dict[str, dict[str, Any]] = {}
    for vessel in tracker.vessels.values():
        if mmsi := str(vessel.get(ATTR_MMSI) or ""):
            vessels[mmsi] = vessel
    for vessel in tracker.last_vessels.values():
        if mmsi := str(vessel.get(ATTR_MMSI) or ""):
            vessels.setdefault(mmsi, vessel)
    return vessels


def _target_mmsis(hass: HomeAssistant, entity_ids: list[str]) -> set[str]:
    """Resolve targeted AIS entities to their current MMSIs."""
    registry = er.async_get(hass)
    mmsis: set[str] = set()
    invalid_entities: list[str] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
        mmsi = str(state.attributes.get(ATTR_MMSI) or "") if state else ""
        if (
            registry_entry is None
            or registry_entry.platform != DOMAIN
            or not mmsi.isdigit()
            or len(mmsi) != 9
        ):
            invalid_entities.append(entity_id)
            continue
        mmsis.add(mmsi)
    if invalid_entities:
        raise ServiceValidationError(
            "Only AIS vessel sensors with a current nine-digit MMSI can be targeted: "
            + ", ".join(invalid_entities)
        )
    return mmsis


async def _async_refresh_vessel_photo(call: ServiceCall) -> None:
    """Refresh one vessel photo, targeted vessels, or all known photos."""
    requested_mmsi = call.data.get(ATTR_MMSI)
    entity_ids = call.data.get(ATTR_ENTITY_ID, [])
    ignore_cache = call.data.get(ATTR_IGNORE_CACHE, False)
    target_mmsis = _target_mmsis(call.hass, entity_ids) if entity_ids else set()
    if requested_mmsi is not None and target_mmsis:
        raise ServiceValidationError("Specify either an AIS vessel target or an MMSI, not both")

    selected_mmsis = target_mmsis or ({requested_mmsi} if requested_mmsi is not None else None)
    refresh_tasks = []
    found_mmsis: set[str] = set()

    for entry in call.hass.config_entries.async_entries(DOMAIN):
        runtime = entry.runtime_data
        if runtime is None:
            continue
        known_vessels = _known_vessels(runtime.tracker)
        vessels = (
            known_vessels
            if selected_mmsis is None
            else {mmsi: known_vessels[mmsi] for mmsi in selected_mmsis if mmsi in known_vessels}
        )
        found_mmsis.update(vessels)
        if ignore_cache:
            for photo in runtime.photos.values():
                if selected_mmsis is None:
                    await photo.async_forget_all()
                else:
                    for mmsi in selected_mmsis:
                        await photo.async_forget(mmsi)
        for photo in runtime.photos.values():
            refresh_tasks.extend(
                photo.async_refresh(force=True, vessel_override=vessel)
                for vessel in vessels.values()
            )

    if selected_mmsis is not None and found_mmsis != selected_mmsis:
        missing = ", ".join(sorted(selected_mmsis - found_mmsis))
        raise ServiceValidationError(f"No known AIS vessel has MMSI {missing}")
    if not refresh_tasks:
        raise ServiceValidationError("No known AIS vessels or configured photo lookup service")

    if requested_mmsi:
        scope = f" for MMSI {requested_mmsi}"
    elif selected_mmsis:
        scope = f" for {len(selected_mmsis)} targeted vessel(s)"
    else:
        scope = " for all known vessels"
    _LOGGER.info(
        "Refreshing AIS vessel photo(s)%s%s",
        scope,
        " (ignoring cached photos)" if ignore_cache else "",
    )
    results = await asyncio.gather(*refresh_tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        _LOGGER.warning("AIS vessel photo refresh completed with %d error(s)", len(errors))


async def _async_purge_vessel_photos(call: ServiceCall) -> None:
    """Delete every cached vessel photo without looking up new ones."""
    purged = 0
    areas = 0
    for entry in call.hass.config_entries.async_entries(DOMAIN):
        runtime = entry.runtime_data
        if runtime is None:
            continue
        for photo in runtime.photos.values():
            purged += await photo.async_forget_all()
            areas += 1
    _LOGGER.info("Purged %d cached AIS vessel photo(s) across %d area(s)", purged, areas)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register AIS Vessel Tracker services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_VESSEL_PHOTO,
        _async_refresh_vessel_photo,
        schema=SERVICE_REFRESH_VESSEL_PHOTO_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PURGE_VESSEL_PHOTOS,
        _async_purge_vessel_photos,
        schema=SERVICE_PURGE_VESSEL_PHOTOS_SCHEMA,
    )
