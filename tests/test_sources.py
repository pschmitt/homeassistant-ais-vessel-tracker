"""Tests for source parsing that do not require a Home Assistant runtime."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "ais_vessel_tracker" / "sources.py"
SPEC = importlib.util.spec_from_file_location("ais_vessel_tracker_sources", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
sources = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sources
SPEC.loader.exec_module(sources)


class SourceParserTest(unittest.TestCase):
    """Validate the source boundary and normalized fields."""

    def test_source_labels_are_human_readable(self):
        self.assertEqual(sources.source_label("aisstream"), "AISStream")
        self.assertEqual(sources.source_label("local_mqtt"), "AIS-catcher")
        self.assertEqual(sources.source_label("aishub"), "AISHub")
        self.assertEqual(sources.source_label("future_source"), "future_source")
        self.assertIsNone(sources.source_label(None))

    def test_aiscatcher_json_full_position(self) -> None:
        observation = sources.parse_aiscatcher_message(
            {
                "class": "AIS",
                "mmsi": 211234567,
                "type": 1,
                "channel": "A",
                "nmea": ["!AIVDM,1,1,,A,payload*00"],
                "lat": 52.52,
                "lon": 13.31,
                "speed": 8.4,
                "course": 127.2,
                "heading": 126,
                "rxtime": "20260831192345",
            }
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.source, "local_mqtt")
        self.assertEqual(observation.mmsi, "211234567")
        self.assertEqual(observation.latitude, 52.52)
        self.assertEqual(observation.speed_knots, 8.4)
        self.assertEqual(observation.raw_nmea, ("!AIVDM,1,1,,A,payload*00",))
        self.assertEqual(observation.source_timestamp.year, 2026)

    def test_invalid_payloads_are_ignored(self) -> None:
        self.assertIsNone(sources.parse_aiscatcher_message("not json"))
        self.assertIsNone(sources.parse_aiscatcher_message({"mmsi": "123"}))
        self.assertIsNone(sources.parse_aiscatcher_message({"mmsi": 123456789.0}))
        observation = sources.parse_aiscatcher_message(
            {"mmsi": "211234567", "nmea": "not-an-array"}
        )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.raw_nmea, ())

    def test_numeric_mmsi_restores_leading_zeroes(self) -> None:
        observation = sources.parse_aiscatcher_message(
            {"mmsi": 2182801, "lat": 52.52, "lon": 13.31}
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.mmsi, "002182801")
        self.assertIsNone(sources.parse_aiscatcher_message({"mmsi": 123456}))

    def test_aiscatcher_non_vessel_message_types_are_ignored_by_default(self) -> None:
        for message_type in (4, 9, 11, 21):
            observation = sources.parse_aiscatcher_message(
                {
                    "mmsi": 2182801,
                    "type": message_type,
                    "lat": 52.52,
                    "lon": 13.31,
                }
            )
            self.assertIsNone(observation, f"message type {message_type}")

    def test_aiscatcher_non_vessel_message_types_can_be_kept(self) -> None:
        for message_type in (4, 9, 11, 21):
            observation = sources.parse_aiscatcher_message(
                {
                    "mmsi": 2182801,
                    "type": message_type,
                    "lat": 52.52,
                    "lon": 13.31,
                },
                include_non_vessel_stations=True,
            )
            self.assertIsNotNone(observation, f"message type {message_type}")
            assert observation is not None
            self.assertEqual(observation.message_type, message_type)

    def test_aiscatcher_static_data_is_normalized(self) -> None:
        observation = sources.parse_aiscatcher_message(
            {
                "mmsi": "211234567",
                "type": 5,
                "shipname": "TEST VESSEL",
                "callsign": "DTEST",
                "imo": 1234567,
                "destination": "BERLIN",
            }
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.vessel_name, "TEST VESSEL")
        self.assertEqual(observation.static_data["destination"], "BERLIN")
        self.assertEqual(observation.static_data["imo_number"], "1234567")

    def test_vessel_name_padding_is_stripped(self) -> None:
        self.assertEqual(sources._sanitize_vessel_name("PRINS@@@@@@@@@"), "PRINS")
        self.assertEqual(sources._sanitize_vessel_name("@@PRINS@@"), "PRINS")

    def test_corrupted_vessel_name_is_discarded(self) -> None:
        self.assertIsNone(sources._sanitize_vessel_name("PRINS@BRNH@B$"))
        self.assertIsNone(sources._sanitize_vessel_name("@@@@@@@@@@@@@@@@@@@@"))
        self.assertIsNone(sources._sanitize_vessel_name(None))

    def test_aiscatcher_static_data_strips_padding_from_shipname(self) -> None:
        observation = sources.parse_aiscatcher_message(
            {"mmsi": "211234567", "type": 5, "shipname": "PRINS@@@@@@@@@@@@@"}
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.vessel_name, "PRINS")

    def test_aiscatcher_static_data_drops_corrupted_shipname(self) -> None:
        observation = sources.parse_aiscatcher_message(
            {"mmsi": "211234567", "type": 5, "shipname": "PRINS@BRNH@B$"}
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertIsNone(observation.vessel_name)

    def test_aisstream_position_is_normalized(self) -> None:
        observation = sources.parse_aisstream_message(
            {
                "MessageType": "PositionReport",
                "MetaData": {"MMSI": 211234567, "ShipName": "TEST VESSEL"},
                "Message": {
                    "PositionReport": {
                        "Latitude": 52.52,
                        "Longitude": 13.31,
                        "Sog": 8.4,
                        "Cog": 127.2,
                        "TrueHeading": 126,
                        "NavigationalStatus": 0,
                    }
                },
            }
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.source, "aisstream")
        self.assertEqual(observation.vessel_class, "Class A")
        self.assertEqual(observation.navigational_status, 0)

    def test_aishub_human_json_is_normalized(self) -> None:
        observations = sources.parse_aishub_response(
            [
                {"ERROR": False, "FORMAT": "HUMAN", "RECORDS": 1},
                [
                    {
                        "MMSI": 211784980,
                        "TIME": "2026-08-31 19:08:40 GMT",
                        "LONGITUDE": 13.318421,
                        "LATITUDE": 52.52264,
                        "COG": 127.2,
                        "SOG": 8.4,
                        "HEADING": 126,
                        "NAVSTAT": 0,
                        "IMO": 1234567,
                        "NAME": "PAULINE",
                        "CALLSIGN": "DTEST",
                        "TYPE": 70,
                        "A": 10,
                        "B": 20,
                        "DEST": "BERLIN",
                        "ETA": "08-31 20:00",
                    }
                ],
            ]
        )

        self.assertIsNotNone(observations)
        assert observations is not None
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation.source, "aishub")
        self.assertEqual(observation.mmsi, "211784980")
        self.assertEqual(observation.latitude, 52.52264)
        self.assertEqual(observation.static_data["vessel_length"], 30)
        self.assertEqual(observation.source_timestamp.year, 2026)

    def test_aishub_unavailable_sentinels_are_normalized(self) -> None:
        observations = sources.parse_aishub_response(
            [{"ERROR": False}, [{"MMSI": 211234567, "COG": 360, "SOG": 102.4, "HEADING": 511}]]
        )

        self.assertIsNotNone(observations)
        assert observations is not None
        self.assertIsNone(observations[0].course)
        self.assertIsNone(observations[0].speed_knots)
        self.assertIsNone(observations[0].heading)


if __name__ == "__main__":
    unittest.main()
