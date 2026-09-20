"""Multi-source AIS collector for AIS Vessel Tracker."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from math import cos, radians, sqrt
from typing import Any

from aiohttp import WSMsgType
from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .areas import area_bounding_box, area_id, area_zone_location, configured_areas
from .const import (CONF_AISSTREAM_ENABLED, CONF_API_KEY,
                    CONF_AISHUB_ENABLED, CONF_AISHUB_USERNAME,
                    CONF_ENABLE_MAP_ENTITIES, CONF_INCLUDE_CLASS_B,
                    CONF_INCLUDE_NON_VESSEL_STATIONS,
                    CONF_LOCAL_MQTT_ENABLED, CONF_LOCAL_MQTT_TOPIC,
                    CONF_MAP_TIMEOUT_MINUTES, CONF_MAX_MAP_ENTITIES,
                    CONF_VESSEL_WATCHLIST, DOMAIN, ISSUE_AIS_AUTHENTICATION,
                    ISSUE_AIS_CONNECTION, ISSUE_SOURCE_UNAVAILABLE)
from .sources import (SOURCE_AISSTREAM, SOURCE_LOCAL_MQTT, AisObservation,
                      SOURCE_AISHUB, parse_aiscatcher_message,
                      parse_aishub_response, parse_aisstream_message,
                      source_label)

_LOGGER = logging.getLogger(__name__)
_AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
_AISHUB_URL = "https://data.aishub.net/ws.php"
_AISHUB_POLL_INTERVAL = 65
_AISHUB_POSITION_AGE_MINUTES = 5
# Keep the Home Assistant Store version stable. The payload format migration is
# handled below so older installs do not require a Store migration callback.
_STORE_VERSION = 1
_RECONNECT_DELAY = 10
# How often stale map vessels are purged. Runs on its own timer rather than
# as a side effect of the AISStream loop, so a stuck or disconnected
# AISStream (or a deployment with it disabled entirely) doesn't also stop
# map cleanup for the other sources.
_PURGE_INTERVAL = timedelta(seconds=60)
# AISStream raises its own more specific authentication/connection repairs
# (see _create_authentication_issue/_create_connection_issue below), so only
# the other, optional sources get the generic "source unavailable" repair.
_ISSUE_ELIGIBLE_SOURCES = frozenset({SOURCE_LOCAL_MQTT, SOURCE_AISHUB})
# AISHub's rate limit is tracked server-side per account, not per running
# task, and does not reset just because a config-entry reload tore down and
# recreated the polling task. Track the last request per username at module
# scope so it survives a reload within the same HA process.
_aishub_last_request: dict[str, datetime] = {}
# Minimum time between last-passing-vessel handoffs to a *different* vessel.
# Without this, several vessels reporting positions around the same time
# would make the last-passing-vessel sensor and its photo lookup flip-flop
# between them instead of settling on whichever is genuinely most current.
_LAST_VESSEL_DEBOUNCE = timedelta(minutes=3)

_NAV_STATUS = {
    0: "Under way using engine",
    1: "At anchor",
    2: "Not under command",
    3: "Restricted manoeuvrability",
    4: "Constrained by her draught",
    5: "Moored",
    6: "Aground",
    7: "Engaged in fishing",
    8: "Under way sailing",
    14: "AIS-SART active",
    15: "Not defined",
}

_NAV_ICONS = {
    0: "mdi:ferry",
    1: "mdi:anchor",
    5: "mdi:pier",
    7: "mdi:fish",
    8: "mdi:sail-boat",
}


def _vessel_type(type_number: Any) -> str | None:
    """Return a human-readable AIS vessel type."""
    if not isinstance(type_number, int):
        return None
    ranges = (
        (20, 29, "Wing in ground (WIG)"),
        (30, 30, "Fishing"),
        (31, 32, "Towing"),
        (33, 33, "Dredging"),
        (34, 34, "Diving Ops"),
        (35, 35, "Military Ops"),
        (36, 36, "Sailing"),
        (37, 37, "Pleasure Craft"),
        (40, 49, "High-Speed Craft"),
        (50, 50, "Pilot Vessel"),
        (51, 51, "Search and Rescue"),
        (52, 52, "Tug"),
        (53, 53, "Port Tender"),
        (54, 54, "Anti-pollution Equipment"),
        (55, 55, "Law Enforcement"),
        (60, 69, "Passenger Vessel"),
        (70, 79, "Cargo Vessel"),
        (80, 89, "Tanker"),
        (90, 99, "Other"),
    )
    for lower, upper, label in ranges:
        if lower <= type_number <= upper:
            return label
    return None


class AisTrackerCoordinator:
    """Own AIS source connections and the current vessel data."""

    def __init__(
        self, hass: HomeAssistant, session: Any, settings: dict[str, Any], entry_id: str
    ) -> None:
        self.hass = hass
        self.session = session
        self.settings = settings
        self.entry_id = entry_id
        self.last_vessels: dict[str, dict[str, Any]] = {}
        self.vessel_sightings: dict[str, list[dict[str, str]]] = {}
        self.vessels: dict[str, dict[str, Any]] = {}
        self._static_vessel_data: dict[str, dict[str, Any]] = {}
        self.connection_status = "Disconnected"
        self.connection_error: str | None = None
        self.source_status: dict[str, str] = {}
        self.source_errors: dict[str, str] = {}
        self.source_last_message: dict[str, str] = {}
        self._seen_mmsis_by_area: dict[str, set[str]] = {}
        # Local date each area's _seen_mmsis_by_area set was last reset for,
        # so a returning vessel is counted again each day instead of only on
        # the first day it was ever seen since the last restart.
        self._seen_mmsis_date: dict[str, date] = {}
        self._last_vessel_switch: dict[str, datetime] = {}
        self._listeners: list[Callable[[], None]] = []
        self._store = Store(
            hass, _STORE_VERSION, f"{DOMAIN}.last_passing_vessel_{entry_id}"
        )
        self._task: asyncio.Task[None] | None = None
        self._aishub_task: asyncio.Task[None] | None = None
        self._mqtt_unsub: Callable[[], None] | None = None
        self._purge_unsub: Callable[[], None] | None = None
        self._stopping = False

    @property
    def last_vessel(self) -> dict[str, Any] | None:
        """Return the first area's last vessel for backwards compatibility."""
        areas = configured_areas(self.settings)
        if not areas:
            return None
        return self.last_vessels.get(area_id(areas[0], 1))

    @property
    def map_entities_enabled(self) -> bool:
        """Return whether individual vessel entities are enabled."""
        return bool(self.settings.get(CONF_ENABLE_MAP_ENTITIES, False))

    @property
    def vessel_watchlist(self) -> list[str]:
        """Return normalized MMSIs configured as a watchlist."""
        raw = str(self.settings.get(CONF_VESSEL_WATCHLIST, ""))
        return [
            item.strip()
            for item in raw.split(",")
            if item.strip().isdigit() and len(item.strip()) == 9
        ]

    @property
    def max_map_entities(self) -> int:
        """Return the maximum number of active vessel entities to expose."""
        return max(0, int(self.settings.get(CONF_MAX_MAP_ENTITIES, 10)))

    @property
    def aisstream_enabled(self) -> bool:
        """Return whether the AISStream source is enabled."""
        return bool(self.settings.get(CONF_AISSTREAM_ENABLED, True))

    @property
    def local_mqtt_enabled(self) -> bool:
        """Return whether the local AIS-catcher MQTT source is enabled."""
        return bool(self.settings.get(CONF_LOCAL_MQTT_ENABLED, False))

    @property
    def local_mqtt_topic(self) -> str:
        """Return the MQTT subscription topic."""
        return str(self.settings.get(CONF_LOCAL_MQTT_TOPIC, "ais-catcher/ais"))

    @property
    def include_non_vessel_stations(self) -> bool:
        """Return whether base stations, AtoN, and SAR aircraft are kept."""
        return bool(self.settings.get(CONF_INCLUDE_NON_VESSEL_STATIONS, False))

    @property
    def aishub_enabled(self) -> bool:
        """Return whether the AISHub source is enabled."""
        return bool(self.settings.get(CONF_AISHUB_ENABLED, False))

    @property
    def aishub_poll_interval_seconds(self) -> int:
        """Return how often AISHub is polled, in seconds."""
        return _AISHUB_POLL_INTERVAL

    @property
    def aishub_username(self) -> str:
        """Return the configured AISHub username/API credential."""
        return str(self.settings.get(CONF_AISHUB_USERNAME, "")).strip()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to coordinator updates."""
        self._listeners.append(listener)

        @callback
        def remove_listener() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove_listener

    async def async_start(self) -> None:
        """Restore state and start all configured AIS sources."""
        self._stopping = False
        stored = await self._store.async_load()
        migrated = False
        if isinstance(stored, dict) and isinstance(stored.get("last_vessels"), dict):
            self.last_vessels = {
                str(area_key): dict(vessel)
                for area_key, vessel in stored["last_vessels"].items()
                if isinstance(vessel, dict) and vessel.get("mmsi")
            }
            if isinstance(stored.get("vessel_sightings"), dict):
                self.vessel_sightings = {
                    str(area_key): [
                        {
                            "mmsi": str(sighting.get("mmsi")),
                            "spotted_time": str(sighting.get("spotted_time")),
                        }
                        for sighting in sightings
                        if isinstance(sighting, dict)
                        and sighting.get("mmsi")
                        and sighting.get("spotted_time")
                    ]
                    for area_key, sightings in stored["vessel_sightings"].items()
                    if isinstance(sightings, list)
                }
        elif isinstance(stored, dict) and stored.get("mmsi"):
            # Migrate the original single-area store format to area_1.
            areas = configured_areas(self.settings)
            if areas:
                self.last_vessels[area_id(areas[0], 1)] = dict(stored)
                migrated = True
        today = dt_util.now().date()
        for area_key, vessel in self.last_vessels.items():
            spotted_time = vessel.get("spotted_time")
            spotted_date = (
                dt_util.as_local(self._sighting_time({"spotted_time": spotted_time})).date()
                if spotted_time
                else None
            )
            self._seen_mmsis_date[area_key] = today
            # Always initialize alongside _seen_mmsis_date so the two stay in
            # sync: _handle_observation() only (re)creates this set when
            # _seen_mmsis_date[area_key] != today, so an area whose last
            # vessel was NOT spotted today would otherwise be marked as
            # already-initialized-for-today here without ever getting a set,
            # crashing every later observation for that area with KeyError.
            self._seen_mmsis_by_area[area_key] = (
                {str(vessel["mmsi"])} if spotted_date == today else set()
            )
            if not spotted_time:
                continue
            if any(
                sighting.get("mmsi") == str(vessel["mmsi"])
                and sighting.get("spotted_time") == str(spotted_time)
                for sighting in self.vessel_sightings.get(area_key, [])
            ):
                continue
            self.vessel_sightings.setdefault(area_key, []).append(
                {
                    "mmsi": str(vessel["mmsi"]),
                    "vessel_name": vessel.get("vessel_name"),
                    "spotted_time": str(spotted_time),
                }
            )
            migrated = True
        self._purge_old_sightings()
        if migrated:
            await self._store.async_save(self._stored_data())
        self._purge_old_sightings()
        if self.local_mqtt_enabled:
            await self._async_start_mqtt()
        if self.aisstream_enabled and self.settings.get(CONF_API_KEY):
            self._task = self.hass.async_create_task(
                self._run(), name=f"{DOMAIN}_{self.entry_id}"
            )
        if self.aishub_enabled and self.aishub_username:
            self._aishub_task = self.hass.async_create_task(
                self._run_aishub(), name=f"{DOMAIN}_{self.entry_id}_aishub"
            )
        self._purge_unsub = async_track_time_interval(
            self.hass, self._handle_purge_interval, _PURGE_INTERVAL
        )

    async def async_restart(self) -> None:
        """Restart the subscription after a source zone changes."""
        await self.async_stop()
        await self.async_start()

    async def async_stop(self) -> None:
        """Stop all source connections."""
        self._stopping = True
        if self._mqtt_unsub is not None:
            self._mqtt_unsub()
            self._mqtt_unsub = None
        if self._purge_unsub is not None:
            self._purge_unsub()
            self._purge_unsub = None
        for task_name in ("_task", "_aishub_task"):
            task = getattr(self, task_name)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, task_name, None)

    async def _async_start_mqtt(self) -> None:
        """Subscribe to the local AIS-catcher MQTT output."""
        self.source_status[SOURCE_LOCAL_MQTT] = "Connecting"
        try:
            available = await asyncio.wait_for(
                mqtt.async_wait_for_mqtt_client(self.hass), timeout=30
            )
            if not available:
                raise RuntimeError("the Home Assistant MQTT client is unavailable")
            self._mqtt_unsub = await mqtt.async_subscribe(
                self.hass,
                self.local_mqtt_topic,
                self._mqtt_message_received,
                qos=0,
            )
        except Exception as error:  # noqa: BLE001
            self._set_source_status(SOURCE_LOCAL_MQTT, "Unavailable", str(error))
            _LOGGER.warning("Local AIS-catcher MQTT source unavailable: %s", error)
            return
        self._set_source_status(SOURCE_LOCAL_MQTT, "Connected")
        _LOGGER.info(
            "Subscribed to local AIS-catcher MQTT topic %s", self.local_mqtt_topic
        )

    @callback
    def _mqtt_message_received(self, message: Any) -> None:
        """Handle one message from the local AIS-catcher MQTT source."""
        observation = parse_aiscatcher_message(
            message.payload,
            include_non_vessel_stations=self.include_non_vessel_stations,
        )
        if observation is None:
            _LOGGER.debug("Ignoring invalid AIS-catcher MQTT payload")
            return
        self._handle_observation(observation)

    async def _run_aishub(self) -> None:
        """Poll the AISHub API without exceeding its published rate limit."""
        while not self._stopping:
            await self._wait_for_aishub_slot()
            try:
                observations = await self._fetch_aishub()
                self._set_source_status(SOURCE_AISHUB, "Connected")
                for observation in observations:
                    self._handle_observation(observation)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                message = str(error)
                self._set_source_status(SOURCE_AISHUB, "Unavailable", message)
                _LOGGER.warning("AISHub source unavailable: %s", message)
            await asyncio.sleep(_AISHUB_POLL_INTERVAL)

    async def _wait_for_aishub_slot(self) -> None:
        """Wait out any time left since this AISHub account's last request.

        Without this, a config-entry reload (e.g. editing the tracking
        area) tears down and recreates this task, which would otherwise
        poll again immediately -- even though AISHub's rate limit did not
        reset, turning a routine reload into a spurious "source
        unavailable" repair.
        """
        username = self.aishub_username
        last = _aishub_last_request.get(username)
        if last is not None:
            remaining = _AISHUB_POLL_INTERVAL - (
                datetime.now(UTC) - last
            ).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
        _aishub_last_request[username] = datetime.now(UTC)

    async def _fetch_aishub(self) -> list[AisObservation]:
        """Fetch recent AISHub positions for the configured tracking areas."""
        bounding_box = self._aishub_bounding_box()
        if bounding_box is None:
            raise RuntimeError("no valid tracking area is available")
        south, west, north, east = bounding_box
        params = {
            "username": self.aishub_username,
            "format": "1",
            "output": "json",
            "compress": "0",
            "latmin": f"{south:.6f}",
            "latmax": f"{north:.6f}",
            "lonmin": f"{west:.6f}",
            "lonmax": f"{east:.6f}",
            "interval": str(_AISHUB_POSITION_AGE_MINUTES),
        }
        async with self.session.get(_AISHUB_URL, params=params, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            payload = await response.json(content_type=None)
        observations = parse_aishub_response(payload)
        if observations is None:
            raise RuntimeError("invalid or rejected API response")
        return observations

    def _aishub_bounding_box(self) -> tuple[float, float, float, float] | None:
        """Return one bounding box covering all configured tracking areas."""
        boxes = [
            box
            for area in configured_areas(self.settings)
            if (box := area_bounding_box(self.hass, area)) is not None
        ]
        if not boxes:
            return None
        return (
            min(box[0][0] for box in boxes),
            min(box[0][1] for box in boxes),
            max(box[1][0] for box in boxes),
            max(box[1][1] for box in boxes),
        )

    async def _run(self) -> None:
        """Maintain a reconnecting AISStream websocket."""
        delay = _RECONNECT_DELAY
        while not self._stopping:
            try:
                await self._connect_once()
                delay = _RECONNECT_DELAY
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self.connection_error = str(error)
                self._set_status("Disconnected", self.connection_error)
                _LOGGER.warning("AIS Vessel Tracker connection failed: %s", error)
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def _connect_once(self) -> None:
        """Connect once and process AISStream messages until disconnect."""
        self._set_status("Connecting")
        async with self.session.ws_connect(
            _AISSTREAM_URL, heartbeat=30, receive_timeout=90, compress=15
        ) as websocket:
            subscription = {
                "APIKey": self.settings[CONF_API_KEY],
                "BoundingBoxes": [
                    bounding_box
                    for area in configured_areas(self.settings)
                    if (bounding_box := area_bounding_box(self.hass, area)) is not None
                ],
                "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
            }
            if self.settings.get(CONF_INCLUDE_CLASS_B, True):
                subscription["FilterMessageTypes"].extend(
                    ["StandardClassBPositionReport", "ExtendedClassBPositionReport"]
                )
            if self.vessel_watchlist:
                subscription["FiltersShipMMSI"] = self.vessel_watchlist
            await websocket.send_json(subscription)
            confirmed = False
            while not self._stopping:
                try:
                    message = await websocket.receive(timeout=60)
                except asyncio.TimeoutError:
                    continue
                if message.type in {WSMsgType.TEXT, WSMsgType.BINARY}:
                    payload = (
                        message.data
                        if message.type == WSMsgType.TEXT
                        else message.data.decode("utf-8")
                    )
                    parsed = json.loads(payload)
                    if parsed.get("MessageType") == "SubscriptionConfirmation":
                        confirmed = True
                        self.connection_error = None
                        self._clear_authentication_issue()
                        self._clear_connection_issue()
                        self._set_status("Connected")
                    else:
                        self._handle_message(parsed)
                elif message.type in {
                    WSMsgType.CLOSED,
                    WSMsgType.CLOSING,
                    WSMsgType.CLOSE,
                    WSMsgType.ERROR,
                }:
                    if (
                        not confirmed
                        and self.connection_status != "Authentication failed"
                    ):
                        self._create_connection_issue()
                    raise ConnectionError(
                        "AISStream websocket closed "
                        f"(code={websocket.close_code}, exception={websocket.exception()})"
                    )

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle one AISStream message."""
        if message.get("Type") == "Error":
            error = str(message.get("Message", "AISStream returned an error"))
            self.connection_error = error
            self.source_errors[SOURCE_AISSTREAM] = error
            if any(word in error.lower() for word in ("api key", "unauthor", "auth")):
                self._create_authentication_issue()
                self._set_status("Authentication failed", error)
            else:
                self._create_connection_issue()
                self._notify()
            _LOGGER.error("AISStream error: %s", error)
            return

        observation = parse_aisstream_message(message)
        if observation is not None:
            self._handle_observation(observation)

    def _handle_observation(self, observation: AisObservation) -> None:
        """Merge a normalized observation from any configured source."""
        self.source_status[observation.source] = "Connected"
        self.source_errors.pop(observation.source, None)
        self.source_last_message[observation.source] = (
            observation.received_at.isoformat()
        )
        if observation.static_data or observation.vessel_name:
            self._handle_static_data(observation)
        if observation.latitude is None or observation.longitude is None:
            self._notify()
            return

        now = observation.received_at
        old_vessel = self.vessels.get(observation.mmsi, {})
        nav_status = observation.navigational_status
        vessel = dict(old_vessel)
        vessel.update(
            {
                "vessel_name": observation.vessel_name
                or old_vessel.get("vessel_name")
                or "Unknown Vessel",
                "mmsi": observation.mmsi,
                "latitude": observation.latitude,
                "longitude": observation.longitude,
                "speed_knots": observation.speed_knots,
                "course": observation.course,
                "heading": observation.heading,
                "navigational_status": _NAV_STATUS.get(
                    nav_status, old_vessel.get("navigational_status", "Not defined")
                ),
                "vessel_class": observation.vessel_class
                or old_vessel.get("vessel_class", "Unknown"),
                "icon": _NAV_ICONS.get(
                    nav_status, old_vessel.get("icon", "mdi:ferry")
                ),
                "source": observation.source,
                "sources_seen": sorted(
                    set(old_vessel.get("sources_seen", [])) | {observation.source}
                ),
                "spotted_time": now.isoformat(),
                "_last_seen": now,
            }
        )
        if observation.raw_nmea:
            vessel["raw_nmea"] = list(observation.raw_nmea)
        vessel.update(self._static_vessel_data.get(observation.mmsi, {}))
        self.vessels[observation.mmsi] = vessel
        self._trim_map_vessels()
        stored = False
        public_vessel = self._public_vessel(vessel)
        today = dt_util.now().date()
        for tracking_area_id in self._area_ids_for_position(
            observation.latitude, observation.longitude
        ):
            if self._seen_mmsis_date.get(tracking_area_id) != today:
                self._seen_mmsis_by_area[tracking_area_id] = set()
                self._seen_mmsis_date[tracking_area_id] = today
            seen_mmsis = self._seen_mmsis_by_area[tracking_area_id]
            if observation.mmsi not in seen_mmsis:
                seen_mmsis.add(observation.mmsi)
                self.vessel_sightings.setdefault(tracking_area_id, []).append(
                    {
                        "mmsi": observation.mmsi,
                        "vessel_name": public_vessel.get("vessel_name"),
                        "spotted_time": public_vessel["spotted_time"],
                    }
                )
                stored = True

            current_vessel = self.last_vessels.get(tracking_area_id)
            if current_vessel is not None and current_vessel.get("mmsi") == observation.mmsi:
                # The vessel already shown as last-passing just reported a
                # newer position: keep it live without writing to the store
                # on every single update, which could be every few seconds.
                self.last_vessels[tracking_area_id] = public_vessel
                continue

            last_switch = self._last_vessel_switch.get(tracking_area_id)
            if (
                current_vessel is None
                or last_switch is None
                or now - last_switch >= _LAST_VESSEL_DEBOUNCE
            ):
                self.last_vessels[tracking_area_id] = public_vessel
                self._last_vessel_switch[tracking_area_id] = now
                stored = True
        if stored:
            self._purge_old_sightings()
            self.hass.async_create_task(self._store.async_save(self._stored_data()))
        self._notify()

    def _area_ids_for_position(self, latitude: Any, longitude: Any) -> list[str]:
        """Return tracking areas containing a vessel position."""
        try:
            vessel_latitude = float(latitude)
            vessel_longitude = float(longitude)
        except (TypeError, ValueError):
            return []

        matching_areas = []
        for index, area in enumerate(configured_areas(self.settings), 1):
            location = area_zone_location(self.hass, area)
            if location is None:
                continue
            area_latitude, area_longitude, radius = location
            latitude_distance = (vessel_latitude - area_latitude) * 111_320
            longitude_distance = (
                (vessel_longitude - area_longitude)
                * 111_320
                * cos(radians(area_latitude))
            )
            if sqrt(latitude_distance**2 + longitude_distance**2) <= radius:
                matching_areas.append(area_id(area, index))
        return matching_areas

    def _stored_data(self) -> dict[str, Any]:
        """Return the persisted per-area last-vessel payload."""
        return {
            "last_vessels": self.last_vessels,
            "vessel_sightings": self.vessel_sightings,
        }

    def _sightings_in_period(
        self, area_key: str, *, period: str
    ) -> list[dict[str, Any]]:
        """Return raw sightings recorded in the current local time period."""
        now = dt_util.now()
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now - timedelta(seconds=3600)

        matched = []
        for sighting in self.vessel_sightings.get(area_key, []):
            try:
                spotted = datetime.fromisoformat(sighting["spotted_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if spotted.tzinfo is None:
                spotted = spotted.replace(tzinfo=UTC)
            if start <= dt_util.as_local(spotted) <= now:
                matched.append(sighting)
        return matched

    def count_vessel_sightings(self, area_key: str, *, period: str) -> int:
        """Count distinct MMSIs recorded in the current local time period."""
        return len(
            {
                str(sighting["mmsi"])
                for sighting in self._sightings_in_period(area_key, period=period)
            }
        )

    def vessels_seen(self, area_key: str, *, period: str) -> list[dict[str, str]]:
        """Return the distinct vessels (MMSI, name, first sighting) seen."""
        vessels: dict[str, dict[str, str]] = {}
        for sighting in self._sightings_in_period(area_key, period=period):
            mmsi = str(sighting["mmsi"])
            spotted_time = str(sighting.get("spotted_time") or "")
            existing = vessels.get(mmsi)
            if existing is not None and (
                not spotted_time or spotted_time >= existing["spotted_time"]
            ):
                continue
            vessels[mmsi] = {
                "vessel_name": str(sighting.get("vessel_name") or "Unknown Vessel"),
                "spotted_time": spotted_time,
            }
        return [
            {"mmsi": mmsi, **vessels[mmsi]}
            for mmsi in sorted(
                vessels, key=lambda mmsi: (vessels[mmsi]["vessel_name"].lower(), mmsi)
            )
        ]

    def set_marine_traffic_vessel_id(self, mmsi: str, vessel_id: str) -> None:
        """Attach MarineTraffic's internal vessel ID to matching vessel data."""
        updated = False
        for vessel in (*self.vessels.values(), *self.last_vessels.values()):
            if str(vessel.get("mmsi")) != mmsi:
                continue
            if vessel.get("marine_traffic_vessel_id") == vessel_id:
                continue
            vessel["marine_traffic_vessel_id"] = vessel_id
            updated = True
        if updated:
            self.hass.async_create_task(self._store.async_save(self._stored_data()))
            self._notify()

    def _purge_old_sightings(self) -> None:
        """Keep enough history for current and previous local-day counters."""
        cutoff = datetime.now(UTC) - timedelta(days=2)
        for area_key, sightings in self.vessel_sightings.items():
            self.vessel_sightings[area_key] = [
                sighting
                for sighting in sightings
                if self._sighting_time(sighting) >= cutoff
            ]

    @staticmethod
    def _sighting_time(sighting: dict[str, str]) -> datetime:
        """Return a sighting timestamp, treating malformed values as expired."""
        try:
            spotted = datetime.fromisoformat(sighting["spotted_time"])
        except (KeyError, TypeError, ValueError):
            return datetime.min.replace(tzinfo=UTC)
        if spotted.tzinfo is None:
            spotted = spotted.replace(tzinfo=UTC)
        return spotted.astimezone(UTC)

    def _trim_map_vessels(self) -> None:
        """Keep only the most recently reported vessels for map entities."""
        if not self.map_entities_enabled:
            self.vessels.clear()
            return
        excess = len(self.vessels) - self.max_map_entities
        if excess <= 0:
            return
        oldest = sorted(
            self.vessels,
            key=lambda mmsi: self.vessels[mmsi].get(
                "_last_seen", datetime.min.replace(tzinfo=UTC)
            ),
        )[:excess]
        for mmsi in oldest:
            self.vessels.pop(mmsi, None)

    def _handle_static_data(self, observation: AisObservation) -> None:
        """Merge static vessel metadata into the tracked vessel."""
        static_values = dict(observation.static_data)
        if observation.vessel_name:
            static_values["vessel_name"] = observation.vessel_name
        self._static_vessel_data.setdefault(observation.mmsi, {}).update(
            {key: value for key, value in static_values.items() if value is not None}
        )
        vessel = self.vessels.get(observation.mmsi)
        if vessel is not None:
            vessel.update(
                {
                    key: value
                    for key, value in static_values.items()
                    if value is not None
                }
            )
            self._notify()
        updated = False
        for last_vessel in self.last_vessels.values():
            if last_vessel.get("mmsi") != observation.mmsi:
                continue
            last_vessel.update(
                {
                    key: value
                    for key, value in static_values.items()
                    if value is not None
                }
            )
            updated = True
        if updated:
            self.hass.async_create_task(self._store.async_save(self._stored_data()))
            self._notify()

    @callback
    def _handle_purge_interval(self, now: datetime) -> None:
        """Run the periodic stale-vessel purge, independent of any source."""
        del now
        self._purge_old_vessels()

    def _purge_old_vessels(self) -> None:
        """Remove map vessels that have not reported recently."""
        cutoff = datetime.now(UTC) - timedelta(
            minutes=int(self.settings.get(CONF_MAP_TIMEOUT_MINUTES, 30))
        )
        expired = [
            mmsi
            for mmsi, vessel in self.vessels.items()
            if vessel.get("_last_seen", datetime.now(UTC)) < cutoff
        ]
        for mmsi in expired:
            self.vessels.pop(mmsi, None)
        if expired:
            self._notify()

    def _public_vessel(self, vessel: dict[str, Any]) -> dict[str, Any]:
        """Remove internal bookkeeping from a vessel payload."""
        return {
            key: value
            for key, value in vessel.items()
            if not key.startswith("_") and value is not None
        }

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _set_status(self, status: str, error: str | None = None) -> None:
        self.connection_status = status
        self.source_status[SOURCE_AISSTREAM] = status
        if status == "Connected":
            self.source_errors.pop(SOURCE_AISSTREAM, None)
        elif error:
            self.source_errors[SOURCE_AISSTREAM] = error
        self._notify()

    @callback
    def _set_source_status(
        self, source: str, status: str, error: str | None = None
    ) -> None:
        """Update a source status and notify diagnostic entities."""
        self.source_status[source] = status
        if error:
            self.source_errors[source] = error
        else:
            self.source_errors.pop(source, None)
        if source in _ISSUE_ELIGIBLE_SOURCES:
            if status == "Connected":
                self._clear_source_issue(source)
            elif status == "Unavailable":
                self._create_source_issue(source, error)
        self._notify()

    def _create_source_issue(self, source: str, error: str | None) -> None:
        """Raise a repair for an optional source that stopped working."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_SOURCE_UNAVAILABLE}_{source}_{self.entry_id}",
            data={"entry_id": self.entry_id},
            is_fixable=True,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SOURCE_UNAVAILABLE,
            translation_placeholders={
                "source": source_label(source) or source,
                "error": error or "unknown error",
            },
        )

    def _clear_source_issue(self, source: str) -> None:
        """Clear the unavailability repair once a source recovers."""
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_SOURCE_UNAVAILABLE}_{source}_{self.entry_id}"
        )

    def _create_authentication_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_AIS_AUTHENTICATION}_{self.entry_id}",
            data={"entry_id": self.entry_id},
            is_fixable=True,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_AIS_AUTHENTICATION,
        )

    def _clear_authentication_issue(self) -> None:
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_AIS_AUTHENTICATION}_{self.entry_id}"
        )

    def _create_connection_issue(self) -> None:
        """Create a repair when AISStream rejects the subscription."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_AIS_CONNECTION}_{self.entry_id}",
            data={"entry_id": self.entry_id},
            is_fixable=True,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_AIS_CONNECTION,
        )

    def _clear_connection_issue(self) -> None:
        """Clear the subscription repair after confirmation."""
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_AIS_CONNECTION}_{self.entry_id}"
        )
