"""The mega integration."""
import asyncio
import logging
from functools import partial

import aiohttp
import voluptuous as vol

from homeassistant.const import (
    CONF_NAME, CONF_DOMAIN,
    CONF_UNIT_OF_MEASUREMENT, CONF_VALUE_TEMPLATE, CONF_DEVICE_CLASS, CONF_PORT
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.config_entries import ConfigEntry
from .const import DOMAIN, CONF_INVERT, CONF_RELOAD, PLATFORMS, CONF_PORTS, CONF_CUSTOM, CONF_SKIP, CONF_PORT_TO_SCAN, \
    CONF_MQTT_INPUTS, CONF_HTTP, CONF_RESPONSE_TEMPLATE, CONF_ACTION, CONF_GET_VALUE, CONF_ALLOW_HOSTS, \
    CONF_CONV_TEMPLATE, CONF_ALL, CONF_FORCE_D, CONF_DEF_RESPONSE, CONF_FORCE_I2C_SCAN, CONF_HEX_TO_FLOAT, \
    RGB_COMBINATIONS, CONF_WS28XX, CONF_ORDER, CONF_SMOOTH, CONF_LED, CONF_WHITE_SEP, CONF_CHIP, CONF_RANGE, \
    CONF_FILTER_VALUES, CONF_FILTER_SCALE, CONF_FILTER_LOW, CONF_FILTER_HIGH, CONF_FILL_NA, CONF_MEGA_ID, CONF_ADDR, \
    CONF_1WBUS, CONFIG_OPTION_KEYS
from .hub import MegaD
from .config_flow import ConfigFlow
from .exceptions import CannotConnect
from .http import MegaView

_LOGGER = logging.getLogger(__name__)

_port_n = vol.Any(int, str)

LED_LIGHT = \
    {
        str: vol.Any(
            {
                vol.Required(CONF_PORTS): vol.Any(
                    vol.ExactSequence([_port_n, _port_n, _port_n]),
                    vol.ExactSequence([_port_n, _port_n, _port_n, _port_n]),
                    msg='ports must be [R, G, B] or [R, G, B, W] of integers 0..255'
                ),
                vol.Optional(CONF_NAME): str,
                vol.Optional(CONF_WHITE_SEP, default=True): bool,
                vol.Optional(CONF_SMOOTH, default=1): cv.time_period_seconds,
            },
            {
                vol.Required(CONF_PORT): int,
                vol.Required(CONF_WS28XX): True,
                vol.Optional(CONF_CHIP, default=100): int,
                vol.Optional(CONF_ORDER, default='rgb'): vol.Any(*RGB_COMBINATIONS, msg=f'order must be one of {RGB_COMBINATIONS}'),
                vol.Optional(CONF_SMOOTH, default=1): cv.time_period_seconds,
                vol.Optional(CONF_NAME): str,
            },
        )
    }

CUSTOMIZE_PORT = {
    vol.Optional(CONF_SKIP, description='исключить порт из сканирования', default=False): bool,
    vol.Optional(CONF_FILL_NA, default='last'): vol.Any(
      'last',
      'none'
    ),
    vol.Optional(CONF_RANGE, description='диапазон диммирования'): [
        vol.Range(0, 255),
        vol.Range(0, 255),
    ],
    vol.Optional(CONF_INVERT, default=False): bool,
    vol.Optional(CONF_NAME): vol.Any(str, {
        vol.Required(str): str
    }),
    vol.Optional(CONF_DOMAIN): vol.Any('light', 'switch'),
    vol.Optional(CONF_UNIT_OF_MEASUREMENT, description='единицы измерений, либо строка либо мепинг'):
        vol.Any(str, {
            vol.Required(str): str
        }),
    vol.Optional(CONF_DEVICE_CLASS):
        vol.Any(str, {
            vol.Required(str): str
        }),
    vol.Optional(
        CONF_RESPONSE_TEMPLATE,
        description='шаблон ответа когда на этот порт приходит'
                    'сообщение из меги '): cv.template,
    vol.Optional(CONF_ACTION): cv.script_action, # пока не реализовано
    vol.Optional(CONF_GET_VALUE, default=True): bool,
    vol.Optional(CONF_CONV_TEMPLATE): cv.template,
    vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    vol.Optional(CONF_FORCE_I2C_SCAN): bool,
    vol.Optional(CONF_HEX_TO_FLOAT): bool,
    vol.Optional(CONF_FILTER_VALUES): [vol.Coerce(float)],
    vol.Optional(CONF_FILTER_SCALE): vol.Coerce(float),
    vol.Optional(CONF_FILTER_LOW): vol.Coerce(float),
    vol.Optional(CONF_FILTER_HIGH): vol.Coerce(float),
    vol.Optional(CONF_SMOOTH): cv.time_period_seconds,
    # vol.Optional(CONF_RANGE): vol.ExactSequence([int, int]), TODO: сделать отбрасывание "плохих" значений
    vol.Optional(str): {
        vol.Optional(CONF_NAME): str,
        vol.Optional(CONF_DEVICE_CLASS): str,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): str,
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    }
}
CUSTOMIZE_DS2413 = {
    vol.Optional(str.lower, description='адрес и индекс устройства'): CUSTOMIZE_PORT
}


def extender(x):
    if isinstance(x, str) and 'e' in x:
        return x
    else:
        raise ValueError('must has "e" in port name')

OWBUS = vol.Schema({
    vol.Required(CONF_PORT): vol.Any(vol.Coerce(int), vol.Coerce(str)),
    vol.Required(CONF_MEGA_ID): vol.Coerce(str),
    vol.Required(CONF_ADDR): [str],
})

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: {
            vol.Optional(CONF_ALLOW_HOSTS): [str],
            vol.Optional('entities'): {
                vol.Optional(str): vol.Any(
                CUSTOMIZE_PORT,
                CUSTOMIZE_DS2413
            )},
            vol.Optional(vol.Any(str, int), description='id меги из веб-интерфейса'): {
                vol.Optional(CONF_FORCE_D, description='Принудительно слать d после срабатывания входа', default=False): bool,
                vol.Optional(
                    CONF_DEF_RESPONSE,
                    description='Ответ по умолчанию',
                    default=None
                ): vol.Any(cv.template, None),
                vol.Optional(CONF_LED): LED_LIGHT,
                vol.Optional(vol.Any(int, extender), description='номер порта'): vol.Any(
                    CUSTOMIZE_PORT,
                    CUSTOMIZE_DS2413,
                ),
                vol.Optional(CONF_FILTER_VALUES): [vol.Coerce(float)],
                vol.Optional(CONF_FILTER_SCALE): vol.Coerce(float),
                vol.Optional(CONF_FILTER_LOW): vol.Coerce(float),
                vol.Optional(CONF_FILTER_HIGH): vol.Coerce(float),
            },
            vol.Optional(CONF_1WBUS): [OWBUS]
        }
    },
    extra=vol.ALLOW_EXTRA,
)

ALIVE_STATE = 'alive'
DEF_ID = 'def'
async def async_setup(hass: HomeAssistant, config: dict):
    """YAML-конфигурация содержит только кастомизации портов"""
    hass.data[DOMAIN] = {CONF_CUSTOM: config.get(DOMAIN, {})}
    hass.data[DOMAIN][CONF_HTTP] = view = MegaView(cfg=config.get(DOMAIN, {}))
    hass.data[DOMAIN][CONF_ALL] = {}
    view.allowed_hosts |= set(config.get(DOMAIN, {}).get(CONF_ALLOW_HOSTS, []))
    hass.http.register_view(view)
    hass.services.async_register(
        DOMAIN, 'save', partial(_save_service, hass), schema=vol.Schema({
            vol.Optional('mega_id'): str
        })
    )
    hass.services.async_register(
        DOMAIN, 'get_port', partial(_get_port, hass), schema=vol.Schema({
            vol.Optional('mega_id'): str,
            vol.Optional('port'): vol.Any(int, [int]),
        })
    )
    hass.services.async_register(
        DOMAIN, 'run_cmd', partial(_run_cmd, hass), schema=vol.Schema({
            vol.Optional('port'): int,
            vol.Required('cmd'): str,
            vol.Optional('mega_id'): str,
        })
    )

    return True


async def get_hub(hass, entry):
    id = entry.data.get('id', entry.entry_id)
    data = dict(entry.data)
    data.update({
        key: value
        for key, value in entry.options.items()
        if key in CONFIG_OPTION_KEYS
    })
    data.update(id=id)
    hub = MegaD(
        hass,
        config=entry,
        **data,
        lg=_LOGGER,
        loop=asyncio.get_running_loop(),
    )
    return hub


async def _add_mega(hass: HomeAssistant, entry: ConfigEntry):
    hub = await get_hub(hass, entry)
    try:
        if not await hub.authenticate():
            raise ConfigEntryAuthFailed("Invalid MegaD password")
        hub.mqtt_id = await hub.get_mqtt_id()
        hub.fw = await hub.get_fw()
    except ConfigEntryAuthFailed:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, CannotConnect) as err:
        _LOGGER.warning(
            "Unable to connect to MegaD at %s (%s)",
            hub.host,
            type(err).__name__,
        )
        raise ConfigEntryNotReady("Unable to connect to MegaD") from None
    return hub


async def _cleanup_runtime_hub(hass: HomeAssistant, id, hub: MegaD) -> None:
    """Stop a hub and remove every runtime reference to it."""
    try:
        await hub.stop()
    finally:
        hass.data[DOMAIN].pop(id, None)
        hass.data[DOMAIN][CONF_ALL].pop(id, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    id = entry.data.get('id', entry.entry_id)
    hub: MegaD = await _add_mega(hass, entry)
    hass.data[DOMAIN][id] = hub
    hass.data[DOMAIN][CONF_ALL][id] = hub
    platform_setup_started = False
    try:
        await hub.start()
        platform_setup_started = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await hub.updater.async_refresh()
        hub.register_http()
    except (Exception, asyncio.CancelledError):
        if platform_setup_started:
            try:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            except (Exception, asyncio.CancelledError) as unload_err:
                _LOGGER.warning(
                    "Unable to unload partially set up MegaD platforms (%s)",
                    type(unload_err).__name__,
                )
        await _cleanup_runtime_hub(hass, id, hub)
        raise
    entry.async_on_unload(entry.add_update_listener(updater))
    return True


async def updater(hass: HomeAssistant, entry: ConfigEntry):
    """
    Обновляется конфигурация
    :param hass:
    :param entry:
    :return:
    """
    # hub: MegaD = hass.data[DOMAIN][entry.data[CONF_ID]]
    # hub.poll_interval = entry.options[CONF_SCAN_INTERVAL]
    # hub.port_to_scan = entry.options.get(CONF_PORT_TO_SCAN, 0)
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    id = entry.data.get('id', entry.entry_id)
    hub: MegaD = hass.data[DOMAIN].get(id)
    _LOGGER.debug('unload %s', id)
    if hub is not None:
        await _cleanup_runtime_hub(hass, id, hub)
    else:
        hass.data[DOMAIN].pop(id, None)
        hass.data[DOMAIN][CONF_ALL].pop(id, None)
    return True


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s to version %s", config_entry.version, ConfigFlow.VERSION)
    hub = await get_hub(hass, config_entry)
    new = dict(config_entry.data)
    try:
        await hub.start()
        cfg = await hub.get_config()
    finally:
        await hub.stop()
    new.update(cfg)
    _LOGGER.debug("migrated config keys: %s", sorted(new))
    hass.config_entries.async_update_entry(
        config_entry,
        data=new,
        version=ConfigFlow.VERSION,
    )

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True


async def _save_service(hass: HomeAssistant, call: ServiceCall):
    mega_id = call.data.get('mega_id')
    if mega_id:
        hub: MegaD = hass.data[DOMAIN][mega_id]
        await hub.save()
    else:
        for hub in hass.data[DOMAIN].values():
            if isinstance(hub, MegaD):
                await hub.save()


async def _get_port(hass: HomeAssistant, call: ServiceCall):
    port = call.data.get('port')
    mega_id = call.data.get('mega_id')
    if mega_id:
        hub: MegaD = hass.data[DOMAIN][mega_id]
        if port is None:
            await hub.get_all_ports(check_skip=True)
        elif isinstance(port, int):
            await hub.get_port(port)
        elif isinstance(port, list):
            for x in port:
                await hub.get_port(x)
        hub.updater.async_set_updated_data(hub.values)
    else:
        for hub in hass.data[DOMAIN][CONF_ALL].values():
            if not isinstance(hub, MegaD):
                continue
            if port is None:
                await hub.get_all_ports(check_skip=True)
            elif isinstance(port, int):
                await hub.get_port(port)
            elif isinstance(port, list):
                for x in port:
                    await hub.get_port(x)
            hub.updater.async_set_updated_data(hub.values)


async def _run_cmd(hass: HomeAssistant, call: ServiceCall):
    mega_id = call.data.get('mega_id')
    cmd = call.data.get('cmd')
    if mega_id:
        hub: MegaD = hass.data[DOMAIN][mega_id]
        await hub.request(cmd=cmd)
    else:
        for hub in hass.data[DOMAIN][CONF_ALL].values():
            await hub.request(cmd=cmd)
