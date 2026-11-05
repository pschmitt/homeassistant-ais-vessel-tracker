<p align="center">
  <a href="https://my.home-assistant.io/redirect/marketplace_repository/?owner=pschmitt&amp;repository=homeassistant-ais-vessel-tracker&amp;category=integration"><img src="https://my.home-assistant.io/badges/marketplace_repository.svg" alt="Open this integration in Home Assistant Marketplace"></a>
</p>

<h1 align="center">AIS Vessel Tracker for Home Assistant</h1>

<p align="center">Track vessels passing Home Assistant zones with live AIS feeds.</p>

## Features

- **Multiple AIS sources:** Combine AISStream, AIS-catcher MQTT and AISHub observations by MMSI. See [AIS sources](docs/sources.md).
- **Area tracking:** Count distinct vessels today and over the last hour, and retain the last passing vessel across restarts. See [Areas and entities](docs/areas-and-entities.md).
- **Map entities:** Create a bounded number of temporary vessel sensors with configurable retention.
- **Vessel photos:** Search public photo sources and cache the results for map and sensor images. See [Vessel photos](docs/photos.md).
- **Diagnostics:** Report per-source connectivity and repair issues without exposing configured credentials.

## Install

Use the Marketplace badge above on Home Assistant 2026.11 or later to add this
repository as a custom integration. On older versions, add it as a HACS
custom repository of type **Integration**. Install and restart Home Assistant.
The badge does not imply a public catalog listing. For manual installation,
copy `custom_components/ais_vessel_tracker/` into Home Assistant's
`custom_components/` directory and restart.

## Quick start

1. Create a Home Assistant zone covering the water you want to monitor.
2. Configure an [AISStream](https://aisstream.io) API key or an AIS-catcher MQTT feed. See [AIS sources](docs/sources.md).
3. Add **AIS Vessel Tracker** under **Settings → Devices & services → Add integration** and select the zone.
4. Inspect the area's **Last Passing Vessel** sensor and sighting counters.

## Documentation

| Topic | Description |
|-------|-------------|
| [AIS sources](docs/sources.md) | Source credentials, formats, filtering and deduplication |
| [Areas and entities](docs/areas-and-entities.md) | Zones, retention, events and counters |
| [Vessel photos](docs/photos.md) | Lookup, cache and refresh actions |

## License

[GPL-3.0-or-later](LICENSE). Integration branding lives under
`custom_components/ais_vessel_tracker/brand/`.
