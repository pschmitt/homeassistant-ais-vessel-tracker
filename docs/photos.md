# Vessel photos

The integration optionally searches SearXNG by vessel name and MMSI, then
tries public VesselFinder and MarineTraffic vessel pages when needed. SearXNG
can use HTTP Basic Auth for a protected endpoint. Retrieved images are cached
by MMSI in Home Assistant storage and survive restarts; a generic “No photo”
placeholder is not cached. The last-vessel sensor and a map sensor with a
collected photo expose `entity_picture` and `picture_url`. The integration
serves only already-downloaded public vessel images at an unauthenticated
Home Assistant endpoint: map marker CSS cannot attach an API token. It does
not expose AIS data or integration credentials at that endpoint.

`ais_vessel_tracker.refresh_vessel_photo` accepts vessel sensor targets or a
nine-digit `mmsi`; with neither, it processes all currently known vessels.
Cached images are reused unless `ignore_cache` is set, which removes the
matching cache entries before searching again. Calling it without targets
and with `ignore_cache` clears the entire photo cache before looking up all
known vessels. `ais_vessel_tracker.purge_vessel_photos` clears cached images
without immediately fetching replacements. The next last-vessel change or
new map entity may start another lookup.

Attributes include the original photo source URL, provider, photographer and
credit page when available. VesselFinder links use an MMSI; a MarineTraffic
link is only exposed if its separate internal ship ID was found. A cached
ship ID can keep that link available after a restart.
