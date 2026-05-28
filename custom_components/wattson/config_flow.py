"""Config flow for Wattson."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .config import entry_value
from .const import (
    BATTERY_MODES,
    CONF_ALLOW_GRID_CHARGE,
    CONF_ALLOW_NEGATIVE_EXPORT,
    CONF_BATTERY_CHARGE_CURRENT_NUMBER,
    CONF_BATTERY_CONTROL_ENABLED,
    CONF_BATTERY_DISCHARGE_CURRENT_NUMBER,
    CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MODE_DEFAULT,
    CONF_BUY_PRICE_ENTITY,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_EASEE_DEVICE_ID,
    CONF_EASEE_ENABLE_SWITCH,
    CONF_EASEE_ONLINE_ENTITY,
    CONF_EASEE_PHASE_MODE_ENTITY,
    CONF_EASEE_POWER_ENTITY,
    CONF_EASEE_SESSION_ENTITY,
    CONF_EASEE_STATUS_ENTITY,
    CONF_ENERGY_PRIORITY_SELECT,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_MAX_AMPS,
    CONF_EV_MODE_DEFAULT,
    CONF_EV_SOLAR_MIN_SURPLUS_W,
    CONF_EV_WINDOWS,
    CONF_EXPENSIVE_PRICE_THRESHOLD,
    CONF_EXPORT_LIMIT_NUMBER,
    CONF_FORECAST_TODAY_ENTITY,
    CONF_GRID_CHARGE_SWITCH,
    CONF_GRID_POWER_ENTITY,
    CONF_INVERTER_ONLINE_ENTITY,
    CONF_INVERTER_STATUS_ENTITY,
    CONF_INVERT_BATTERY_POWER_SIGN,
    CONF_INVERT_GRID_POWER_SIGN,
    CONF_LIMIT_CONTROL_MODE_SELECT,
    CONF_LOAD_POWER_ENTITY,
    CONF_NAME,
    CONF_PV1_POWER_ENTITY,
    CONF_PV2_POWER_ENTITY,
    CONF_SELL_PRICE_ENTITY,
    CONF_SHADOW_MODE,
    CONF_SOLAR_SELL_SWITCH,
    CONF_STALE_SECONDS,
    CONF_TOU_ENABLE_SWITCH,
    DEFAULT_ALLOW_GRID_CHARGE,
    DEFAULT_ALLOW_NEGATIVE_EXPORT,
    DEFAULT_BATTERY_CONTROL_ENABLED,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_BATTERY_MODE,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_EV_CONTROL_ENABLED,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MODE,
    DEFAULT_EV_SOLAR_MIN_SURPLUS_W,
    DEFAULT_EV_WINDOWS,
    DEFAULT_EXPENSIVE_PRICE_THRESHOLD,
    DEFAULT_NAME,
    DEFAULT_INVERT_BATTERY_POWER_SIGN,
    DEFAULT_INVERT_GRID_POWER_SIGN,
    DEFAULT_SHADOW_MODE,
    DEFAULT_STALE_SECONDS,
    DOMAIN,
    EV_MODES,
)
from .mapping import suggested_mapping


def _entity(domain: str) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain))


def _build_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): TextSelector(TextSelectorConfig()),
            vol.Required(CONF_SHADOW_MODE, default=defaults.get(CONF_SHADOW_MODE, DEFAULT_SHADOW_MODE)): BooleanSelector(),
            vol.Required(CONF_PV1_POWER_ENTITY, default=defaults.get(CONF_PV1_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_PV2_POWER_ENTITY, default=defaults.get(CONF_PV2_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_LOAD_POWER_ENTITY, default=defaults.get(CONF_LOAD_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_GRID_POWER_ENTITY, default=defaults.get(CONF_GRID_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_BATTERY_SOC_ENTITY, default=defaults.get(CONF_BATTERY_SOC_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_BATTERY_POWER_ENTITY, default=defaults.get(CONF_BATTERY_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_INVERTER_ONLINE_ENTITY, default=defaults.get(CONF_INVERTER_ONLINE_ENTITY, "")): _entity("binary_sensor"),
            vol.Optional(CONF_INVERTER_STATUS_ENTITY, default=defaults.get(CONF_INVERTER_STATUS_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_GRID_CHARGE_SWITCH, default=defaults.get(CONF_GRID_CHARGE_SWITCH, "")): _entity("switch"),
            vol.Optional(CONF_SOLAR_SELL_SWITCH, default=defaults.get(CONF_SOLAR_SELL_SWITCH, "")): _entity("switch"),
            vol.Optional(CONF_TOU_ENABLE_SWITCH, default=defaults.get(CONF_TOU_ENABLE_SWITCH, "")): _entity("switch"),
            vol.Optional(CONF_ENERGY_PRIORITY_SELECT, default=defaults.get(CONF_ENERGY_PRIORITY_SELECT, "")): _entity("select"),
            vol.Optional(CONF_LIMIT_CONTROL_MODE_SELECT, default=defaults.get(CONF_LIMIT_CONTROL_MODE_SELECT, "")): _entity("select"),
            vol.Optional(CONF_BATTERY_CHARGE_CURRENT_NUMBER, default=defaults.get(CONF_BATTERY_CHARGE_CURRENT_NUMBER, "")): _entity("number"),
            vol.Optional(CONF_BATTERY_DISCHARGE_CURRENT_NUMBER, default=defaults.get(CONF_BATTERY_DISCHARGE_CURRENT_NUMBER, "")): _entity("number"),
            vol.Optional(CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER, default=defaults.get(CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER, "")): _entity("number"),
            vol.Optional(CONF_EXPORT_LIMIT_NUMBER, default=defaults.get(CONF_EXPORT_LIMIT_NUMBER, "")): _entity("number"),
            vol.Required(CONF_EASEE_DEVICE_ID, default=defaults.get(CONF_EASEE_DEVICE_ID, "")): TextSelector(TextSelectorConfig()),
            vol.Required(CONF_EASEE_ENABLE_SWITCH, default=defaults.get(CONF_EASEE_ENABLE_SWITCH, "")): _entity("switch"),
            vol.Required(CONF_EASEE_STATUS_ENTITY, default=defaults.get(CONF_EASEE_STATUS_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_EASEE_POWER_ENTITY, default=defaults.get(CONF_EASEE_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_EASEE_SESSION_ENTITY, default=defaults.get(CONF_EASEE_SESSION_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_EASEE_PHASE_MODE_ENTITY, default=defaults.get(CONF_EASEE_PHASE_MODE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_EASEE_ONLINE_ENTITY, default=defaults.get(CONF_EASEE_ONLINE_ENTITY, "")): _entity("binary_sensor"),
            vol.Optional(CONF_BUY_PRICE_ENTITY, default=defaults.get(CONF_BUY_PRICE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_SELL_PRICE_ENTITY, default=defaults.get(CONF_SELL_PRICE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_FORECAST_TODAY_ENTITY, default=defaults.get(CONF_FORECAST_TODAY_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_STALE_SECONDS, default=defaults.get(CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)): NumberSelector(NumberSelectorConfig(min=30, max=3600, step=10, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_INVERT_GRID_POWER_SIGN, default=defaults.get(CONF_INVERT_GRID_POWER_SIGN, DEFAULT_INVERT_GRID_POWER_SIGN)): BooleanSelector(),
            vol.Required(CONF_INVERT_BATTERY_POWER_SIGN, default=defaults.get(CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN)): BooleanSelector(),
            vol.Required(CONF_BATTERY_MIN_SOC, default=defaults.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)): NumberSelector(NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_BATTERY_MAX_SOC, default=defaults.get(CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)): NumberSelector(NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_CHEAP_PRICE_THRESHOLD, default=defaults.get(CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD)): NumberSelector(NumberSelectorConfig(min=-5, max=10, step=0.05, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_EXPENSIVE_PRICE_THRESHOLD, default=defaults.get(CONF_EXPENSIVE_PRICE_THRESHOLD, DEFAULT_EXPENSIVE_PRICE_THRESHOLD)): NumberSelector(NumberSelectorConfig(min=-5, max=20, step=0.05, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_ALLOW_GRID_CHARGE, default=defaults.get(CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE)): BooleanSelector(),
            vol.Required(CONF_ALLOW_NEGATIVE_EXPORT, default=defaults.get(CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT)): BooleanSelector(),
            vol.Required(CONF_EV_CONTROL_ENABLED, default=defaults.get(CONF_EV_CONTROL_ENABLED, DEFAULT_EV_CONTROL_ENABLED)): BooleanSelector(),
            vol.Required(CONF_BATTERY_CONTROL_ENABLED, default=defaults.get(CONF_BATTERY_CONTROL_ENABLED, DEFAULT_BATTERY_CONTROL_ENABLED)): BooleanSelector(),
            vol.Required(CONF_EV_MODE_DEFAULT, default=defaults.get(CONF_EV_MODE_DEFAULT, DEFAULT_EV_MODE)): SelectSelector(SelectSelectorConfig(options=EV_MODES)),
            vol.Required(CONF_BATTERY_MODE_DEFAULT, default=defaults.get(CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE)): SelectSelector(SelectSelectorConfig(options=BATTERY_MODES)),
            vol.Required(CONF_EV_MAX_AMPS, default=defaults.get(CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS)): NumberSelector(NumberSelectorConfig(min=6, max=32, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_EV_SOLAR_MIN_SURPLUS_W, default=defaults.get(CONF_EV_SOLAR_MIN_SURPLUS_W, DEFAULT_EV_SOLAR_MIN_SURPLUS_W)): NumberSelector(NumberSelectorConfig(min=500, max=20000, step=100, mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_EV_WINDOWS, default=defaults.get(CONF_EV_WINDOWS, DEFAULT_EV_WINDOWS)): TextSelector(TextSelectorConfig()),
        }
    )


class WattsonConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        defaults = {**suggested_mapping(self.hass), CONF_NAME: DEFAULT_NAME}
        return self.async_show_form(step_id="user", data_schema=_build_schema(defaults))

    @staticmethod
    def async_get_options_flow(config_entry):
        return WattsonOptionsFlow(config_entry)


class WattsonOptionsFlow(OptionsFlow):
    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = {
            key: entry_value(self.config_entry, key, default)
            for key, default in {
                CONF_SHADOW_MODE: DEFAULT_SHADOW_MODE,
                CONF_STALE_SECONDS: DEFAULT_STALE_SECONDS,
                CONF_INVERT_GRID_POWER_SIGN: DEFAULT_INVERT_GRID_POWER_SIGN,
                CONF_INVERT_BATTERY_POWER_SIGN: DEFAULT_INVERT_BATTERY_POWER_SIGN,
                CONF_BATTERY_MIN_SOC: DEFAULT_BATTERY_MIN_SOC,
                CONF_BATTERY_MAX_SOC: DEFAULT_BATTERY_MAX_SOC,
                CONF_CHEAP_PRICE_THRESHOLD: DEFAULT_CHEAP_PRICE_THRESHOLD,
                CONF_EXPENSIVE_PRICE_THRESHOLD: DEFAULT_EXPENSIVE_PRICE_THRESHOLD,
                CONF_ALLOW_GRID_CHARGE: DEFAULT_ALLOW_GRID_CHARGE,
                CONF_ALLOW_NEGATIVE_EXPORT: DEFAULT_ALLOW_NEGATIVE_EXPORT,
                CONF_EV_CONTROL_ENABLED: DEFAULT_EV_CONTROL_ENABLED,
                CONF_BATTERY_CONTROL_ENABLED: DEFAULT_BATTERY_CONTROL_ENABLED,
                CONF_EV_MODE_DEFAULT: DEFAULT_EV_MODE,
                CONF_BATTERY_MODE_DEFAULT: DEFAULT_BATTERY_MODE,
                CONF_EV_MAX_AMPS: DEFAULT_EV_MAX_AMPS,
                CONF_EV_SOLAR_MIN_SURPLUS_W: DEFAULT_EV_SOLAR_MIN_SURPLUS_W,
                CONF_EV_WINDOWS: DEFAULT_EV_WINDOWS,
            }.items()
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_SHADOW_MODE, default=defaults[CONF_SHADOW_MODE]): BooleanSelector(),
                vol.Required(CONF_STALE_SECONDS, default=defaults[CONF_STALE_SECONDS]): NumberSelector(NumberSelectorConfig(min=30, max=3600, step=10, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_INVERT_GRID_POWER_SIGN, default=defaults[CONF_INVERT_GRID_POWER_SIGN]): BooleanSelector(),
                vol.Required(CONF_INVERT_BATTERY_POWER_SIGN, default=defaults[CONF_INVERT_BATTERY_POWER_SIGN]): BooleanSelector(),
                vol.Required(CONF_BATTERY_MIN_SOC, default=defaults[CONF_BATTERY_MIN_SOC]): NumberSelector(NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_BATTERY_MAX_SOC, default=defaults[CONF_BATTERY_MAX_SOC]): NumberSelector(NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_CHEAP_PRICE_THRESHOLD, default=defaults[CONF_CHEAP_PRICE_THRESHOLD]): NumberSelector(NumberSelectorConfig(min=-5, max=10, step=0.05, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_EXPENSIVE_PRICE_THRESHOLD, default=defaults[CONF_EXPENSIVE_PRICE_THRESHOLD]): NumberSelector(NumberSelectorConfig(min=-5, max=20, step=0.05, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_ALLOW_GRID_CHARGE, default=defaults[CONF_ALLOW_GRID_CHARGE]): BooleanSelector(),
                vol.Required(CONF_ALLOW_NEGATIVE_EXPORT, default=defaults[CONF_ALLOW_NEGATIVE_EXPORT]): BooleanSelector(),
                vol.Required(CONF_EV_CONTROL_ENABLED, default=defaults[CONF_EV_CONTROL_ENABLED]): BooleanSelector(),
                vol.Required(CONF_BATTERY_CONTROL_ENABLED, default=defaults[CONF_BATTERY_CONTROL_ENABLED]): BooleanSelector(),
                vol.Required(CONF_EV_MODE_DEFAULT, default=defaults[CONF_EV_MODE_DEFAULT]): SelectSelector(SelectSelectorConfig(options=EV_MODES)),
                vol.Required(CONF_BATTERY_MODE_DEFAULT, default=defaults[CONF_BATTERY_MODE_DEFAULT]): SelectSelector(SelectSelectorConfig(options=BATTERY_MODES)),
                vol.Required(CONF_EV_MAX_AMPS, default=defaults[CONF_EV_MAX_AMPS]): NumberSelector(NumberSelectorConfig(min=6, max=32, step=1, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_EV_SOLAR_MIN_SURPLUS_W, default=defaults[CONF_EV_SOLAR_MIN_SURPLUS_W]): NumberSelector(NumberSelectorConfig(min=500, max=20000, step=100, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_EV_WINDOWS, default=defaults[CONF_EV_WINDOWS]): TextSelector(TextSelectorConfig()),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
