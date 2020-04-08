"""Adds support for generic humistat units."""
import asyncio
import logging

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateDevice
from homeassistant.components.climate.const import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_HUMIDITY,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODES,
    ATTR_MAX_HUMIDITY,
    ATTR_MIN_HUMIDITY,
    ATTR_PRESET_MODE,
    CURRENT_HVAC_DRY,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODES,
    HVAC_MODE_DRY,
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_NONE,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_HUMIDITY,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, callback
from homeassistant.helpers import condition
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

HVAC_MODE_HUMIDIFY = "humidify"
HVAC_MODES.append(HVAC_MODE_HUMIDIFY)

CURRENT_HVAC_HUMIDIFY = "humidifying"
ATTR_HUMIDITY = "humidity"

DEFAULT_TOLERANCE = 5
DEFAULT_NAME = "Generic Humistat"

CONF_HUMIDIFYER = "humidifyer"
CONF_SENSOR = "target_sensor"
CONF_MIN_HUMIDITY = "min_humidity"
CONF_MAX_HUMIDITY = "max_humidity"
CONF_TARGET_HUMIDITY = "target_humidity"
CONF_AC_MODE = "ac_mode"
CONF_MIN_DUR = "min_cycle_duration"
CONF_DRYNESS_TOLERANCE = "dryness_tolerance"
CONF_OVERMOIST_TOLERANCE = "overmoist_tolerance"
CONF_KEEP_ALIVE = "keep_alive"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
SUPPORT_FLAGS = SUPPORT_TARGET_HUMIDITY | SUPPORT_PRESET_MODE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HUMIDIFYER): cv.entity_id,
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Optional(CONF_AC_MODE): cv.boolean,
        vol.Optional(CONF_MAX_HUMIDITY, default=ATTR_MAX_HUMIDITY): vol.Coerce(float),
        vol.Optional(CONF_MIN_DUR): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_MIN_HUMIDITY, default=ATTR_MIN_HUMIDITY): vol.Coerce(float),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DRYNESS_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_OVERMOIST_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_TARGET_HUMIDITY, default=40): vol.Coerce(float),
        vol.Optional(CONF_KEEP_ALIVE): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVAC_MODE_DRY, HVAC_MODE_HUMIDIFY, HVAC_MODE_OFF]
        ),
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the generic thermostat platform."""
    name = config.get(CONF_NAME)
    humidifyer_entity_id = config.get(CONF_HUMIDIFYER)
    sensor_entity_id = config.get(CONF_SENSOR)
    min_humidity = config.get(CONF_MIN_HUMIDITY)
    max_humidity = config.get(CONF_MAX_HUMIDITY)
    target_humidity = config.get(CONF_TARGET_HUMIDITY)
    ac_mode = config.get(CONF_AC_MODE)
    min_cycle_duration = config.get(CONF_MIN_DUR)
    dryness_tolerance = config.get(CONF_DRYNESS_TOLERANCE)
    overmoist_tolerance = config.get(CONF_OVERMOIST_TOLERANCE)
    keep_alive = config.get(CONF_KEEP_ALIVE)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    unit = hass.config.units.temperature_unit

    async_add_entities(
        [
            GenericHumistat(
                name,
                humidifyer_entity_id,
                sensor_entity_id,
                min_humidity,
                max_humidity,
                target_humidity,
                ac_mode,
                min_cycle_duration,
                dryness_tolerance,
                overmoist_tolerance,
                keep_alive,
                initial_hvac_mode,
                unit,
            )
        ]
    )

class GenericHumistat(ClimateDevice, RestoreEntity):
    """Representation of a Generic Humistat device."""

    def __init__(
        self,
        name,
        humidifyer_entity_id,
        sensor_entity_id,
        min_humidity,
        max_humidity,
        target_humidity,
        ac_mode,
        min_cycle_duration,
        dryness_tolerance,
        overmoist_tolerance,
        keep_alive,
        initial_hvac_mode,
        unit,
    ):
        """Initialize the humistat."""
        self._name = name
        self.humidifyer_entity_id = humidifyer_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.ac_mode = ac_mode
        self.min_cycle_duration = min_cycle_duration
        self._dryness_tolerance = dryness_tolerance
        self._overmoist_tolerance = overmoist_tolerance
        self._keep_alive = keep_alive
        self._hvac_mode = initial_hvac_mode
        self._saved_target_humidity = target_humidity 
        if self.ac_mode:
            self._presets_list = [HVAC_MODE_DRY, PRESET_NONE]
        else:
            self._presets_list = [HVAC_MODE_HUMIDIFY, PRESET_NONE]
        self._active = False
        self._cur_humidity = None
        self._humidity_lock = asyncio.Lock()
        self._min_humidity = min_humidity
        self._max_humidity = max_humidity
        self._target_humidity = target_humidity
        self._unit = unit
        self._support_flags = SUPPORT_FLAGS | SUPPORT_PRESET_MODE
        self._is_away = False

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        async_track_state_change(
            self.hass, self.sensor_entity_id, self._async_sensor_changed
        )
        async_track_state_change(
            self.hass, self.humidifyer_entity_id, self._async_switch_changed
        )

        if self._keep_alive:
            async_track_time_interval(
                self.hass, self._async_control_moisturizing, self._keep_alive
            )

        @callback
        def _async_startup(event):
            """Init on startup."""
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if sensor_state and sensor_state.state != STATE_UNKNOWN:
                self._async_update_humidity(sensor_state)

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            # If we have no initial humidity, restore
            if self._target_humidity is None:
                # If we have a previously saved humidity 
                if old_state.attributes.get(ATTR_HUMIDITY) is None:
                    if self.ac_mode:
                        self._target_humidity = self.max_humidity
                    else:
                        self._target_humidity = self.min_humidity
                    _LOGGER.warning(
                        "Undefined target humidity, falling back to %s",
                        self._target_humidity,
                    )
                else:
                    self._target_humidity = float(old_state.attributes[ATTR_HUMIDITY])
            if old_state.attributes.get(ATTR_PRESET_MODE) == PRESET_AWAY:
                self._is_away = True
            if not self._hvac_mode and old_state.state:
                self._hvac_mode = old_state.state

        else:
            # No previous state, try and restore defaults
            if self._target_humidity is None:
                if self.ac_mode:
                    self._target_humidity = self.max_humidity
                else:
                    self._target_humidity = self.min_humidity
            _LOGGER.warning(
                "No previously saved humidity, setting to %s", self._target_humidity
            )

        # Set default state to off
        if not self._hvac_mode:
            self._hvac_mode = HVAC_MODE_OFF

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def humidity_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_humidity(self):
        """Return the sensor humidity."""
        return self._cur_humidity

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVAC_MODE_OFF:
            return CURRENT_HVAC_OFF
        if not self._is_device_active:
            return CURRENT_HVAC_IDLE
        if self.ac_mode:
            return CURRENT_HVAC_DRY
        return CURRENT_HVAC_HUMIDIFY

    @property
    def target_humidity(self):
        """Return the humidity we try to reach."""
        return self._target_humidity

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        #only HVAC_MODE_OFF is supported
        return [HVAC_MODE_OFF]

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        return self._preset_mode

    @property
    def preset_modes(self):
        """Return a list of available preset modes or PRESET_NONE if _away_humidity is undefined."""
        return self._presets_list

    async def async_set_preset_mode(self, preset_mode):        
        if preset_mode == HVAC_MODE_HUMIDIFY:
            self._preset_mode = HVAC_MODE_HUMIDIFY
            await self._async_control_moisturizing(force=True)
        elif preset_mode == HVAC_MODE_DRY:
            self._preset_mode = HVAC_MODE_DRY
            await self._async_control_moisturizing(force=True)
        elif preset_mode == PRESET_NONE:
            self._preset_mode = PRESET_NONE
            if self._is_device_active:
                await self._async_humidifyer_turn_off()
        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return



    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        if hvac_mode == HVAC_MODE_OFF:
            self._hvac_mode = HVAC_MODE_OFF
            if self._is_device_active:
                await self._async_humidifyer_turn_off()
        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return
        # Ensure we update the current operation after changing the mode
        self.schedule_update_ha_state()

    async def async_set_humidity(self, **kwargs):
        """Set new target humidity."""
        humidity = kwargs.get(ATTR_HUMIDITY)
        if humidity is None:
            return
        self._target_humidity = humidity
        await self._async_control_moisturizing(force=True)
        await self.async_update_ha_state()

    @property
    def min_humidity(self):
        """Return the minimum humidity."""
        if self._min_humidity is not None:
            return self._min_humidity

        # get default humidity from super class
        return super().min_humidity

    @property
    def max_humidity(self):
        """Return the maximum humidity."""
        if self._max_humidity is not None:
            return self._max_humidity

        # Get default humidity from super class
        return super().max_humidity

    async def _async_sensor_changed(self, entity_id, old_state, new_state):
        """Handle humidity  changes."""
        if new_state is None:
            return

        self._async_update_humidity(new_state)
        await self._async_control_moisturizing()
        await self.async_update_ha_state()

    @callback
    def _async_switch_changed(self, entity_id, old_state, new_state):
        """Handle humidifyer switch state changes."""
        if new_state is None:
            return
        self.async_schedule_update_ha_state()

    @callback
    def _async_update_humidity(self, state):
        """Update thermostat with latest state from sensor."""
        try:
            self._cur_humidity = float(state.state)
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    async def _async_control_moisturizing(self, time=None, force=False):
        """Check if we need to turn humidifying on or off."""
        async with self._humidity_lock:
            if not self._active and None not in (self._cur_humidity, self._target_humidity):
                self._active = True
                _LOGGER.info(
                    "Obtained current and target humidity. "
                    "Generic humistat active. %s, %s",
                    self._cur_humidity,
                    self._target_humidity,
                )

            if not self._active or self._hvac_mode == HVAC_MODE_OFF:
                return

            if not force and time is None:
                # If the `force` argument is True, we
                # ignore `min_cycle_duration`.
                # If the `time` argument is not none, we were invoked for
                # keep-alive purposes, and `min_cycle_duration` is irrelevant.
                if self.min_cycle_duration:
                    if self._is_device_active:
                        current_state = STATE_ON
                    else:
                        current_state = HVAC_MODE_OFF
                    long_enough = condition.state(
                        self.hass,
                        self.humidifyer_entity_id,
                        current_state,
                        self.min_cycle_duration,
                    )
                    if not long_enough:
                        return

            too_dry = self._target_humidity >= self._cur_humidity + self._dryness_tolerance
            too_wet = self._cur_humidity >= self._target_humidity + self._overmoist_tolerance
            if self._is_device_active:
                if (self.ac_mode and too_dry) or (not self.ac_mode and too_wet):
                    _LOGGER.info("Turning off humidifyer %s", self.humidifyer_entity_id)
                    await self._async_humidifyer_turn_off()
                elif time is not None:
                    # The time argument is passed only in keep-alive case
                    _LOGGER.info(
                        "Keep-alive - Turning on humidifyer %s",
                        self.humidifyer_entity_id,
                    )
                    await self._async_humidifyer_turn_on()
            else:
                if (self.ac_mode and too_wet) or (not self.ac_mode and too_dry):
                    _LOGGER.info("Turning on humidifyer %s", self.humidifyer_entity_id)
                    await self._async_humidifyer_turn_on()
                elif time is not None:
                    # The time argument is passed only in keep-alive case
                    _LOGGER.info(
                        "Keep-alive - Turning off humidifyer %s", self.humidifyer_entity_id
                    )
                    await self._async_humidifyer_turn_off()

    @property
    def _is_device_active(self):
        """If the toggleable device is currently active."""
        return self.hass.states.is_state(self.humidifyer_entity_id, STATE_ON)

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    async def _async_humidifyer_turn_on(self):
        """Turn heater toggleable device on."""
        data = {ATTR_ENTITY_ID: self.humidifyer_entity_id}
        await self.hass.services.async_call(HA_DOMAIN, SERVICE_TURN_ON, data)

    async def _async_humidifyer_turn_off(self):
        """Turn heater toggleable device off."""
        data = {ATTR_ENTITY_ID: self.humidifyer_entity_id}
        await self.hass.services.async_call(HA_DOMAIN, SERVICE_TURN_OFF, data)

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        if preset_mode == PRESET_AWAY and not self._is_away:
            self._is_away = True
            self._saved_target_humidity = self._target_humidity
            self._target_humidity = self._away_humidity
            await self._async_control_moisturizing(force=True)
        elif preset_mode == PRESET_NONE and self._is_away:
            self._is_away = False
            self._target_humidity = self._saved_target_humidity
            await self._async_control_moisturizing(force=True)

        await self.async_update_ha_state()

    # properties to be a climate device

    @property
    def capability_attributes(self):
      return {
         ATTR_HVAC_MODES: self.hvac_modes,
         ATTR_MIN_HUMIDITY: self.min_humidity,
         ATTR_MAX_HUMIDITY: self.max_humidity,
      }

    @property
    def state_attributes(self):
      return {
          ATTR_CURRENT_HUMIDITY: self.current_humidity,
          ATTR_HUMIDITY: self.target_humidity,
          ATTR_HVAC_ACTION: self.hvac_action,
          ATTR_PRESET_MODE: self.preset_mode,
      }
 

    @property
    def temperature_unit(self):
      return self._unit
