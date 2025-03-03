"""This platform enables the possibility to control a MQTT alarm."""
from __future__ import annotations

import asyncio
import functools
import logging
import re

import voluptuous as vol

import homeassistant.components.alarm_control_panel as alarm
from homeassistant.components.alarm_control_panel import AlarmControlPanelEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_CODE,
    CONF_NAME,
    CONF_VALUE_TEMPLATE,
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_CUSTOM_BYPASS,
    STATE_ALARM_ARMED_HOME,
    STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_ARMED_VACATION,
    STATE_ALARM_ARMING,
    STATE_ALARM_DISARMED,
    STATE_ALARM_DISARMING,
    STATE_ALARM_PENDING,
    STATE_ALARM_TRIGGERED,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import MqttCommandTemplate, MqttValueTemplate, subscription
from .. import mqtt
from .const import (
    CONF_COMMAND_TEMPLATE,
    CONF_COMMAND_TOPIC,
    CONF_ENCODING,
    CONF_QOS,
    CONF_RETAIN,
    CONF_STATE_TOPIC,
)
from .debug_info import log_messages
from .mixins import (
    MQTT_ENTITY_COMMON_SCHEMA,
    MqttEntity,
    async_get_platform_config_from_yaml,
    async_setup_entry_helper,
    async_setup_platform_helper,
    warn_for_legacy_schema,
)

_LOGGER = logging.getLogger(__name__)

CONF_CODE_ARM_REQUIRED = "code_arm_required"
CONF_CODE_DISARM_REQUIRED = "code_disarm_required"
CONF_CODE_TRIGGER_REQUIRED = "code_trigger_required"
CONF_PAYLOAD_DISARM = "payload_disarm"
CONF_PAYLOAD_ARM_HOME = "payload_arm_home"
CONF_PAYLOAD_ARM_AWAY = "payload_arm_away"
CONF_PAYLOAD_ARM_NIGHT = "payload_arm_night"
CONF_PAYLOAD_ARM_VACATION = "payload_arm_vacation"
CONF_PAYLOAD_ARM_CUSTOM_BYPASS = "payload_arm_custom_bypass"
CONF_PAYLOAD_TRIGGER = "payload_trigger"

MQTT_ALARM_ATTRIBUTES_BLOCKED = frozenset(
    {
        alarm.ATTR_CHANGED_BY,
        alarm.ATTR_CODE_ARM_REQUIRED,
        alarm.ATTR_CODE_FORMAT,
    }
)

DEFAULT_COMMAND_TEMPLATE = "{{action}}"
DEFAULT_ARM_NIGHT = "ARM_NIGHT"
DEFAULT_ARM_VACATION = "ARM_VACATION"
DEFAULT_ARM_AWAY = "ARM_AWAY"
DEFAULT_ARM_HOME = "ARM_HOME"
DEFAULT_ARM_CUSTOM_BYPASS = "ARM_CUSTOM_BYPASS"
DEFAULT_DISARM = "DISARM"
DEFAULT_TRIGGER = "TRIGGER"
DEFAULT_NAME = "MQTT Alarm"

REMOTE_CODE = "REMOTE_CODE"
REMOTE_CODE_TEXT = "REMOTE_CODE_TEXT"

PLATFORM_SCHEMA_MODERN = mqtt.MQTT_BASE_SCHEMA.extend(
    {
        vol.Optional(CONF_CODE): cv.string,
        vol.Optional(CONF_CODE_ARM_REQUIRED, default=True): cv.boolean,
        vol.Optional(CONF_CODE_DISARM_REQUIRED, default=True): cv.boolean,
        vol.Optional(CONF_CODE_TRIGGER_REQUIRED, default=True): cv.boolean,
        vol.Optional(
            CONF_COMMAND_TEMPLATE, default=DEFAULT_COMMAND_TEMPLATE
        ): cv.template,
        vol.Required(CONF_COMMAND_TOPIC): mqtt.valid_publish_topic,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PAYLOAD_ARM_AWAY, default=DEFAULT_ARM_AWAY): cv.string,
        vol.Optional(CONF_PAYLOAD_ARM_HOME, default=DEFAULT_ARM_HOME): cv.string,
        vol.Optional(CONF_PAYLOAD_ARM_NIGHT, default=DEFAULT_ARM_NIGHT): cv.string,
        vol.Optional(
            CONF_PAYLOAD_ARM_VACATION, default=DEFAULT_ARM_VACATION
        ): cv.string,
        vol.Optional(
            CONF_PAYLOAD_ARM_CUSTOM_BYPASS, default=DEFAULT_ARM_CUSTOM_BYPASS
        ): cv.string,
        vol.Optional(CONF_PAYLOAD_DISARM, default=DEFAULT_DISARM): cv.string,
        vol.Optional(CONF_PAYLOAD_TRIGGER, default=DEFAULT_TRIGGER): cv.string,
        vol.Optional(CONF_RETAIN, default=mqtt.DEFAULT_RETAIN): cv.boolean,
        vol.Required(CONF_STATE_TOPIC): mqtt.valid_subscribe_topic,
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    }
).extend(MQTT_ENTITY_COMMON_SCHEMA.schema)

# Configuring MQTT alarm control panels under the alarm_control_panel platform key is deprecated in HA Core 2022.6
PLATFORM_SCHEMA = vol.All(
    cv.PLATFORM_SCHEMA.extend(PLATFORM_SCHEMA_MODERN.schema),
    warn_for_legacy_schema(alarm.DOMAIN),
)

DISCOVERY_SCHEMA = PLATFORM_SCHEMA_MODERN.extend({}, extra=vol.REMOVE_EXTRA)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up MQTT alarm control panel configured under the alarm_control_panel key (deprecated)."""
    # Deprecated in HA Core 2022.6
    await async_setup_platform_helper(
        hass, alarm.DOMAIN, config, async_add_entities, _async_setup_entity
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MQTT alarm control panel through configuration.yaml and dynamically through MQTT discovery."""
    # load and initialize platform config from configuration.yaml
    await asyncio.gather(
        *(
            _async_setup_entity(hass, async_add_entities, config, config_entry)
            for config in await async_get_platform_config_from_yaml(
                hass, alarm.DOMAIN, PLATFORM_SCHEMA_MODERN
            )
        )
    )
    # setup for discovery
    setup = functools.partial(
        _async_setup_entity, hass, async_add_entities, config_entry=config_entry
    )
    await async_setup_entry_helper(hass, alarm.DOMAIN, setup, DISCOVERY_SCHEMA)


async def _async_setup_entity(
    hass, async_add_entities, config, config_entry=None, discovery_data=None
):
    """Set up the MQTT Alarm Control Panel platform."""
    async_add_entities([MqttAlarm(hass, config, config_entry, discovery_data)])


class MqttAlarm(MqttEntity, alarm.AlarmControlPanelEntity):
    """Representation of a MQTT alarm status."""

    _entity_id_format = alarm.ENTITY_ID_FORMAT
    _attributes_extra_blocked = MQTT_ALARM_ATTRIBUTES_BLOCKED

    def __init__(self, hass, config, config_entry, discovery_data):
        """Init the MQTT Alarm Control Panel."""
        self._state = None

        MqttEntity.__init__(self, hass, config, config_entry, discovery_data)

    @staticmethod
    def config_schema():
        """Return the config schema."""
        return DISCOVERY_SCHEMA

    def _setup_from_config(self, config):
        self._value_template = MqttValueTemplate(
            self._config.get(CONF_VALUE_TEMPLATE),
            entity=self,
        ).async_render_with_possible_json_value
        self._command_template = MqttCommandTemplate(
            self._config[CONF_COMMAND_TEMPLATE], entity=self
        ).async_render

    def _prepare_subscribe_topics(self):
        """(Re)Subscribe to topics."""

        @callback
        @log_messages(self.hass, self.entity_id)
        def message_received(msg):
            """Run when new MQTT message has been received."""
            payload = self._value_template(msg.payload)
            if payload not in (
                STATE_ALARM_DISARMED,
                STATE_ALARM_ARMED_HOME,
                STATE_ALARM_ARMED_AWAY,
                STATE_ALARM_ARMED_NIGHT,
                STATE_ALARM_ARMED_VACATION,
                STATE_ALARM_ARMED_CUSTOM_BYPASS,
                STATE_ALARM_PENDING,
                STATE_ALARM_ARMING,
                STATE_ALARM_DISARMING,
                STATE_ALARM_TRIGGERED,
            ):
                _LOGGER.warning("Received unexpected payload: %s", msg.payload)
                return
            self._state = payload
            self.async_write_ha_state()

        self._sub_state = subscription.async_prepare_subscribe_topics(
            self.hass,
            self._sub_state,
            {
                "state_topic": {
                    "topic": self._config[CONF_STATE_TOPIC],
                    "msg_callback": message_received,
                    "qos": self._config[CONF_QOS],
                    "encoding": self._config[CONF_ENCODING] or None,
                }
            },
        )

    async def _subscribe_topics(self):
        """(Re)Subscribe to topics."""
        await subscription.async_subscribe_topics(self.hass, self._sub_state)

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return (
            AlarmControlPanelEntityFeature.ARM_HOME
            | AlarmControlPanelEntityFeature.ARM_AWAY
            | AlarmControlPanelEntityFeature.ARM_NIGHT
            | AlarmControlPanelEntityFeature.ARM_VACATION
            | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
            | AlarmControlPanelEntityFeature.TRIGGER
        )

    @property
    def code_format(self):
        """Return one or more digits/characters."""
        if (code := self._config.get(CONF_CODE)) is None:
            return None
        if code == REMOTE_CODE or (isinstance(code, str) and re.search("^\\d+$", code)):
            return alarm.CodeFormat.NUMBER
        return alarm.CodeFormat.TEXT

    @property
    def code_arm_required(self):
        """Whether the code is required for arm actions."""
        code_required = self._config.get(CONF_CODE_ARM_REQUIRED)
        return code_required

    async def async_alarm_disarm(self, code=None):
        """Send disarm command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_DISARM_REQUIRED]
        if code_required and not self._validate_code(code, "disarming"):
            return
        payload = self._config[CONF_PAYLOAD_DISARM]
        await self._publish(code, payload)

    async def async_alarm_arm_home(self, code=None):
        """Send arm home command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_ARM_REQUIRED]
        if code_required and not self._validate_code(code, "arming home"):
            return
        action = self._config[CONF_PAYLOAD_ARM_HOME]
        await self._publish(code, action)

    async def async_alarm_arm_away(self, code=None):
        """Send arm away command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_ARM_REQUIRED]
        if code_required and not self._validate_code(code, "arming away"):
            return
        action = self._config[CONF_PAYLOAD_ARM_AWAY]
        await self._publish(code, action)

    async def async_alarm_arm_night(self, code=None):
        """Send arm night command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_ARM_REQUIRED]
        if code_required and not self._validate_code(code, "arming night"):
            return
        action = self._config[CONF_PAYLOAD_ARM_NIGHT]
        await self._publish(code, action)

    async def async_alarm_arm_vacation(self, code=None):
        """Send arm vacation command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_ARM_REQUIRED]
        if code_required and not self._validate_code(code, "arming vacation"):
            return
        action = self._config[CONF_PAYLOAD_ARM_VACATION]
        await self._publish(code, action)

    async def async_alarm_arm_custom_bypass(self, code=None):
        """Send arm custom bypass command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_ARM_REQUIRED]
        if code_required and not self._validate_code(code, "arming custom bypass"):
            return
        action = self._config[CONF_PAYLOAD_ARM_CUSTOM_BYPASS]
        await self._publish(code, action)

    async def async_alarm_trigger(self, code=None):
        """Send trigger command.

        This method is a coroutine.
        """
        code_required = self._config[CONF_CODE_TRIGGER_REQUIRED]
        if code_required and not self._validate_code(code, "triggering"):
            return
        action = self._config[CONF_PAYLOAD_TRIGGER]
        await self._publish(code, action)

    async def _publish(self, code, action):
        """Publish via mqtt."""
        variables = {"action": action, "code": code}
        payload = self._command_template(None, variables=variables)
        await self.async_publish(
            self._config[CONF_COMMAND_TOPIC],
            payload,
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
            self._config[CONF_ENCODING],
        )

    def _validate_code(self, code, state):
        """Validate given code."""
        conf_code = self._config.get(CONF_CODE)
        check = (
            conf_code is None
            or code == conf_code
            or (conf_code == REMOTE_CODE and code)
            or (conf_code == REMOTE_CODE_TEXT and code)
        )
        if not check:
            _LOGGER.warning("Wrong code entered for %s", state)
        return check
