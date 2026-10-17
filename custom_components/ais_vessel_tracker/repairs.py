"""Repairs for AIS Vessel Tracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config_flow import _clean_input, _data_schema, _validate_input
from .const import CONF_API_KEY, CONF_AREA_COUNT, CONF_SEARXNG_PASSWORD


class AisVesselTrackerRepairFlow(RepairsFlow):
    """Repair AIS Vessel Tracker configuration."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the repair flow."""
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Start the repair flow."""
        del user_input
        return await self.async_step_configure()

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Handle the repair form."""
        entry: ConfigEntry | None = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        current = {**entry.data, **entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _clean_input(user_input)
            if not user_input.get(CONF_API_KEY):
                user_input[CONF_API_KEY] = current.get(CONF_API_KEY, "")
            if not user_input.get(CONF_SEARXNG_PASSWORD):
                user_input.pop(CONF_SEARXNG_PASSWORD, None)
                if current.get(CONF_SEARXNG_PASSWORD):
                    user_input[CONF_SEARXNG_PASSWORD] = current[CONF_SEARXNG_PASSWORD]
            error = _validate_input(user_input)
            if error:
                errors["base"] = error
            else:
                updated = {**current, **user_input}
                updated.pop(CONF_AREA_COUNT, None)
                self.hass.config_entries.async_update_entry(entry, data=updated, options={})
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(data={})

        suggested = dict(current)
        suggested.pop(CONF_API_KEY, None)
        suggested.pop(CONF_SEARXNG_PASSWORD, None)
        return self.async_show_form(
            step_id="configure",
            data_schema=self.add_suggested_values_to_schema(_data_schema(), suggested),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the configuration repair flow."""
    del hass, issue_id
    return AisVesselTrackerRepairFlow(str((data or {}).get("entry_id", "")))
