"""Helpers for normalizing AIS tracking areas."""

from __future__ import annotations

from math import cos, radians
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import (
    CONF_AREA_NAME,
    CONF_AREAS,
    CONF_LATITUDE_NORTH,
    CONF_LATITUDE_SOUTH,
    CONF_LONGITUDE_EAST,
    CONF_LONGITUDE_WEST,
    CONF_MAP_RADIUS,
    CONF_ZONE_ENTITY,
    CONF_ZONE_RADIUS,
)

DEFAULT_AREA_NAME = "Home"


def area_id(area: dict[str, Any], index: int | None = None) -> str:
    """Return the stable identifier for a configured area."""
    return str(area.get("id", f"area_{index or 1}"))


def area_name(area: dict[str, Any], index: int | None = None) -> str:
    """Return the display name for a configured area."""
    return str(area.get("name", f"Area {index or 1}"))


def area_slug(area: dict[str, Any], index: int | None = None) -> str:
    """Return an entity-ID-friendly slug for a configured area."""
    return slugify(area_name(area, index)) or area_id(area, index)


def legacy_area(settings: dict[str, Any]) -> dict[str, Any]:
    """Convert the original single-area settings to one named area."""
    return {
        "id": "area_1",
        "name": DEFAULT_AREA_NAME,
        CONF_ZONE_ENTITY: "zone.home",
        CONF_LONGITUDE_WEST: float(settings.get(CONF_LONGITUDE_WEST, 0)),
        CONF_LATITUDE_SOUTH: float(settings.get(CONF_LATITUDE_SOUTH, 0)),
        CONF_LONGITUDE_EAST: float(settings.get(CONF_LONGITUDE_EAST, 0)),
        CONF_LATITUDE_NORTH: float(settings.get(CONF_LATITUDE_NORTH, 0)),
        CONF_ZONE_RADIUS: float(settings.get(CONF_ZONE_RADIUS, 100)),
    }


def configured_areas(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized configured tracking areas."""
    raw_areas = settings.get(CONF_AREAS)
    if not isinstance(raw_areas, list) or not raw_areas:
        return [legacy_area(settings)]

    areas: list[dict[str, Any]] = []
    for index, raw_area in enumerate(raw_areas, start=1):
        if not isinstance(raw_area, dict):
            continue
        area = dict(raw_area)
        area.setdefault("id", f"area_{index}")
        area.setdefault("name", f"Area {index}")
        if index == 1:
            area.setdefault(CONF_ZONE_ENTITY, "zone.home")
        area.setdefault(CONF_ZONE_RADIUS, 100)
        areas.append(area)
    return areas or [legacy_area(settings)]


def area_form_defaults(area: dict[str, Any]) -> dict[str, Any]:
    """Convert a stored area to config-flow field names."""
    defaults = {
        CONF_AREA_NAME: area.get("name", DEFAULT_AREA_NAME),
        CONF_ZONE_ENTITY: area.get(CONF_ZONE_ENTITY, "zone.home"),
    }
    if area.get(CONF_MAP_RADIUS):
        defaults[CONF_MAP_RADIUS] = area[CONF_MAP_RADIUS]
    return defaults


def area_from_form(user_input: dict[str, Any], index: int) -> dict[str, Any]:
    """Convert config-flow fields to a stored area."""
    area = {
        "id": f"area_{index}",
        "name": str(user_input[CONF_AREA_NAME]).strip(),
        CONF_ZONE_ENTITY: str(user_input[CONF_ZONE_ENTITY]),
    }
    map_radius = user_input.get(CONF_MAP_RADIUS)
    if map_radius:
        area[CONF_MAP_RADIUS] = float(map_radius)
    return area


def area_zone_location(
    hass: HomeAssistant, area: dict[str, Any]
) -> tuple[float, float, float] | None:
    """Return the source HA zone's latitude, longitude, and radius."""
    zone_entity = str(area.get(CONF_ZONE_ENTITY, "")).strip()
    if zone_entity:
        state = hass.states.get(zone_entity)
        if state is not None:
            try:
                latitude = float(state.attributes["latitude"])
                longitude = float(state.attributes["longitude"])
                radius = float(state.attributes["radius"])
                if radius > 0:
                    return latitude, longitude, radius
            except (KeyError, TypeError, ValueError):
                pass

    try:
        latitude = (float(area[CONF_LATITUDE_SOUTH]) + float(area[CONF_LATITUDE_NORTH])) / 2
        longitude = (float(area[CONF_LONGITUDE_WEST]) + float(area[CONF_LONGITUDE_EAST])) / 2
        radius = float(area.get(CONF_ZONE_RADIUS, 100))
    except (KeyError, TypeError, ValueError):
        return None
    return (latitude, longitude, radius) if radius > 0 else None


def area_map_location(
    hass: HomeAssistant, area: dict[str, Any]
) -> tuple[float, float, float] | None:
    """Return the area's center and the radius used to fetch AIS data.

    This is the source zone's radius, widened to the optional per-area
    ``map_radius`` when one is configured. It is only used to decide how much
    data to request from AIS sources; matching vessels to an area for the
    last-passing-vessel entity and sighting counters always uses the narrower
    ``area_zone_location`` radius, so a wider map radius only surfaces more
    vessels on the map without affecting notifications.
    """
    location = area_zone_location(hass, area)
    if location is None:
        return None
    latitude, longitude, radius = location
    try:
        map_radius = float(area.get(CONF_MAP_RADIUS, 0) or 0)
    except (TypeError, ValueError):
        map_radius = 0
    return (latitude, longitude, max(radius, map_radius))


def area_bounding_box(hass: HomeAssistant, area: dict[str, Any]) -> list[list[float]] | None:
    """Convert a circular HA zone into an AISStream square bounding box."""
    location = area_map_location(hass, area)
    if location is None:
        return None
    latitude, longitude, radius = location
    latitude_delta = radius / 111_320
    longitude_delta = radius / (111_320 * max(abs(cos(radians(latitude))), 0.01))
    return [
        [latitude - latitude_delta, longitude - longitude_delta],
        [latitude + latitude_delta, longitude + longitude_delta],
    ]
