"""Config flow for AIS Vessel Tracker."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import probatio as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntry, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .areas import area_form_defaults, area_from_form, configured_areas
from .const import (
    CONF_AISHUB_ENABLED,
    CONF_AISHUB_USERNAME,
    CONF_AISSTREAM_ENABLED,
    CONF_API_KEY,
    CONF_AREA_COUNT,
    CONF_AREA_NAME,
    CONF_AREAS,
    CONF_ENABLE_MAP_ENTITIES,
    CONF_INCLUDE_CLASS_B,
    CONF_INCLUDE_NON_VESSEL_STATIONS,
    CONF_LOCAL_MQTT_ENABLED,
    CONF_LOCAL_MQTT_TOPIC,
    CONF_MAP_RADIUS,
    CONF_MAP_TIMEOUT_MINUTES,
    CONF_MAX_MAP_ENTITIES,
    CONF_SEARXNG_PASSWORD,
    CONF_SEARXNG_URL,
    CONF_SEARXNG_USERNAME,
    CONF_VESSEL_WATCHLIST,
    CONF_ZONE_ENTITY,
    DOMAIN,
)

_MAX_AREAS = 10


def _valid_url(value: str) -> bool:
    """Return whether a value is an HTTP(S) URL."""
    parsed_url = urlparse(value.strip())
    return parsed_url.scheme in {"http", "https"} and bool(parsed_url.hostname)


def _normalize_url(value: str) -> str:
    """Normalize a configured SearXNG URL."""
    return value.strip().rstrip("/")


def _common_schema(
    defaults: dict[str, Any] | None = None,
    *,
    api_key_required: bool,
    include_area_count: bool = True,
    include_sources: bool = True,
) -> vol.Schema:
    """Return the shared, non-area integration settings schema."""
    defaults = defaults or {}
    schema: dict[Any, Any] = {
        vol.Required(CONF_ENABLE_MAP_ENTITIES, default=True): BooleanSelector(),
        vol.Required(CONF_INCLUDE_CLASS_B, default=True): BooleanSelector(),
        vol.Optional(CONF_VESSEL_WATCHLIST, default=""): TextSelector(),
        vol.Required(CONF_MAP_TIMEOUT_MINUTES, default=30): NumberSelector(
            NumberSelectorConfig(min=5, max=1440, step=1)
        ),
        vol.Required(CONF_MAX_MAP_ENTITIES, default=10): NumberSelector(
            NumberSelectorConfig(min=0, max=50, step=1, mode="slider")
        ),
        vol.Optional(CONF_SEARXNG_URL, default=""): TextSelector(),
        vol.Optional(CONF_SEARXNG_USERNAME): TextSelector(),
        vol.Optional(CONF_SEARXNG_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
    if include_sources:
        schema.update(
            {
                vol.Required(
                    CONF_AISSTREAM_ENABLED,
                    default=defaults.get(CONF_AISSTREAM_ENABLED, True),
                ): BooleanSelector(),
                (
                    data_entry_flow.Required(
                        CONF_API_KEY, visible={"field": CONF_AISSTREAM_ENABLED, "value": True}
                    )
                    if api_key_required
                    else data_entry_flow.Optional(
                        CONF_API_KEY,
                        default=defaults.get(CONF_API_KEY, ""),
                        visible={"field": CONF_AISSTREAM_ENABLED, "value": True},
                    )
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(
                    CONF_LOCAL_MQTT_ENABLED,
                    default=defaults.get(CONF_LOCAL_MQTT_ENABLED, True),
                ): BooleanSelector(),
                data_entry_flow.Required(
                    CONF_LOCAL_MQTT_TOPIC,
                    default=defaults.get(CONF_LOCAL_MQTT_TOPIC, "ais-catcher/ais"),
                    visible={"field": CONF_LOCAL_MQTT_ENABLED, "value": True},
                ): TextSelector(),
                data_entry_flow.Required(
                    CONF_INCLUDE_NON_VESSEL_STATIONS,
                    default=defaults.get(CONF_INCLUDE_NON_VESSEL_STATIONS, False),
                    visible={"field": CONF_LOCAL_MQTT_ENABLED, "value": True},
                ): BooleanSelector(),
                vol.Required(
                    CONF_AISHUB_ENABLED,
                    default=defaults.get(CONF_AISHUB_ENABLED, False),
                ): BooleanSelector(),
                data_entry_flow.Optional(
                    CONF_AISHUB_USERNAME,
                    default=defaults.get(CONF_AISHUB_USERNAME, ""),
                    visible={"field": CONF_AISHUB_ENABLED, "value": True},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
    if include_area_count:
        schema[vol.Required(CONF_AREA_COUNT, default=defaults.get(CONF_AREA_COUNT, 1))] = (
            NumberSelector(NumberSelectorConfig(min=1, max=_MAX_AREAS, step=1, mode="slider"))
        )
    return vol.Schema(schema)


def _aisstream_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the AISStream source settings schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_AISSTREAM_ENABLED,
                default=defaults.get(CONF_AISSTREAM_ENABLED, True),
            ): BooleanSelector(),
            data_entry_flow.Optional(
                CONF_API_KEY,
                default=defaults.get(CONF_API_KEY, ""),
                visible={"field": CONF_AISSTREAM_ENABLED, "value": True},
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
    )


def _local_mqtt_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the local AIS-catcher MQTT source settings schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_LOCAL_MQTT_ENABLED,
                default=defaults.get(CONF_LOCAL_MQTT_ENABLED, True),
            ): BooleanSelector(),
            data_entry_flow.Required(
                CONF_LOCAL_MQTT_TOPIC,
                default=defaults.get(CONF_LOCAL_MQTT_TOPIC, "ais-catcher/ais"),
                visible={"field": CONF_LOCAL_MQTT_ENABLED, "value": True},
            ): TextSelector(),
            data_entry_flow.Required(
                CONF_INCLUDE_NON_VESSEL_STATIONS,
                default=defaults.get(CONF_INCLUDE_NON_VESSEL_STATIONS, False),
                visible={"field": CONF_LOCAL_MQTT_ENABLED, "value": True},
            ): BooleanSelector(),
        }
    )


def _aishub_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the AISHub source settings schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_AISHUB_ENABLED,
                default=defaults.get(CONF_AISHUB_ENABLED, False),
            ): BooleanSelector(),
            data_entry_flow.Optional(
                CONF_AISHUB_USERNAME,
                default=defaults.get(CONF_AISHUB_USERNAME, ""),
                visible={"field": CONF_AISHUB_ENABLED, "value": True},
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
    )


def _zone_options(hass: Any, selected: str | None = None) -> list[dict[str, str]]:
    """Return selectable Home Assistant zones for an area source."""
    options = []
    entity_ids = hass.states.async_entity_ids("zone")
    for entity_id in sorted(entity_ids):
        state = hass.states.get(entity_id)
        label = (
            str(state.attributes.get("friendly_name", entity_id))
            if state is not None
            else entity_id
        )
        options.append({"value": entity_id, "label": f"{label} ({entity_id})"})
    if selected and selected not in entity_ids:
        options.append({"value": selected, "label": selected})
    return options


def _area_schema(defaults: dict[str, Any] | None = None, hass: Any = None) -> vol.Schema:
    """Return the schema for one named tracking area."""
    defaults = defaults or {}
    selected_zone = defaults.get(CONF_ZONE_ENTITY, "zone.home")
    return vol.Schema(
        {
            vol.Required(
                CONF_AREA_NAME, default=defaults.get(CONF_AREA_NAME, "Home")
            ): TextSelector(),
            vol.Required(CONF_ZONE_ENTITY, default=selected_zone): SelectSelector(
                SelectSelectorConfig(options=_zone_options(hass, selected_zone))
            ),
            vol.Optional(
                CONF_MAP_RADIUS,
                description={"suggested_value": defaults.get(CONF_MAP_RADIUS)},
            ): NumberSelector(NumberSelectorConfig(min=1, mode="box", unit_of_measurement="m")),
        }
    )


def _validate_common_input(user_input: dict[str, Any]) -> str | None:
    """Validate settings that selectors cannot express."""
    api_key = str(user_input.get(CONF_API_KEY, "")).strip()
    aisstream_enabled = user_input.get(CONF_AISSTREAM_ENABLED, True)
    local_mqtt_enabled = user_input.get(CONF_LOCAL_MQTT_ENABLED, False)
    aishub_enabled = user_input.get(CONF_AISHUB_ENABLED, False)
    if not aisstream_enabled and not local_mqtt_enabled and not aishub_enabled:
        return "no_sources"
    if aisstream_enabled and (not api_key or set(api_key) == {"*"}):
        return "invalid_api_key"
    topic = str(user_input.get(CONF_LOCAL_MQTT_TOPIC, "")).strip()
    if local_mqtt_enabled and not _valid_mqtt_subscription(topic):
        return "invalid_mqtt_topic"
    if aishub_enabled and not str(user_input.get(CONF_AISHUB_USERNAME, "")).strip():
        return "invalid_aishub_credentials"
    if user_input.get(CONF_SEARXNG_URL) and not _valid_url(user_input[CONF_SEARXNG_URL]):
        return "invalid_url"
    return None


def _valid_mqtt_subscription(topic: str) -> bool:
    """Validate an MQTT subscription topic, including wildcard rules."""
    if not topic or topic.startswith("/") or "//" in topic:
        return False
    levels = topic.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            return False
        if "+" in level and level != "+":
            return False
    return True


def _validate_area(user_input: dict[str, Any], hass: Any) -> str | None:
    """Validate one tracking area."""
    if not str(user_input.get(CONF_AREA_NAME, "")).strip():
        return "invalid_area_name"
    zone_entity = str(user_input.get(CONF_ZONE_ENTITY, ""))
    zone_state = hass.states.get(zone_entity)
    if not zone_entity.startswith("zone.") or zone_state is None:
        return "invalid_zone"
    map_radius = user_input.get(CONF_MAP_RADIUS)
    if map_radius is not None:
        try:
            zone_radius = float(zone_state.attributes["radius"])
        except (KeyError, TypeError, ValueError):
            zone_radius = None
        if zone_radius is not None and float(map_radius) < zone_radius:
            return "map_radius_too_small"
    return None


def _clean_common_input(
    user_input: dict[str, Any], *, include_sources: bool = True
) -> dict[str, Any]:
    """Normalize common settings and remove blank optional secrets."""
    cleaned = dict(user_input)
    cleaned[CONF_SEARXNG_URL] = _normalize_url(cleaned.get(CONF_SEARXNG_URL, ""))
    if include_sources:
        cleaned[CONF_LOCAL_MQTT_TOPIC] = str(
            cleaned.get(CONF_LOCAL_MQTT_TOPIC, "ais-catcher/ais")
        ).strip()
        cleaned[CONF_AISHUB_USERNAME] = str(cleaned.get(CONF_AISHUB_USERNAME, "")).strip()
    if not cleaned.get(CONF_SEARXNG_PASSWORD):
        cleaned.pop(CONF_SEARXNG_PASSWORD, None)
    cleaned[CONF_VESSEL_WATCHLIST] = ",".join(
        item.strip()
        for item in str(cleaned.get(CONF_VESSEL_WATCHLIST, "")).split(",")
        if item.strip()
    )
    return cleaned


# Kept as compatibility helpers for external tooling and old repair flows.
def _data_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the options-compatible common settings schema."""
    return _common_schema(
        defaults,
        api_key_required=False,
        include_area_count=False,
        include_sources=True,
    )


def _validate_input(user_input: dict[str, Any]) -> str | None:
    """Validate common settings for existing callers."""
    return _validate_common_input(user_input)


def _clean_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Clean common settings for existing callers."""
    return _clean_common_input(user_input)


class _AreaFlowMixin:
    """Implement repeated area steps shared by config and options flows."""

    _pending_settings: dict[str, Any]
    _pending_areas: list[dict[str, Any]]
    _area_count: int
    _area_index: int
    _area_defaults: list[dict[str, Any]]

    async def _async_step_area(self, user_input: dict[str, Any] | None = None):
        """Collect one area and continue until the requested count is reached."""
        errors: dict[str, str] = {}
        defaults = self._area_defaults[self._area_index]
        if user_input is not None:
            error = _validate_area(user_input, self.hass)
            if error:
                errors["base"] = error
            else:
                self._pending_areas.append(area_from_form(user_input, self._area_index + 1))
                self._area_index += 1
                if self._area_index >= self._area_count:
                    return self._async_finish_area_flow()
                defaults = self._area_defaults[self._area_index]

        return self.async_show_form(
            step_id="area",
            data_schema=_area_schema(defaults, self.hass),
            description_placeholders={
                "area_number": str(self._area_index + 1),
                "area_count": str(self._area_count),
            },
            errors=errors,
        )


class AisVesselTrackerConfigFlow(_AreaFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AIS Vessel Tracker."""

    VERSION = 4

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Return the options flow for an existing config entry."""
        return AisVesselTrackerOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            area_count = int(user_input.pop(CONF_AREA_COUNT))
            user_input = _clean_common_input(user_input)
            error = _validate_common_input(user_input)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id("ais_vessel_tracker")
                self._abort_if_unique_id_configured()
                self._pending_settings = user_input
                self._pending_areas = []
                self._area_count = area_count
                self._area_index = 0
                self._area_defaults = [{CONF_AREA_NAME: "Home", CONF_ZONE_ENTITY: "zone.home"}] + [
                    {} for _ in range(area_count - 1)
                ]
                return await self._async_step_area()

        return self.async_show_form(
            step_id="user",
            data_schema=_common_schema({CONF_AREA_COUNT: 1}, api_key_required=False),
            errors=errors,
        )

    def _async_finish_area_flow(self):
        """Create the entry after all area forms have been completed."""
        return self.async_create_entry(
            title="AIS Vessel Tracker",
            data={**self._pending_settings, CONF_AREAS: self._pending_areas},
        )

    async def async_step_area(self, user_input: dict[str, Any] | None = None):
        """Handle one initial setup area."""
        return await self._async_step_area(user_input)


class AisVesselTrackerOptionsFlow(_AreaFlowMixin, OptionsFlowWithReload):
    """Handle options for an existing AIS Vessel Tracker entry."""

    def _initialize_pending(self) -> None:
        """Copy current settings into the editable options-flow state."""
        if hasattr(self, "_pending_settings"):
            return
        current = {**self.config_entry.data, **self.config_entry.options}
        self._pending_settings = {
            key: value for key, value in current.items() if key not in {CONF_AREAS, "cache_photos"}
        }
        self._pending_areas = [dict(area) for area in configured_areas(current)]

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Show the options-flow navigation menu."""
        self._initialize_pending()
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "general",
                "aisstream",
                "local_mqtt",
                "aishub",
                "areas",
                "finish",
            ],
        )

    async def async_step_general(self, user_input: dict[str, Any] | None = None):
        """Manage settings shared by all tracking areas."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {
                **self._pending_settings,
                **_clean_common_input(user_input, include_sources=False),
            }
            error = _validate_common_input(candidate)
            if error:
                errors["base"] = error
            else:
                self._pending_settings = candidate
                return await self.async_step_init()

        suggested = dict(self._pending_settings)
        suggested.pop(CONF_AISSTREAM_ENABLED, None)
        suggested.pop(CONF_API_KEY, None)
        suggested.pop(CONF_LOCAL_MQTT_ENABLED, None)
        suggested.pop(CONF_LOCAL_MQTT_TOPIC, None)
        suggested.pop(CONF_AISHUB_ENABLED, None)
        suggested.pop(CONF_AISHUB_USERNAME, None)
        suggested.pop(CONF_SEARXNG_PASSWORD, None)
        return self.async_show_form(
            step_id="general",
            data_schema=self.add_suggested_values_to_schema(
                _common_schema(
                    api_key_required=False,
                    include_area_count=False,
                    include_sources=False,
                ),
                suggested,
            ),
            errors=errors,
        )

    async def async_step_aisstream(self, user_input: dict[str, Any] | None = None):
        """Manage the AISStream source settings."""
        self._initialize_pending()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._pending_settings, **user_input}
            api_key = str(user_input.get(CONF_API_KEY, "")).strip()
            candidate[CONF_API_KEY] = api_key or self._pending_settings.get(CONF_API_KEY, "")
            error = _validate_common_input(candidate)
            if error:
                errors["base"] = error
            else:
                self._pending_settings = candidate
                return await self.async_step_init()
        suggested = dict(self._pending_settings)
        suggested.pop(CONF_API_KEY, None)
        return self.async_show_form(
            step_id="aisstream",
            data_schema=self.add_suggested_values_to_schema(
                _aisstream_schema(suggested), suggested
            ),
            errors=errors,
        )

    async def async_step_local_mqtt(self, user_input: dict[str, Any] | None = None):
        """Manage the local AIS-catcher MQTT source settings."""
        self._initialize_pending()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._pending_settings, **user_input}
            if CONF_LOCAL_MQTT_TOPIC in user_input:
                candidate[CONF_LOCAL_MQTT_TOPIC] = str(user_input[CONF_LOCAL_MQTT_TOPIC]).strip()
            error = _validate_common_input(candidate)
            if error:
                errors["base"] = error
            else:
                self._pending_settings = candidate
                return await self.async_step_init()
        suggested = dict(self._pending_settings)
        return self.async_show_form(
            step_id="local_mqtt",
            data_schema=self.add_suggested_values_to_schema(
                _local_mqtt_schema(suggested), suggested
            ),
            errors=errors,
        )

    async def async_step_aishub(self, user_input: dict[str, Any] | None = None):
        """Manage the AISHub source settings."""
        self._initialize_pending()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._pending_settings, **user_input}
            username = str(user_input.get(CONF_AISHUB_USERNAME, "")).strip()
            candidate[CONF_AISHUB_USERNAME] = username or self._pending_settings.get(
                CONF_AISHUB_USERNAME, ""
            )
            error = _validate_common_input(candidate)
            if error:
                errors["base"] = error
            else:
                self._pending_settings = candidate
                return await self.async_step_init()
        suggested = dict(self._pending_settings)
        suggested.pop(CONF_AISHUB_USERNAME, None)
        return self.async_show_form(
            step_id="aishub",
            data_schema=self.add_suggested_values_to_schema(_aishub_schema(suggested), suggested),
            errors=errors,
        )

    async def async_step_areas(self, user_input: dict[str, Any] | None = None):
        """Choose an area to edit, remove, or add."""
        self._initialize_pending()
        if user_input is not None:
            action = str(user_input["area_action"])
            if action == "back":
                return await self.async_step_init()
            if action == "add":
                self._area_index = None
                return await self.async_step_area()
            self._area_index = int(action.split(":", 1)[1])
            if action.startswith("remove:"):
                return await self.async_step_remove_area()
            return await self.async_step_area()

        options = [
            {"value": "add", "label": "Add tracking area"},
            *[
                {"value": f"edit:{index}", "label": f"Edit {area['name']}"}
                for index, area in enumerate(self._pending_areas)
            ],
            *[
                {"value": f"remove:{index}", "label": f"Remove {area['name']}"}
                for index, area in enumerate(self._pending_areas)
            ],
            {"value": "back", "label": "Back to settings"},
        ]
        return self.async_show_form(
            step_id="areas",
            data_schema=vol.Schema(
                {vol.Required("area_action"): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def async_step_area(self, user_input: dict[str, Any] | None = None):
        """Add or edit one tracking area."""
        errors: dict[str, str] = {}
        is_new = self._area_index is None
        if is_new:
            defaults = {
                CONF_AREA_NAME: f"Area {len(self._pending_areas) + 1}",
                CONF_ZONE_ENTITY: "zone.home",
            }
        else:
            defaults = area_form_defaults(self._pending_areas[self._area_index])
        if user_input is not None:
            error = _validate_area(user_input, self.hass)
            if error:
                errors["base"] = error
            else:
                area = area_from_form(
                    user_input,
                    self._area_index + 1
                    if self._area_index is not None
                    else len(self._pending_areas) + 1,
                )
                if is_new:
                    self._pending_areas.append(area)
                else:
                    area["id"] = self._pending_areas[self._area_index].get("id", area["id"])
                    self._pending_areas[self._area_index] = area
                return await self.async_step_areas()
        return self.async_show_form(
            step_id="area",
            data_schema=_area_schema(defaults, self.hass),
            description_placeholders={"area_name": str(defaults.get(CONF_AREA_NAME, ""))},
            errors=errors,
        )

    async def async_step_remove_area(self, user_input: dict[str, Any] | None = None):
        """Confirm removal of one tracking area."""
        errors: dict[str, str] = {}
        area = self._pending_areas[self._area_index]
        if user_input is not None:
            if len(self._pending_areas) == 1:
                errors["base"] = "cannot_remove_last_area"
            elif user_input.get("confirm_remove"):
                self._pending_areas.pop(self._area_index)
                return await self.async_step_areas()
        return self.async_show_form(
            step_id="remove_area",
            data_schema=vol.Schema(
                {vol.Required("confirm_remove", default=False): BooleanSelector()}
            ),
            description_placeholders={"area_name": str(area.get("name", ""))},
            errors=errors,
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None):
        """Save the edited settings and areas."""
        del user_input
        return self.async_create_entry(
            title="",
            data={**self._pending_settings, CONF_AREAS: self._pending_areas},
        )
