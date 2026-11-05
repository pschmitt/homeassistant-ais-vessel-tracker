# Areas and entities

Each named tracking area uses an existing Home Assistant `zone.*` entity.
The integration reads its center and radius and rebuilds the AIS subscription
when these change. Add or remove areas under **Configure → Manage tracking
areas**. All areas share one set of source credentials. An optional extended
map radius requests vessels farther out for map display without widening the
zone used for last-vessel detection and counters.

An area creates a persistent **Last Passing Vessel** sensor, distinct-vessel
counters for the local day and rolling last 3,600 seconds, and a
`last_vessel_updated` event entity. The counters' `vessels` attributes contain
MMSI, vessel name and first-sighting timestamp. The event fires
`vessel_updated` when a different vessel takes the last-vessel slot, after a
bounded photo lookup. The last-vessel sensor's position, speed and course
update with reports; switching the slot to a different vessel is debounced
for three minutes. It restores its last known vessel after a restart.

Optional map sensors expose latitude, longitude and AIS details for active
vessels. The default cap is ten; zero disables map sensors without removing
area sensors. A map vessel expires after 30 minutes without reports by
default; both its entity and registry entry are removed, and it can be
recreated on the next sighting. Map sensors are not restored after a restart.
The map timeout is configurable from five minutes to 24 hours. Class B
filtering and an MMSI watchlist are also available in the integration options.

The last-vessel and map sensors include available picture and vessel-page
attributes. See [Vessel photos](photos.md) for the image endpoint and cache.
