# AIS sources

Configure one or more sources in the integration's config and options flows:

- **AISStream:** Create an API key at [AISStream](https://aisstream.io). A
  single WebSocket subscription covers all configured areas. AISStream
  subscription failures and authentication errors create repair issues.
- **AIS-catcher MQTT:** Enable Home Assistant's MQTT integration and publish
  `JSON_FULL` AIS-catcher messages to the configured topic (default
  `ais-catcher/ais`). `JSON_NMEA` alone does not contain decoded positions.
  Base stations, aids to navigation and SAR aircraft are filtered out by
  default; the local-source option can include them. The integration uses
  Home Assistant's MQTT subscription, not a direct broker connection.
- **AISHub:** An approved contributor account is required before enabling
  this optional remote source. The integration polls recent positions within
  the combined area bounds every 65 seconds to respect the one-request-per-
  minute limit. See [AISHub's requirements](https://www.aishub.net/join-us).

Reports with invalid MMSIs or missing positions cannot trigger area entry.
Static and voyage reports enrich later position reports. Observations for the
same nine-digit MMSI are merged; `source` and `source_name` identify the latest
source, while `sources_seen` lists the sources encountered during the current
runtime. AISHub's aggregate positions do **not** prove that a local antenna
received the vessel. A local AIS-catcher receiver is needed for that claim.

Per-source diagnostic binary sensors show connectivity, last message, errors
and tracked vessel counts. Repairs clear when a failing source recovers.
