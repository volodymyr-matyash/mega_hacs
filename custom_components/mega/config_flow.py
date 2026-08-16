"""Пока не сделано"""
import asyncio
import logging

import aiohttp
import voluptuous as vol

from homeassistant import config_entries, core
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_ID, CONF_PASSWORD
from homeassistant.core import callback, HomeAssistant
from .const import DOMAIN, CONF_RELOAD, CONF_SCAN_INTERVAL, \
    CONF_NPORTS, CONF_UPDATE_ALL, CONF_POLL_OUTS, CONF_FAKE_RESPONSE, CONF_FORCE_D, \
    CONF_ALLOW_HOSTS, CONF_PROTECTED, CONF_RESTORE_ON_RESTART, CONF_UPDATE_TIME, \
    CONFIG_OPTION_KEYS
from .hub import MegaD
from . import exceptions

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID, default='mega'): str,
        vol.Required(CONF_HOST, default="192.168.0.14"): str,
        vol.Required(CONF_PASSWORD, default="sec"): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=30): int,
        vol.Optional(CONF_POLL_OUTS, default=False): bool,
        # vol.Optional(CONF_PORT_TO_SCAN, default=0): int,
        # vol.Optional(CONF_MQTT_INPUTS, default=False): bool,
        vol.Optional(CONF_NPORTS, default=37): int,
        vol.Optional(CONF_UPDATE_ALL, default=True): bool,
        vol.Optional(CONF_FAKE_RESPONSE, default=True): bool,
        vol.Optional(CONF_FORCE_D, default=True): bool,
        vol.Optional(CONF_RESTORE_ON_RESTART, default=True): bool,
        vol.Optional(CONF_PROTECTED, default=True): bool,
        vol.Optional(CONF_ALLOW_HOSTS, default='::1;127.0.0.1'): str,
        vol.Optional(CONF_UPDATE_TIME, default=True): bool,
    },
)


async def get_hub(hass: HomeAssistant, data):
    # _mqtt = hass.data.get(mqtt.DOMAIN)
    # if not isinstance(_mqtt, mqtt.MQTT):
    #     raise exceptions.MqttNotConfigured("mqtt must be configured first")
    hub = MegaD(
        hass,
        **data,
        lg=_LOGGER,
        loop=asyncio.get_running_loop(),
    )  # mqtt=_mqtt,
    try:
        if not await hub.authenticate():
            raise exceptions.InvalidAuth
        hub.mqtt_id = await hub.get_mqtt_id()
    except exceptions.InvalidAuth:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, exceptions.CannotConnect) as err:
        _LOGGER.warning(
            "Unable to connect to MegaD at %s (%s)",
            data.get(CONF_HOST),
            type(err).__name__,
        )
        raise exceptions.CannotConnect from None
    return hub


async def validate_input(hass: core.HomeAssistant, data):
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    if data[CONF_ID] in hass.data.get(DOMAIN, []):
        raise exceptions.DuplicateId('duplicate_id')
    hub = await get_hub(hass, data)

    return hub


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for mega."""

    VERSION = 26
    CONNECTION_CLASS = config_entries.CONN_CLASS_ASSUMED

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors = {}

        try:
            hub = await validate_input(self.hass, user_input)
            try:
                await hub.start()
                hub.new_naming = True
                config = await hub.get_config(
                    nports=user_input.get(CONF_NPORTS, 37)
                )
            finally:
                await hub.stop()
            hub.lg.debug("config loaded with keys: %s", sorted(config))
            config.update(user_input)
            config['new_naming'] = True
            return self.async_create_entry(
                title=user_input.get(CONF_ID, user_input[CONF_HOST]),
                data=config,
            )
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except exceptions.InvalidAuth:
            errors["base"] = "invalid_auth"
        except exceptions.DuplicateId:
            errors["base"] = "duplicate_id"
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors[CONF_ID] = str(exc)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """Start reauthentication after the controller rejects the password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Validate and store a replacement MegaD password."""
        errors = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            data = dict(entry.data)
            data[CONF_PASSWORD] = password
            hub = None
            try:
                hub = await get_hub(self.hass, data)
            except exceptions.InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unable to reconnect to MegaD")
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: password},
                )
            finally:
                if hub is not None:
                    await hub.stop()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={
                "host": str(entry.data.get(CONF_HOST, "MegaD"))
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        """Manage the options."""
        current = dict(self.config_entry.data)
        current.update({
            key: value
            for key, value in self.config_entry.options.items()
            if key in CONFIG_OPTION_KEYS
        })
        new_naming = current.get('new_naming', False)

        if user_input is not None:
            user_input = dict(user_input)
            reload_config = user_input.pop(CONF_RELOAD)
            options = {
                key: value
                for key, value in user_input.items()
                if key in CONFIG_OPTION_KEYS
            }
            cfg = dict(current)
            cfg.update(options)
            cfg['new_naming'] = new_naming

            if reload_config:
                id = cfg.get('id', self.config_entry.entry_id)
                hub: MegaD = self.hass.data[DOMAIN].get(id)
                if hub is None:
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_options_schema(current),
                        errors={"base": "cannot_connect"},
                    )
                try:
                    cfg = await hub.reload(
                        reload_entry=False,
                        base_config=cfg,
                    )
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unable to reload MegaD configuration")
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_options_schema(current),
                        errors={"base": "cannot_connect"},
                    )

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=cfg,
                    options=options,
                )

            return self.async_create_entry(
                title='',
                data=options,
            )

        ret = self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current),
        )
        return ret


def _options_schema(data):
    return vol.Schema({
        vol.Optional(
            CONF_SCAN_INTERVAL,
            default=data.get(CONF_SCAN_INTERVAL, 0),
        ): int,
        vol.Optional(
            CONF_POLL_OUTS,
            default=data.get(CONF_POLL_OUTS, False),
        ): bool,
        vol.Optional(CONF_NPORTS, default=data.get(CONF_NPORTS, 37)): int,
        vol.Optional(CONF_RELOAD, default=False): bool,
        vol.Optional(
            CONF_UPDATE_ALL,
            default=data.get(CONF_UPDATE_ALL, True),
        ): bool,
        vol.Optional(
            CONF_FAKE_RESPONSE,
            default=data.get(CONF_FAKE_RESPONSE, True),
        ): bool,
        vol.Optional(
            CONF_FORCE_D,
            default=data.get(CONF_FORCE_D, False),
        ): bool,
        vol.Optional(
            CONF_RESTORE_ON_RESTART,
            default=data.get(CONF_RESTORE_ON_RESTART, False),
        ): bool,
        vol.Optional(
            CONF_PROTECTED,
            default=data.get(CONF_PROTECTED, True),
        ): bool,
        vol.Optional(
            CONF_ALLOW_HOSTS,
            default=data.get(CONF_ALLOW_HOSTS, '::1;127.0.0.1'),
        ): str,
        vol.Optional(
            CONF_UPDATE_TIME,
            default=data.get(CONF_UPDATE_TIME, False),
        ): bool,
    })
