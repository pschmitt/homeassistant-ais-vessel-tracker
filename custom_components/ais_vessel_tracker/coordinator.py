"""SearXNG search and image-proxy handling for AIS Vessel Tracker."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from aiohttp import BasicAuth, ClientError, ClientResponseError, ClientSession
from bs4 import BeautifulSoup
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import DOMAIN, ISSUE_SEARXNG_AUTHENTICATION, ISSUE_SEARXNG_ENDPOINT
from .entity import marine_traffic_url, vessel_finder_url

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .tracker import AisTrackerCoordinator

_VESSEL_FINDER_NO_PHOTO = re.compile(
    r"(?:cool-ship|no[-_ ]photo|placeholder)",
    re.IGNORECASE,
)
_MARINE_TRAFFIC_VESSEL_ID = re.compile(r"/shipid:(\d+)", re.IGNORECASE)
_RETRY_INTERVAL = timedelta(minutes=5)
_PHOTO_STORE_VERSION = 1


def _search_photo_candidate(search_html: str, searxng_url: str) -> tuple[str, str] | None:
    """Find a supported SearXNG image proxy URL in the result page."""
    soup = BeautifulSoup(search_html, "html.parser")
    for tag in soup.find_all(["a", "img"]):
        for attribute in ("src", "data-src", "href", "data-url"):
            value = tag.get(attribute)
            if not isinstance(value, str) or "/image_proxy" not in value:
                continue
            proxy_url = urljoin(f"{searxng_url}/", value)
            image_url = parse_qs(urlsplit(proxy_url).query).get("url", [""])[0]
            image_url = image_url.lower()
            if "marinetraffic.com/getassetdefaultphoto" in image_url:
                return proxy_url, "MarineTraffic"
            if "static.vesselfinder.net/ship-photo/" in image_url:
                return proxy_url, "VesselFinder"
    return None


def _marine_traffic_vessel_id(search_html: str) -> str | None:
    """Extract MarineTraffic's internal vessel ID from search results."""
    soup = BeautifulSoup(search_html, "html.parser")
    values: list[str] = []
    for tag in soup.find_all(["a", "img", "meta", "source"]):
        for attribute in (
            "href",
            "src",
            "data-src",
            "data-original",
            "data-url",
            "content",
        ):
            value = tag.get(attribute)
            if isinstance(value, str):
                values.append(value)

    # SearXNG may HTML- or percent-encode the result URL in an image result.
    values.append(search_html)
    for value in values:
        normalized = unquote(unescape(value))
        match = _MARINE_TRAFFIC_VESSEL_ID.search(normalized)
        if match:
            return match.group(1)
    return None


def _tag_photo_value(tag: Any) -> str | None:
    """Return the first usable image URL from a parsed HTML tag."""
    for attribute in ("src", "data-src", "data-original", "content"):
        value = tag.get(attribute)
        if isinstance(value, str) and value:
            return value
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if isinstance(srcset, str) and srcset:
        return srcset.split(",", 1)[0].strip().split(" ", 1)[0]
    return None


def _vessel_finder_photo_candidate(details_html: str, details_url: str) -> tuple[str, bool] | None:
    """Extract the main VesselFinder photo and identify placeholders."""
    soup = BeautifulSoup(details_html, "html.parser")
    tags = list(soup.select("img.main-photo"))
    tags.extend(soup.select('meta[property="og:image"]'))
    for tag in tags:
        value = _tag_photo_value(tag)
        if not value:
            continue
        photo_url = urljoin(details_url, value)
        alt_text = " ".join(str(tag.get(attribute) or "") for attribute in ("alt", "title"))
        is_placeholder = bool(
            _VESSEL_FINDER_NO_PHOTO.search(photo_url) or _VESSEL_FINDER_NO_PHOTO.search(alt_text)
        )
        return photo_url, not is_placeholder
    return None


def _vessel_finder_photo_page_url(details_html: str, details_url: str) -> str | None:
    """Return the gallery page URL associated with the main vessel photo."""
    soup = BeautifulSoup(details_html, "html.parser")
    for tag in soup.find_all("a", href=True):
        href = str(tag["href"])
        if re.search(r"/ship-photos/\d+", href):
            return urljoin(details_url, href)
    return None


def _vessel_finder_photo_author(photo_html: str) -> str | None:
    """Extract the photographer name from a VesselFinder photo page."""
    soup = BeautifulSoup(photo_html, "html.parser")
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        if heading.get_text(" ", strip=True).lower() != "photographer":
            continue
        table = heading.find_next("table")
        if table is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2 or cells[0].get_text(" ", strip=True).lower() not in {
                "name",
                "name:",
            }:
                continue
            author = cells[1].get_text(" ", strip=True)
            if author and author != "-":
                return author
    return None


def _marine_traffic_photo_candidate(details_html: str, details_url: str) -> str | None:
    """Extract a MarineTraffic vessel photo from the details page."""
    soup = BeautifulSoup(details_html, "html.parser")
    for tag in soup.find_all(["img", "meta", "source"]):
        value = _tag_photo_value(tag)
        if not value:
            continue
        if "marinetraffic.com/getassetdefaultphoto" in value.lower():
            return urljoin(details_url, value)
    return None


class VesselPhotoCoordinator:
    """Keep the latest vessel photo and its lookup metadata."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: ClientSession,
        searxng_url: str,
        tracker: AisTrackerCoordinator,
        username: str | None,
        password: str | None,
        entry_id: str,
        area_id: str,
        area_name: str,
    ) -> None:
        self.hass = hass
        self.session = session
        self.searxng_url = searxng_url.rstrip("/")
        self.tracker = tracker
        self.area_id = area_id
        self.area_name = area_name
        self._auth = BasicAuth(username, password) if username else None
        self.entry_id = entry_id
        self._image: bytes | None = None
        self._content_type = "image/jpeg"
        self._mmsi = ""
        self._marine_traffic_vessel_id = ""
        self._vessel_name = ""
        self._provider = ""
        self._photo_url = ""
        self._photo_author = ""
        self._photo_credit_url = ""
        self._photo_cacheable = False
        self._last_attempt: datetime | None = None
        self._last_updated: datetime | None = None
        self._error = ""
        self._listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._cached_photos: dict[str, dict[str, Any]] = {}
        self._photo_records: dict[str, dict[str, Any]] = {}
        self._photo_images: dict[str, tuple[bytes, str]] = {}
        self._store = Store(
            hass,
            _PHOTO_STORE_VERSION,
            f"{DOMAIN}.photo_{entry_id}_{area_id}",
        )

    async def async_restore(self) -> None:
        """Restore the cached photo, if one exists for the last vessel."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return

        if isinstance(stored.get("photos"), dict):
            self._cached_photos = {
                str(mmsi): payload
                for mmsi, payload in stored["photos"].items()
                if isinstance(payload, dict) and payload.get("image")
            }
        elif stored.get("image") and stored.get("mmsi"):
            # Migrate the initial single-photo cache format.
            self._cached_photos[str(stored["mmsi"])] = dict(stored)
        self._photo_records.update(self._cached_photos)

        current_vessel = self.tracker.last_vessels.get(self.area_id)
        if current_vessel:
            self._restore_cached_photo(str(current_vessel.get("mmsi") or ""))

    def _decode_cached_image(self, mmsi: str) -> tuple[bytes, str] | None:
        """Return an MMSI's image bytes, decoding and caching them on demand.

        Only the last-passing vessel's photo is decoded eagerly on startup
        (see async_restore); every other MMSI's payload sits in
        _photo_records as base64 until something actually asks for its
        image (entity_picture, the photo view, or a later restore call),
        at which point it is decoded once and kept in _photo_images.
        """
        cached_image = self._photo_images.get(mmsi)
        if cached_image is not None:
            return cached_image
        stored = self._photo_records.get(mmsi)
        if not stored:
            return None
        raw_image = stored.get("image")
        if not raw_image:
            return None
        try:
            image = base64.b64decode(str(raw_image), validate=True)
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring invalid cached AIS photo for MMSI %s", mmsi)
            self._cached_photos.pop(mmsi, None)
            self._photo_records.pop(mmsi, None)
            return None
        if not image:
            return None
        content_type = str(stored.get("content_type") or "image/jpeg")
        if not content_type.lower().startswith("image/"):
            content_type = "image/jpeg"
        self._photo_images[mmsi] = (image, content_type)
        # Match the shape async_refresh() leaves behind after a live fetch:
        # metadata only, no base64 blob duplicated in memory. Reassign
        # rather than mutate `stored` in place -- after async_restore(),
        # _photo_records[mmsi] and _cached_photos[mmsi] are the *same*
        # dict object, and _cached_photos is what gets persisted back to
        # the store, so popping "image" off `stored` would silently strip
        # it from the on-disk cache too.
        self._photo_records[mmsi] = {key: value for key, value in stored.items() if key != "image"}
        return self._photo_images[mmsi]

    def _restore_cached_photo(self, mmsi: str) -> bool:
        """Restore one cached MMSI photo into the active vessel state."""
        if not mmsi:
            return False
        stored = self._photo_records.get(mmsi)
        if not stored:
            return False
        decoded = self._decode_cached_image(mmsi)
        if decoded is None:
            return False
        image, content_type = decoded

        self._image = image
        self._content_type = content_type
        self._mmsi = mmsi
        self._marine_traffic_vessel_id = str(stored.get("marine_traffic_vessel_id") or "")
        self._vessel_name = str(stored.get("vessel_name") or "")
        self._provider = str(stored.get("provider") or "")
        self._photo_url = str(stored.get("photo_url") or "")
        self._photo_author = str(stored.get("photo_author") or "")
        self._photo_credit_url = str(stored.get("photo_credit_url") or "")
        self._last_attempt = datetime.now(UTC)
        self._photo_cacheable = True
        self._error = ""
        last_updated = stored.get("last_updated")
        if isinstance(last_updated, str):
            try:
                self._last_updated = datetime.fromisoformat(last_updated)
            except ValueError:
                self._last_updated = None
        return True

    def _stored_data(self) -> dict[str, Any]:
        """Return the cached photo payload for Home Assistant storage."""
        return {"photos": self._cached_photos}

    def _current_photo_data(self) -> dict[str, Any]:
        """Return the active photo payload for the MMSI cache."""
        return {
            "image": base64.b64encode(self._image or b"").decode("ascii"),
            "content_type": self._content_type,
            "mmsi": self._mmsi,
            "marine_traffic_vessel_id": self._marine_traffic_vessel_id,
            "vessel_name": self._vessel_name,
            "provider": self._provider,
            "photo_url": self._photo_url,
            "photo_author": self._photo_author,
            "photo_credit_url": self._photo_credit_url,
            "last_updated": (self._last_updated.isoformat() if self._last_updated else None),
        }

    @property
    def available(self) -> bool:
        """Return whether a current photo is available."""
        return self._image is not None

    @property
    def image(self) -> bytes | None:
        """Return the cached image bytes."""
        return self._image

    @property
    def content_type(self) -> str:
        """Return the cached image content type."""
        return self._content_type

    def photo_for_mmsi(self, mmsi: object) -> dict[str, Any] | None:
        """Return photo metadata collected for an MMSI, if available."""
        value = str(mmsi).strip() if mmsi is not None else ""
        if not value:
            return None
        photo = self._photo_records.get(value)
        if photo is None or not photo.get("photo_url"):
            return None
        return photo

    def _clear_current_locked(self) -> None:
        """Reset the active photo pointer. Caller must hold ``_lock``."""
        self._image = None
        self._mmsi = ""
        self._marine_traffic_vessel_id = ""
        self._provider = ""
        self._photo_url = ""
        self._photo_author = ""
        self._photo_credit_url = ""
        self._photo_cacheable = False
        self._last_attempt = None
        self._last_updated = None
        self._error = ""

    async def async_forget(self, mmsi: object) -> None:
        """Delete one MMSI's cached photo so the next lookup is a fresh fetch."""
        value = str(mmsi).strip() if mmsi is not None else ""
        if not value:
            return
        async with self._lock:
            changed = self._cached_photos.pop(value, None) is not None
            self._photo_records.pop(value, None)
            self._photo_images.pop(value, None)
            if value == self._mmsi:
                self._clear_current_locked()
            if changed:
                await self._store.async_save(self._stored_data())
        self._notify_listeners()

    async def async_forget_all(self) -> int:
        """Delete every cached photo for this area. Returns the count removed."""
        async with self._lock:
            removed = len(self._cached_photos)
            self._cached_photos.clear()
            self._photo_records.clear()
            self._photo_images.clear()
            self._clear_current_locked()
            if removed:
                await self._store.async_save(self._stored_data())
        self._notify_listeners()
        return removed

    def image_for_mmsi(self, mmsi: object) -> tuple[bytes, str] | None:
        """Return collected image bytes and their content type for an MMSI."""
        value = str(mmsi).strip() if mmsi is not None else ""
        if not value or value not in self._photo_records:
            return None
        return self._decode_cached_image(value)

    @property
    def vessel_name(self) -> str:
        """Return the current vessel name."""
        return self._vessel_name

    @property
    def marine_traffic_vessel_id(self) -> str:
        """Return MarineTraffic's internal vessel ID."""
        return self._marine_traffic_vessel_id

    @property
    def attributes(self) -> dict[str, Any]:
        """Return the current vessel and photo lookup details."""
        vessel = self.tracker.last_vessels.get(self.area_id, {})
        vessel_attributes = {
            key: value
            for key, value in vessel.items()
            if not key.startswith("_") and value is not None
        }
        vessel_name = str(vessel_attributes.get("vessel_name") or self._vessel_name or "")
        mmsi = str(vessel_attributes.get("mmsi") or self._mmsi or "")
        marine_vessel_id = str(
            vessel_attributes.get("marine_traffic_vessel_id")
            or self._marine_traffic_vessel_id
            or ""
        )
        search_query = " ".join(part for part in (vessel_name, mmsi) if part)
        search_url = (
            f"{self.searxng_url}/search?{urlencode({'q': search_query, 'categories': 'images'})}"
            if self.searxng_url and search_query
            else None
        )
        photo_credit = " via ".join(part for part in (self._photo_author, self._provider) if part)
        return {
            **vessel_attributes,
            "vessel_name": vessel_name or None,
            "mmsi": mmsi or None,
            "vessel_finder_url": vessel_finder_url(mmsi),
            "marine_traffic_vessel_id": marine_vessel_id or None,
            "marinetraffic_url": marine_traffic_url(marine_vessel_id, mmsi),
            "provider": self._provider or None,
            "photo_origin": self._provider or None,
            "photo_url": self._photo_url or None,
            "photo_author": self._photo_author or None,
            "photo_credit": photo_credit or None,
            "photo_credit_url": self._photo_credit_url or None,
            "searxng_search_query": search_query or None,
            "searxng_search_url": search_url,
            "last_updated": self._last_updated.isoformat() if self._last_updated else None,
            "error": self._error or None,
        }

    @property
    def needs_refresh(self) -> bool:
        """Return whether the current entity needs a lookup."""
        vessel = self.tracker.last_vessels.get(self.area_id)
        if vessel is None:
            return False
        mmsi = str(vessel.get("mmsi", ""))
        if mmsi != self._mmsi:
            return True
        if self._image is not None and self._photo_cacheable and self._marine_traffic_vessel_id:
            return False
        return (
            self._last_attempt is None or datetime.now(UTC) - self._last_attempt >= _RETRY_INTERVAL
        )

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for coordinator updates."""
        self._listeners.append(listener)

        @callback
        def remove_listener() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove_listener

    async def async_refresh(
        self,
        *,
        force: bool = False,
        vessel_override: dict[str, Any] | None = None,
    ) -> None:
        """Search for and cache the current vessel photo."""
        vessel = vessel_override or self.tracker.last_vessels.get(self.area_id)
        if vessel is None:
            self._set_error("No vessel has been detected yet")
            return

        vessel_name = str(vessel.get("vessel_name") or "Unknown vessel")
        mmsi = str(vessel.get("mmsi") or "")
        if not mmsi:
            self._set_error("Tracked vessel has no MMSI")
            return
        if (
            not force
            and mmsi == self._mmsi
            and self._last_attempt is not None
            and datetime.now(UTC) - self._last_attempt < _RETRY_INTERVAL
        ):
            return

        async with self._lock:
            self._last_attempt = datetime.now(UTC)
            if (
                (mmsi != self._mmsi or self._image is None)
                and self._restore_cached_photo(mmsi)
                and self._marine_traffic_vessel_id
            ):
                self._notify_listeners()
                return
            self._vessel_name = vessel_name
            self._mmsi = mmsi
            self._marine_traffic_vessel_id = str(vessel.get("marine_traffic_vessel_id") or "")
            self._provider = ""
            self._photo_url = ""
            self._photo_author = ""
            self._photo_credit_url = ""
            self._image = None
            self._photo_cacheable = False
            self._error = ""
            query = " ".join(part for part in (vessel_name, mmsi) if part)
            photo_url: str | None = None
            provider = ""
            photo_cacheable = True
            photo_headers = {"User-Agent": "Home Assistant AIS vessel photo lookup"}
            photo_auth = self._auth
            photo_via_searxng = False
            details_url = vessel_finder_url(mmsi)
            details_html: str | None = None

            if self.searxng_url:
                search_url = (
                    f"{self.searxng_url}/search?{urlencode({'q': query, 'categories': 'images'})}"
                )
                try:
                    async with self.session.get(
                        search_url,
                        headers={
                            "Accept": "text/html",
                            "User-Agent": "Home Assistant AIS vessel photo lookup",
                        },
                        auth=self._auth,
                        timeout=15,
                    ) as response:
                        response.raise_for_status()
                        search_html = await response.text()
                    self._set_service_issue(None)
                    marine_vessel_id = _marine_traffic_vessel_id(search_html)
                    if marine_vessel_id:
                        self._marine_traffic_vessel_id = marine_vessel_id
                        self.tracker.set_marine_traffic_vessel_id(mmsi, marine_vessel_id)
                    search_candidate = _search_photo_candidate(search_html, self.searxng_url)
                    if search_candidate:
                        photo_url, provider = search_candidate
                        photo_via_searxng = True
                except ClientResponseError as err:
                    if err.status in (401, 403):
                        self._set_service_issue(ISSUE_SEARXNG_AUTHENTICATION)
                    elif err.status in (404, 429):
                        self._set_service_issue(ISSUE_SEARXNG_ENDPOINT)
                    _LOGGER.warning(
                        "AIS Vessel Tracker SearXNG lookup failed for %s: HTTP %s; "
                        "trying direct provider fallbacks",
                        mmsi,
                        err.status,
                    )
                except (TimeoutError, ClientError) as err:
                    _LOGGER.warning(
                        "AIS Vessel Tracker SearXNG lookup failed for %s: %s; "
                        "trying direct provider fallbacks",
                        mmsi,
                        err,
                    )
            else:
                self._set_service_issue(None)
                _LOGGER.debug(
                    "SearXNG photo lookup is disabled for %s; trying direct provider fallbacks",
                    mmsi,
                )

            if photo_url is None:
                if details_url:
                    try:
                        async with self.session.get(
                            details_url,
                            headers=photo_headers,
                            timeout=15,
                        ) as response:
                            response.raise_for_status()
                            details_html = await response.text()
                        details_candidate = _vessel_finder_photo_candidate(
                            details_html, details_url
                        )
                        if details_candidate:
                            photo_url, photo_cacheable = details_candidate
                            provider = "VesselFinder"
                            photo_headers["Referer"] = details_url
                            photo_auth = None
                    except (TimeoutError, ClientError) as err:
                        _LOGGER.debug(
                            "Direct VesselFinder photo lookup failed for %s: %s",
                            mmsi,
                            err,
                        )
            if photo_url is not None and provider == "VesselFinder" and details_url:
                if details_html is None:
                    try:
                        async with self.session.get(
                            details_url,
                            headers=photo_headers,
                            timeout=15,
                        ) as response:
                            response.raise_for_status()
                            details_html = await response.text()
                    except (TimeoutError, ClientError) as err:
                        _LOGGER.debug(
                            "VesselFinder credit lookup failed for %s: %s",
                            mmsi,
                            err,
                        )
                if details_html:
                    credit_url = _vessel_finder_photo_page_url(details_html, details_url)
                    if credit_url:
                        self._photo_credit_url = credit_url
                        try:
                            async with self.session.get(
                                credit_url,
                                headers=photo_headers,
                                timeout=15,
                            ) as response:
                                response.raise_for_status()
                                credit_html = await response.text()
                            self._photo_author = _vessel_finder_photo_author(credit_html) or ""
                        except (TimeoutError, ClientError) as err:
                            _LOGGER.debug(
                                "VesselFinder photographer lookup failed for %s: %s",
                                mmsi,
                                err,
                            )
            if photo_url is None:
                marine_url = marine_traffic_url(self._marine_traffic_vessel_id, mmsi)
                if marine_url:
                    try:
                        async with self.session.get(
                            marine_url,
                            headers=photo_headers,
                            timeout=15,
                        ) as response:
                            response.raise_for_status()
                            marine_html = await response.text()
                        marine_photo_url = _marine_traffic_photo_candidate(marine_html, marine_url)
                        if marine_photo_url:
                            photo_url = marine_photo_url
                            provider = "MarineTraffic"
                            photo_headers["Referer"] = marine_url
                            photo_auth = None
                    except (TimeoutError, ClientError) as err:
                        _LOGGER.debug(
                            "Direct MarineTraffic photo lookup failed for %s: %s",
                            mmsi,
                            err,
                        )
            if photo_url is None:
                self._set_error("No SearXNG, VesselFinder, or MarineTraffic photo found")
                _LOGGER.debug("No photo result found for %s (%s)", vessel_name, mmsi)
                return

            try:
                async with self.session.get(
                    photo_url,
                    headers=photo_headers,
                    auth=photo_auth,
                    timeout=20,
                ) as response:
                    response.raise_for_status()
                    image = await response.read()
                    content_type = response.headers.get("Content-Type", "image/jpeg")

                    if not image:
                        self._set_error("Photo proxy returned an empty image")
                        return
                    self._image = image
                    self._content_type = content_type.split(";", 1)[0]
                    if not self._content_type.lower().startswith("image/"):
                        self._content_type = "image/jpeg"
                    self._provider = provider
                    self._photo_url = photo_url
                    self._last_updated = datetime.now(UTC)
                    self._photo_cacheable = photo_cacheable
                    photo_data = self._current_photo_data()
                    if photo_cacheable:
                        self._photo_images[mmsi] = (image, self._content_type)
                        self._photo_records[mmsi] = {
                            key: value for key, value in photo_data.items() if key != "image"
                        }
                    if photo_cacheable:
                        self._cached_photos[mmsi] = photo_data
                        await self._store.async_save(self._stored_data())
                    _LOGGER.debug(
                        "Updated %s photo for %s (%s) via %s",
                        self.tracker.entry_id,
                        vessel_name,
                        mmsi,
                        provider,
                    )
            except ClientResponseError as err:
                if photo_via_searxng and err.status in (401, 403):
                    self._set_service_issue(ISSUE_SEARXNG_AUTHENTICATION)
                elif photo_via_searxng and err.status == 404:
                    self._set_service_issue(ISSUE_SEARXNG_ENDPOINT)
                self._set_error(f"Photo lookup failed with HTTP {err.status}")
                _LOGGER.warning(
                    "AIS Vessel Tracker photo lookup failed for %s: HTTP %s",
                    mmsi,
                    err.status,
                )
            except (TimeoutError, ClientError) as err:
                self._set_error(f"Photo lookup failed: {err}")
                _LOGGER.warning("AIS vessel photo lookup failed for %s: %s", mmsi, err)
            finally:
                self._notify_listeners()

    def _set_error(self, error: str) -> None:
        """Set an error while clearing the old photo."""
        self._image = None
        self._error = error

    def _set_service_issue(self, issue_key: str | None) -> None:
        """Create or clear SearXNG service repairs."""
        issue_keys = (ISSUE_SEARXNG_AUTHENTICATION, ISSUE_SEARXNG_ENDPOINT)
        for known_issue in issue_keys:
            issue_id = f"{known_issue}_{self.entry_id}"
            if known_issue == issue_key:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    data={"entry_id": self.entry_id},
                    is_fixable=True,
                    is_persistent=True,
                    issue_domain=DOMAIN,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=known_issue,
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _notify_listeners(self) -> None:
        """Notify entities that coordinator data changed."""
        for listener in tuple(self._listeners):
            listener()
