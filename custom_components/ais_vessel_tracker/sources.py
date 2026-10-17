"""AIS source adapters and the normalized observation model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SOURCE_AISSTREAM = "aisstream"
SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_AISHUB = "aishub"

SOURCE_LABELS = {
    SOURCE_AISSTREAM: "AISStream",
    SOURCE_LOCAL_MQTT: "AIS-catcher",
    SOURCE_AISHUB: "AISHub",
}

# AIS message types that report a station or object rather than a vessel:
# 4/11 base station (and its UTC/date response), 9 SAR aircraft, 21
# aid-to-navigation. AISStream's own message-type filter already excludes
# these; AIS-catcher's JSON_FULL feed does not, so they are dropped here by
# default (see include_non_vessel_stations on parse_aiscatcher_message).
_NON_VESSEL_MESSAGE_TYPES = frozenset({4, 9, 11, 21})


def source_label(source: object) -> str | None:
    """Return a human-readable label for a normalized source identifier."""
    value = str(source).strip() if source is not None else ""
    return SOURCE_LABELS.get(value, value or None)


@dataclass(slots=True)
class AisObservation:
    """One decoded AIS observation from any supported source."""

    source: str
    mmsi: str
    latitude: float | None = None
    longitude: float | None = None
    speed_knots: float | None = None
    course: float | None = None
    heading: int | None = None
    vessel_name: str | None = None
    destination: str | None = None
    eta: str | None = None
    vessel_type: str | None = None
    call_sign: str | None = None
    imo_number: str | None = None
    navigational_status: int | None = None
    vessel_class: str | None = None
    message_type: int | None = None
    source_timestamp: datetime | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    raw_nmea: tuple[str, ...] = ()
    static_data: dict[str, Any] = field(default_factory=dict)


def parse_aiscatcher_message(
    payload: str | bytes | bytearray | dict[str, Any],
    *,
    include_non_vessel_stations: bool = False,
) -> AisObservation | None:
    """Parse one AIS-catcher JSON MQTT message.

    By default, base stations, aids to navigation, and SAR aircraft are
    dropped; set include_non_vessel_stations to keep them.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict):
        return None

    mmsi = _mmsi(payload.get("mmsi"))
    if mmsi is None:
        return None

    message_type = _int(payload.get("type"))
    if not include_non_vessel_stations and message_type in _NON_VESSEL_MESSAGE_TYPES:
        return None

    static_data = _static_data_from_aiscatcher(payload)
    return AisObservation(
        source=SOURCE_LOCAL_MQTT,
        mmsi=mmsi,
        latitude=_float(payload.get("lat")),
        longitude=_float(payload.get("lon")),
        speed_knots=_float(payload.get("speed")),
        course=_float(payload.get("course")),
        heading=_int(payload.get("heading")),
        vessel_name=_sanitize_vessel_name(payload.get("shipname")),
        destination=static_data.get("destination"),
        eta=static_data.get("eta"),
        vessel_type=static_data.get("vessel_type"),
        call_sign=static_data.get("call_sign"),
        imo_number=static_data.get("imo_number"),
        navigational_status=_int(payload.get("status")),
        message_type=message_type,
        source_timestamp=_aiscatcher_timestamp(payload),
        raw_nmea=_nmea(payload.get("nmea")),
        static_data=static_data,
    )


def parse_aisstream_message(message: dict[str, Any]) -> AisObservation | None:
    """Parse one AISStream message into the common observation model."""
    metadata = message.get("MetaData") or {}
    mmsi = _mmsi(metadata.get("MMSI"))
    if mmsi is None:
        return None

    message_type = message.get("MessageType")
    if message_type == "ShipStaticData":
        static = (message.get("Message") or {}).get("ShipStaticData") or {}
        return AisObservation(
            source=SOURCE_AISSTREAM,
            mmsi=mmsi,
            vessel_name=_sanitize_vessel_name(metadata.get("ShipName")),
            static_data=_static_data_from_aisstream(static),
        )

    report_types = {
        "PositionReport": "Class A",
        "StandardClassBPositionReport": "Class B",
        "ExtendedClassBPositionReport": "Class B",
    }
    vessel_class = report_types.get(message_type)
    if vessel_class is None:
        return None
    report = (message.get("Message") or {}).get(message_type) or {}
    return AisObservation(
        source=SOURCE_AISSTREAM,
        mmsi=mmsi,
        latitude=_float(report.get("Latitude")),
        longitude=_float(report.get("Longitude")),
        speed_knots=_float(report.get("Sog")),
        course=_float(report.get("Cog")),
        heading=_int(report.get("TrueHeading")),
        vessel_name=_sanitize_vessel_name(metadata.get("ShipName")),
        navigational_status=_int(report.get("NavigationalStatus")),
        vessel_class=vessel_class,
    )


def parse_aishub_response(payload: Any) -> list[AisObservation] | None:
    """Parse one AISHub human-readable JSON response."""
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    header, records = payload[0], payload[1]
    if not isinstance(header, dict) or header.get("ERROR") is True:
        return None
    if not isinstance(records, list):
        return None

    observations = []
    for record in records:
        if not isinstance(record, dict):
            continue
        mmsi = _mmsi(record.get("MMSI"))
        if mmsi is None:
            continue
        observations.append(
            AisObservation(
                source=SOURCE_AISHUB,
                mmsi=mmsi,
                latitude=_available_float(record.get("LATITUDE")),
                longitude=_available_float(record.get("LONGITUDE")),
                speed_knots=_available_float(record.get("SOG"), unavailable=102.4),
                course=_available_float(record.get("COG"), unavailable=360),
                heading=_available_int(record.get("HEADING"), unavailable=511),
                vessel_name=_sanitize_vessel_name(record.get("NAME")),
                navigational_status=_int(record.get("NAVSTAT")),
                source_timestamp=_aishub_timestamp(record),
                static_data=_static_data_from_aishub(record),
            )
        )
    return observations


def _static_data_from_aiscatcher(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract optional static/voyage fields from JSON_FULL output."""
    values = {
        "destination": _text(payload.get("destination")),
        "eta": _text(payload.get("eta")),
        "vessel_type": _text(payload.get("shiptype_text")),
        "call_sign": _text(payload.get("callsign")),
        "imo_number": _text(payload.get("imo")),
    }
    return {key: value for key, value in values.items() if value is not None}


def _static_data_from_aishub(record: dict[str, Any]) -> dict[str, Any]:
    """Extract optional static/voyage fields from an AISHub record."""
    values = {
        "destination": _text(record.get("DEST")),
        "eta": _text(record.get("ETA")),
        "vessel_type": _vessel_type(record.get("TYPE")),
        "call_sign": _text(record.get("CALLSIGN")),
        "imo_number": _text(record.get("IMO")),
        "vessel_length": _sum_dimensions(record.get("A"), record.get("B")),
    }
    return {key: value for key, value in values.items() if value is not None}


def _static_data_from_aisstream(static: dict[str, Any]) -> dict[str, Any]:
    """Extract optional static/voyage fields from AISStream output."""
    dimensions = static.get("Dimension") or {}
    values = {
        "destination": _text(static.get("Destination")),
        "eta": _eta(static.get("Eta")),
        "vessel_type": _vessel_type(static.get("Type")),
        "call_sign": _text(static.get("CallSign")),
        "imo_number": _text(static.get("ImoNumber")),
        "vessel_length": _sum_dimensions(dimensions.get("A"), dimensions.get("B")),
    }
    return {key: value for key, value in values.items() if value is not None}


def _aiscatcher_timestamp(payload: dict[str, Any]) -> datetime | None:
    """Return the source receive timestamp from AIS-catcher metadata."""
    unix_time = _float(payload.get("rxuxtime"))
    if unix_time is not None:
        try:
            return datetime.fromtimestamp(unix_time, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    value = payload.get("rxtime")
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _aishub_timestamp(record: dict[str, Any]) -> datetime | None:
    """Return the AISHub position timestamp in UTC."""
    value = record.get("TIME") or record.get("TSTAMP")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S GMT", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _mmsi(value: Any) -> str | None:
    """Normalize an MMSI value without accepting malformed identifiers.

    AIS-catcher emits MMSI as a JSON integer.  JSON integers do not preserve
    leading zeroes, which are valid for some non-vessel AIS identities.  Keep
    string input strict, but restore those zeroes for integer input.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # The normal nine-digit forms may lose one or two leading zeroes when
        # AIS-catcher serializes them as JSON integers.  Seven digits is the
        # shortest plausible representation after that loss.
        if not 1_000_000 <= value <= 999_999_999:
            return None
        normalized = f"{value:09d}"
    elif isinstance(value, str):
        normalized = value.strip()
    else:
        return None
    if not normalized.isdigit() or len(normalized) != 9:
        return None
    return normalized


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _sanitize_vessel_name(value: Any) -> str | None:
    """Clean up a raw AIS ship name.

    AIS transmits names as fixed-width 6-bit ASCII, right-padded with "@"
    (bit-pattern 0) to fill unused character positions (ITU-R M.1371). A
    "@" appearing anywhere else in the decoded text means bits were zeroed
    out by a weak or noisy reception, so treat any name still containing
    "@" after stripping padding as unusable rather than surface a garbled
    string.
    """
    text = _text(value)
    if text is None:
        return None
    stripped = text.strip("@").strip()
    if not stripped or "@" in stripped:
        return None
    return stripped


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _available_float(value: Any, *, unavailable: float | None = None) -> float | None:
    """Parse a float while mapping an AIS unavailable sentinel to None."""
    parsed = _float(value)
    if parsed is None or parsed == unavailable:
        return None
    return parsed


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _available_int(value: Any, *, unavailable: int | None = None) -> int | None:
    """Parse an integer while mapping an AIS unavailable sentinel to None."""
    parsed = _int(value)
    if parsed is None or parsed == unavailable:
        return None
    return parsed


def _nmea(value: Any) -> tuple[str, ...]:
    """Return NMEA sentences from the JSON array, ignoring malformed values."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _eta(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    month = _int(value.get("Month"))
    day = _int(value.get("Day"))
    if not month or not day:
        return None
    hour = _int(value.get("Hour")) or 0
    minute = _int(value.get("Minute")) or 0
    return f"{day:02d}/{month:02d} {hour:02d}:{minute:02d} UTC"


def _sum_dimensions(first: Any, second: Any) -> int | None:
    if first is None or second is None:
        return None
    try:
        return int(first) + int(second)
    except (TypeError, ValueError):
        return None


def _vessel_type(type_number: Any) -> str | None:
    number = _int(type_number)
    if number is None:
        return None
    for lower, upper, label in (
        (20, 29, "Wing in ground (WIG)"),
        (30, 30, "Fishing"),
        (31, 32, "Towing"),
        (33, 33, "Dredging"),
        (34, 35, "Special operation"),
        (36, 36, "Sailing"),
        (37, 37, "Pleasure craft"),
        (40, 49, "High-speed craft"),
        (50, 59, "Service vessel"),
        (60, 69, "Passenger vessel"),
        (70, 79, "Cargo vessel"),
        (80, 89, "Tanker"),
        (90, 99, "Other"),
    ):
        if lower <= number <= upper:
            return label
    return None
