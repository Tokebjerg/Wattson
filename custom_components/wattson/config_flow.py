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
    CONF_BATTERY_CAPACITY_KWH,
    CONF_PRICE_VAT_MULTIPLIER,
    DEFAULT_PRICE_VAT_MULTIPLIER,
    CONF_SOLAR_CHARGE_PRIORITY_SOC,
    DEFAULT_SOLAR_CHARGE_PRIORITY_SOC,
    CONF_EV_REQUIRED_HOURS,
    CONF_EV_SOC_ENTITY,
    DEFAULT_EV_SOC_ENTITY,
    CONF_EV_CHARGE_SPEED_PCT_H,
    DEFAULT_EV_CHARGE_SPEED_PCT_H,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MODE_DEFAULT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
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
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_EV_REQUIRED_HOURS,
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

CONF_CONFIRM_SINGLE_CONTROLLER = "confirm_single_controller"


def _entity(domain: str) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain))


def _text() -> TextSelector:
    return TextSelector(TextSelectorConfig())


def _bool() -> BooleanSelector:
    return BooleanSelector()


def _number(
    min_value: float,
    max_value: float,
    step: float,
) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=step,
            mode=NumberSelectorMode.BOX,
        )
    )


def _select(options: list[str]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=options))


def _step_profile_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME,
                default=defaults.get(CONF_NAME, DEFAULT_NAME),
            ): _text(),
            vol.Required(
                CONF_SHADOW_MODE,
                default=defaults.get(CONF_SHADOW_MODE, DEFAULT_SHADOW_MODE),
            ): _bool(),
            vol.Required(CONF_CONFIRM_SINGLE_CONTROLLER, default=False): _bool(),
        }
    )


def _step_inverter_read_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PV1_POWER_ENTITY, default=defaults.get(CONF_PV1_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_PV2_POWER_ENTITY, default=defaults.get(CONF_PV2_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_LOAD_POWER_ENTITY, default=defaults.get(CONF_LOAD_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_GRID_POWER_ENTITY, default=defaults.get(CONF_GRID_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_BATTERY_SOC_ENTITY, default=defaults.get(CONF_BATTERY_SOC_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_BATTERY_POWER_ENTITY, default=defaults.get(CONF_BATTERY_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_INVERTER_ONLINE_ENTITY, default=defaults.get(CONF_INVERTER_ONLINE_ENTITY, "")): _entity("binary_sensor"),
            vol.Optional(CONF_INVERTER_STATUS_ENTITY, default=defaults.get(CONF_INVERTER_STATUS_ENTITY, "")): _entity("sensor"),
        }
    )


def _step_inverter_control_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_GRID_CHARGE_SWITCH, default=defaults.get(CONF_GRID_CHARGE_SWITCH, "")): _entity("switch"),
            vol.Optional(CONF_SOLAR_SELL_SWITCH, default=defaults.get(CONF_SOLAR_SELL_SWITCH, "")): _entity("switch"),
            vol.Optional(CONF_TOU_ENABLE_SWITCH, default=defaults.get(CONF_TOU_ENABLE_SWITCH, "")): _entity("switch"),
            vol.Optional(CONF_ENERGY_PRIORITY_SELECT, default=defaults.get(CONF_ENERGY_PRIORITY_SELECT, "")): _entity("select"),
            vol.Optional(CONF_LIMIT_CONTROL_MODE_SELECT, default=defaults.get(CONF_LIMIT_CONTROL_MODE_SELECT, "")): _entity("select"),
            vol.Optional(CONF_BATTERY_CHARGE_CURRENT_NUMBER, default=defaults.get(CONF_BATTERY_CHARGE_CURRENT_NUMBER, "")): _entity("number"),
            vol.Optional(CONF_BATTERY_DISCHARGE_CURRENT_NUMBER, default=defaults.get(CONF_BATTERY_DISCHARGE_CURRENT_NUMBER, "")): _entity("number"),
            vol.Optional(CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER, default=defaults.get(CONF_BATTERY_GRID_CHARGE_CURRENT_NUMBER, "")): _entity("number"),
            vol.Optional(CONF_EXPORT_LIMIT_NUMBER, default=defaults.get(CONF_EXPORT_LIMIT_NUMBER, "")): _entity("number"),
        }
    )


def _step_ev_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EASEE_DEVICE_ID, default=defaults.get(CONF_EASEE_DEVICE_ID, "")): _text(),
            vol.Required(CONF_EASEE_ENABLE_SWITCH, default=defaults.get(CONF_EASEE_ENABLE_SWITCH, "")): _entity("switch"),
            vol.Required(CONF_EASEE_STATUS_ENTITY, default=defaults.get(CONF_EASEE_STATUS_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_EASEE_POWER_ENTITY, default=defaults.get(CONF_EASEE_POWER_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_EASEE_SESSION_ENTITY, default=defaults.get(CONF_EASEE_SESSION_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_EASEE_PHASE_MODE_ENTITY, default=defaults.get(CONF_EASEE_PHASE_MODE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_EASEE_ONLINE_ENTITY, default=defaults.get(CONF_EASEE_ONLINE_ENTITY, "")): _entity("binary_sensor"),
            vol.Required(CONF_EV_CONTROL_ENABLED, default=defaults.get(CONF_EV_CONTROL_ENABLED, DEFAULT_EV_CONTROL_ENABLED)): _bool(),
        }
    )


def _step_price_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_BUY_PRICE_ENTITY, default=defaults.get(CONF_BUY_PRICE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_SELL_PRICE_ENTITY, default=defaults.get(CONF_SELL_PRICE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_FORECAST_TODAY_ENTITY, default=defaults.get(CONF_FORECAST_TODAY_ENTITY, "")): _entity("sensor"),
            vol.Required(
                CONF_CHEAP_PRICE_THRESHOLD,
                default=defaults.get(CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD),
            ): _number(-5, 10, 0.05),
            vol.Required(
                CONF_EXPENSIVE_PRICE_THRESHOLD,
                default=defaults.get(CONF_EXPENSIVE_PRICE_THRESHOLD, DEFAULT_EXPENSIVE_PRICE_THRESHOLD),
            ): _number(-5, 20, 0.05),
        }
    )


def _step_safety_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_STALE_SECONDS, default=defaults.get(CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)): _number(30, 3600, 10),
            vol.Required(CONF_INVERT_GRID_POWER_SIGN, default=defaults.get(CONF_INVERT_GRID_POWER_SIGN, DEFAULT_INVERT_GRID_POWER_SIGN)): _bool(),
            vol.Required(CONF_INVERT_BATTERY_POWER_SIGN, default=defaults.get(CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN)): _bool(),
            vol.Required(CONF_BATTERY_CONTROL_ENABLED, default=defaults.get(CONF_BATTERY_CONTROL_ENABLED, DEFAULT_BATTERY_CONTROL_ENABLED)): _bool(),
            vol.Required(CONF_BATTERY_MIN_SOC, default=defaults.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)): _number(0, 100, 1),
            vol.Required(CONF_BATTERY_MAX_SOC, default=defaults.get(CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)): _number(0, 100, 1),
            vol.Required(CONF_ALLOW_GRID_CHARGE, default=defaults.get(CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE)): _bool(),
            vol.Required(CONF_ALLOW_NEGATIVE_EXPORT, default=defaults.get(CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT)): _bool(),
            vol.Required(CONF_EV_MODE_DEFAULT, default=defaults.get(CONF_EV_MODE_DEFAULT, DEFAULT_EV_MODE)): _select(EV_MODES),
            vol.Required(CONF_BATTERY_MODE_DEFAULT, default=defaults.get(CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE)): _select(BATTERY_MODES),
            vol.Required(CONF_EV_MAX_AMPS, default=defaults.get(CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS)): _number(6, 32, 1),
            vol.Required(CONF_EV_SOLAR_MIN_SURPLUS_W, default=defaults.get(CONF_EV_SOLAR_MIN_SURPLUS_W, DEFAULT_EV_SOLAR_MIN_SURPLUS_W)): _number(500, 20000, 100),
            vol.Required(CONF_EV_WINDOWS, default=defaults.get(CONF_EV_WINDOWS, DEFAULT_EV_WINDOWS)): _text(),
        }
    )


def _options_runtime_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SHADOW_MODE, default=defaults[CONF_SHADOW_MODE]): _bool(),
            vol.Required(CONF_STALE_SECONDS, default=defaults[CONF_STALE_SECONDS]): _number(30, 3600, 10),
            vol.Required(CONF_INVERT_GRID_POWER_SIGN, default=defaults[CONF_INVERT_GRID_POWER_SIGN]): _bool(),
            vol.Required(CONF_INVERT_BATTERY_POWER_SIGN, default=defaults[CONF_INVERT_BATTERY_POWER_SIGN]): _bool(),
        }
    )


def _options_battery_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BATTERY_CONTROL_ENABLED, default=defaults[CONF_BATTERY_CONTROL_ENABLED]): _bool(),
            vol.Required(CONF_BATTERY_MIN_SOC, default=defaults[CONF_BATTERY_MIN_SOC]): _number(0, 100, 1),
            vol.Required(CONF_BATTERY_MAX_SOC, default=defaults[CONF_BATTERY_MAX_SOC]): _number(0, 100, 1),
            vol.Required(CONF_BATTERY_MODE_DEFAULT, default=defaults[CONF_BATTERY_MODE_DEFAULT]): _select(BATTERY_MODES),
            vol.Required(CONF_ALLOW_GRID_CHARGE, default=defaults[CONF_ALLOW_GRID_CHARGE]): _bool(),
            vol.Required(CONF_ALLOW_NEGATIVE_EXPORT, default=defaults[CONF_ALLOW_NEGATIVE_EXPORT]): _bool(),
            vol.Required(CONF_CHEAP_PRICE_THRESHOLD, default=defaults[CONF_CHEAP_PRICE_THRESHOLD]): _number(-5, 10, 0.05),
            vol.Required(CONF_EXPENSIVE_PRICE_THRESHOLD, default=defaults[CONF_EXPENSIVE_PRICE_THRESHOLD]): _number(-5, 20, 0.05),
            vol.Required(CONF_BATTERY_CAPACITY_KWH, default=defaults[CONF_BATTERY_CAPACITY_KWH]): _number(1, 100, 0.5),
            vol.Required(CONF_SOLAR_CHARGE_PRIORITY_SOC, default=defaults[CONF_SOLAR_CHARGE_PRIORITY_SOC]): _number(0, 100, 5),
            vol.Required(CONF_PRICE_VAT_MULTIPLIER, default=defaults[CONF_PRICE_VAT_MULTIPLIER]): _number(1.0, 2.0, 0.05),
        }
    )


def _options_ev_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EV_CONTROL_ENABLED, default=defaults[CONF_EV_CONTROL_ENABLED]): _bool(),
            vol.Required(CONF_EV_MODE_DEFAULT, default=defaults[CONF_EV_MODE_DEFAULT]): _select(EV_MODES),
            vol.Required(CONF_EV_MAX_AMPS, default=defaults[CONF_EV_MAX_AMPS]): _number(6, 32, 1),
            vol.Required(CONF_EV_SOLAR_MIN_SURPLUS_W, default=defaults[CONF_EV_SOLAR_MIN_SURPLUS_W]): _number(500, 20000, 100),
            vol.Required(CONF_EV_REQUIRED_HOURS, default=defaults[CONF_EV_REQUIRED_HOURS]): _number(1, 12, 1),
            vol.Optional(CONF_EV_SOC_ENTITY, default=defaults.get(CONF_EV_SOC_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_EV_CHARGE_SPEED_PCT_H, default=defaults.get(CONF_EV_CHARGE_SPEED_PCT_H, DEFAULT_EV_CHARGE_SPEED_PCT_H)): _number(5, 60, 0.5),
            vol.Required(CONF_EV_WINDOWS, default=defaults[CONF_EV_WINDOWS]): _text(),
        }
    )


def _options_mapping_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PV1_POWER_ENTITY, default=defaults[CONF_PV1_POWER_ENTITY]): _entity("sensor"),
            vol.Required(CONF_PV2_POWER_ENTITY, default=defaults[CONF_PV2_POWER_ENTITY]): _entity("sensor"),
            vol.Required(CONF_LOAD_POWER_ENTITY, default=defaults[CONF_LOAD_POWER_ENTITY]): _entity("sensor"),
            vol.Required(CONF_GRID_POWER_ENTITY, default=defaults[CONF_GRID_POWER_ENTITY]): _entity("sensor"),
            vol.Required(CONF_BATTERY_SOC_ENTITY, default=defaults[CONF_BATTERY_SOC_ENTITY]): _entity("sensor"),
            vol.Required(CONF_BATTERY_POWER_ENTITY, default=defaults[CONF_BATTERY_POWER_ENTITY]): _entity("sensor"),
            vol.Required(CONF_INVERTER_ONLINE_ENTITY, default=defaults[CONF_INVERTER_ONLINE_ENTITY]): _entity("binary_sensor"),
            vol.Optional(CONF_INVERTER_STATUS_ENTITY, default=defaults.get(CONF_INVERTER_STATUS_ENTITY, "")): _entity("sensor"),
            vol.Required(CONF_EASEE_DEVICE_ID, default=defaults[CONF_EASEE_DEVICE_ID]): _text(),
            vol.Required(CONF_EASEE_ENABLE_SWITCH, default=defaults[CONF_EASEE_ENABLE_SWITCH]): _entity("switch"),
            vol.Required(CONF_EASEE_STATUS_ENTITY, default=defaults[CONF_EASEE_STATUS_ENTITY]): _entity("sensor"),
            vol.Required(CONF_EASEE_POWER_ENTITY, default=defaults[CONF_EASEE_POWER_ENTITY]): _entity("sensor"),
            vol.Required(CONF_EASEE_SESSION_ENTITY, default=defaults[CONF_EASEE_SESSION_ENTITY]): _entity("sensor"),
            vol.Optional(CONF_EASEE_PHASE_MODE_ENTITY, default=defaults.get(CONF_EASEE_PHASE_MODE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_EASEE_ONLINE_ENTITY, default=defaults.get(CONF_EASEE_ONLINE_ENTITY, "")): _entity("binary_sensor"),
            vol.Optional(CONF_BUY_PRICE_ENTITY, default=defaults.get(CONF_BUY_PRICE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_SELL_PRICE_ENTITY, default=defaults.get(CONF_SELL_PRICE_ENTITY, "")): _entity("sensor"),
            vol.Optional(CONF_FORECAST_TODAY_ENTITY, default=defaults.get(CONF_FORECAST_TODAY_ENTITY, "")): _entity("sensor"),
        }
    )


def _merge(defaults: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(updates)
    return merged


class WattsonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Wattson config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._defaults: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Initial profile step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_CONFIRM_SINGLE_CONTROLLER):
                errors[CONF_CONFIRM_SINGLE_CONTROLLER] = "confirm_single_controller"
            else:
                self._data = dict(user_input)
                self._data.pop(CONF_CONFIRM_SINGLE_CONTROLLER, None)
                return await self.async_step_inverter_read()

        self._defaults = {
            **suggested_mapping(self.hass),
            CONF_NAME: DEFAULT_NAME,
            CONF_SHADOW_MODE: DEFAULT_SHADOW_MODE,
        }
        return self.async_show_form(
            step_id="user",
            data_schema=_step_profile_schema(self._defaults),
            errors=errors,
            description_placeholders={
                "inverter_hint": self._defaults.get(CONF_INVERTER_STATUS_ENTITY, "sensor.klatremishw_deye_running_status"),
                "charger_hint": self._defaults.get(CONF_EASEE_STATUS_ENTITY, "sensor.ehut8c3w_status"),
            },
        )

    async def async_step_inverter_read(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Map inverter read entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_inverter_control()

        defaults = _merge(self._defaults, self._data)
        return self.async_show_form(
            step_id="inverter_read",
            data_schema=_step_inverter_read_schema(defaults),
            description_placeholders={
                "device_name": "Klatremishw / Deye",
            },
        )

    async def async_step_inverter_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Map inverter write entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_charger()

        defaults = _merge(self._defaults, self._data)
        return self.async_show_form(
            step_id="inverter_control",
            data_schema=_step_inverter_control_schema(defaults),
            description_placeholders={
                "write_path_hint": defaults.get(CONF_GRID_CHARGE_SWITCH, "switch.klatremishw_deye_grid_charge"),
            },
        )

    async def async_step_charger(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Map Easee EV charger entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_pricing()

        defaults = _merge(self._defaults, self._data)
        return self.async_show_form(
            step_id="charger",
            data_schema=_step_ev_schema(defaults),
            description_placeholders={
                "charger_name": "Easee Charge Lite",
                "device_id_hint": defaults.get(CONF_EASEE_DEVICE_ID, ""),
            },
        )

    async def async_step_pricing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure price and forecast entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_safety()

        defaults = _merge(self._defaults, self._data)
        return self.async_show_form(
            step_id="pricing",
            data_schema=_step_price_schema(defaults),
        )

    async def async_step_safety(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure safety and operating defaults."""
        if user_input is not None:
            self._data.update(user_input)
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._data[CONF_NAME],
                data=self._data,
            )

        defaults = _merge(self._defaults, self._data)
        return self.async_show_form(
            step_id="safety",
            data_schema=_step_safety_schema(defaults),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow."""
        return WattsonOptionsFlow()


class WattsonOptionsFlow(OptionsFlow):
    """Handle Wattson options."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    def _defaults(self) -> dict[str, Any]:
        return {
            CONF_SHADOW_MODE: entry_value(self.config_entry, CONF_SHADOW_MODE, DEFAULT_SHADOW_MODE),
            CONF_STALE_SECONDS: entry_value(self.config_entry, CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS),
            CONF_INVERT_GRID_POWER_SIGN: entry_value(self.config_entry, CONF_INVERT_GRID_POWER_SIGN, DEFAULT_INVERT_GRID_POWER_SIGN),
            CONF_INVERT_BATTERY_POWER_SIGN: entry_value(self.config_entry, CONF_INVERT_BATTERY_POWER_SIGN, DEFAULT_INVERT_BATTERY_POWER_SIGN),
            CONF_BATTERY_CONTROL_ENABLED: entry_value(self.config_entry, CONF_BATTERY_CONTROL_ENABLED, DEFAULT_BATTERY_CONTROL_ENABLED),
            CONF_BATTERY_MIN_SOC: entry_value(self.config_entry, CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC),
            CONF_BATTERY_MAX_SOC: entry_value(self.config_entry, CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC),
            CONF_BATTERY_MODE_DEFAULT: entry_value(self.config_entry, CONF_BATTERY_MODE_DEFAULT, DEFAULT_BATTERY_MODE),
            CONF_ALLOW_GRID_CHARGE: entry_value(self.config_entry, CONF_ALLOW_GRID_CHARGE, DEFAULT_ALLOW_GRID_CHARGE),
            CONF_ALLOW_NEGATIVE_EXPORT: entry_value(self.config_entry, CONF_ALLOW_NEGATIVE_EXPORT, DEFAULT_ALLOW_NEGATIVE_EXPORT),
            CONF_CHEAP_PRICE_THRESHOLD: entry_value(self.config_entry, CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD),
            CONF_EXPENSIVE_PRICE_THRESHOLD: entry_value(self.config_entry, CONF_EXPENSIVE_PRICE_THRESHOLD, DEFAULT_EXPENSIVE_PRICE_THRESHOLD),
            CONF_BATTERY_CAPACITY_KWH: entry_value(self.config_entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
            CONF_SOLAR_CHARGE_PRIORITY_SOC: entry_value(self.config_entry, CONF_SOLAR_CHARGE_PRIORITY_SOC, DEFAULT_SOLAR_CHARGE_PRIORITY_SOC),
            CONF_PRICE_VAT_MULTIPLIER: entry_value(self.config_entry, CONF_PRICE_VAT_MULTIPLIER, DEFAULT_PRICE_VAT_MULTIPLIER),
            CONF_EV_REQUIRED_HOURS: entry_value(self.config_entry, CONF_EV_REQUIRED_HOURS, DEFAULT_EV_REQUIRED_HOURS),
            CONF_EV_SOC_ENTITY: entry_value(self.config_entry, CONF_EV_SOC_ENTITY, DEFAULT_EV_SOC_ENTITY),
            CONF_EV_CHARGE_SPEED_PCT_H: entry_value(self.config_entry, CONF_EV_CHARGE_SPEED_PCT_H, DEFAULT_EV_CHARGE_SPEED_PCT_H),
            CONF_EV_CONTROL_ENABLED: entry_value(self.config_entry, CONF_EV_CONTROL_ENABLED, DEFAULT_EV_CONTROL_ENABLED),
            CONF_EV_MODE_DEFAULT: entry_value(self.config_entry, CONF_EV_MODE_DEFAULT, DEFAULT_EV_MODE),
            CONF_EV_MAX_AMPS: entry_value(self.config_entry, CONF_EV_MAX_AMPS, DEFAULT_EV_MAX_AMPS),
            CONF_EV_SOLAR_MIN_SURPLUS_W: entry_value(self.config_entry, CONF_EV_SOLAR_MIN_SURPLUS_W, DEFAULT_EV_SOLAR_MIN_SURPLUS_W),
            CONF_EV_WINDOWS: entry_value(self.config_entry, CONF_EV_WINDOWS, DEFAULT_EV_WINDOWS),
            CONF_PV1_POWER_ENTITY: entry_value(self.config_entry, CONF_PV1_POWER_ENTITY, ""),
            CONF_PV2_POWER_ENTITY: entry_value(self.config_entry, CONF_PV2_POWER_ENTITY, ""),
            CONF_LOAD_POWER_ENTITY: entry_value(self.config_entry, CONF_LOAD_POWER_ENTITY, ""),
            CONF_GRID_POWER_ENTITY: entry_value(self.config_entry, CONF_GRID_POWER_ENTITY, ""),
            CONF_BATTERY_SOC_ENTITY: entry_value(self.config_entry, CONF_BATTERY_SOC_ENTITY, ""),
            CONF_BATTERY_POWER_ENTITY: entry_value(self.config_entry, CONF_BATTERY_POWER_ENTITY, ""),
            CONF_INVERTER_ONLINE_ENTITY: entry_value(self.config_entry, CONF_INVERTER_ONLINE_ENTITY, ""),
            CONF_INVERTER_STATUS_ENTITY: entry_value(self.config_entry, CONF_INVERTER_STATUS_ENTITY, ""),
            CONF_EASEE_DEVICE_ID: entry_value(self.config_entry, CONF_EASEE_DEVICE_ID, ""),
            CONF_EASEE_ENABLE_SWITCH: entry_value(self.config_entry, CONF_EASEE_ENABLE_SWITCH, ""),
            CONF_EASEE_STATUS_ENTITY: entry_value(self.config_entry, CONF_EASEE_STATUS_ENTITY, ""),
            CONF_EASEE_POWER_ENTITY: entry_value(self.config_entry, CONF_EASEE_POWER_ENTITY, ""),
            CONF_EASEE_SESSION_ENTITY: entry_value(self.config_entry, CONF_EASEE_SESSION_ENTITY, ""),
            CONF_EASEE_PHASE_MODE_ENTITY: entry_value(self.config_entry, CONF_EASEE_PHASE_MODE_ENTITY, ""),
            CONF_EASEE_ONLINE_ENTITY: entry_value(self.config_entry, CONF_EASEE_ONLINE_ENTITY, ""),
            CONF_BUY_PRICE_ENTITY: entry_value(self.config_entry, CONF_BUY_PRICE_ENTITY, ""),
            CONF_SELL_PRICE_ENTITY: entry_value(self.config_entry, CONF_SELL_PRICE_ENTITY, ""),
            CONF_FORECAST_TODAY_ENTITY: entry_value(self.config_entry, CONF_FORECAST_TODAY_ENTITY, ""),
        }

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["runtime", "battery", "ev_settings", "mapping"],
        )

    async def async_step_runtime(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure runtime and sign handling."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=_merge(self._defaults(), self._options))

        defaults = _merge(self._defaults(), self._options)
        return self.async_show_form(
            step_id="runtime",
            data_schema=_options_runtime_schema(defaults),
        )

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure battery strategy defaults."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=_merge(self._defaults(), self._options))

        defaults = _merge(self._defaults(), self._options)
        return self.async_show_form(
            step_id="battery",
            data_schema=_options_battery_schema(defaults),
        )

    async def async_step_ev_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure EV strategy defaults."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=_merge(self._defaults(), self._options))

        defaults = _merge(self._defaults(), self._options)
        return self.async_show_form(
            step_id="ev_settings",
            data_schema=_options_ev_schema(defaults),
        )

    async def async_step_mapping(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit entity mapping after installation."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=_merge(self._defaults(), self._options))

        defaults = _merge(self._defaults(), self._options)
        return self.async_show_form(
            step_id="mapping",
            data_schema=_options_mapping_schema(defaults),
        )
