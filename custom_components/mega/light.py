"""Platform for light integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial

import voluptuous as vol
import colorsys
import time

from homeassistant.components.light import (
    PLATFORM_SCHEMA as LIGHT_SCHEMA,
    LightEntity,
    ColorMode,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    CONF_PORT,
    CONF_UNIQUE_ID,
    CONF_ID,
    CONF_DOMAIN,
)
from homeassistant.core import HomeAssistant
from .entities import MegaOutPort, BaseMegaEntity, safe_int

from .hub import MegaD
from .const import (
    CONF_DIMMER,
    CONF_SWITCH,
    DOMAIN,
    CONF_CUSTOM,
    CONF_SKIP,
    CONF_LED,
    CONF_WS28XX,
    CONF_PORTS,
    CONF_WHITE_SEP,
    CONF_SMOOTH,
    CONF_ORDER,
    CONF_CHIP,
    RGB,
)
from .tools import int_ignore, map_reorder_rgb

lg = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=5)

# Validation of the user's configuration
_EXTENDED = {
    vol.Required(CONF_PORT): int,
    vol.Optional(CONF_NAME): str,
    vol.Optional(CONF_UNIQUE_ID): str,
}
_ITEM = vol.Any(int, _EXTENDED)
DIMMER = {vol.Required(CONF_DIMMER): [_ITEM]}
SWITCH = {vol.Required(CONF_SWITCH): [_ITEM]}
PLATFORM_SCHEMA = LIGHT_SCHEMA.extend(
    {
        vol.Optional(str, description="mega id"): {
            vol.Optional("dimmer", default=[]): [_ITEM],
            vol.Optional("switch", default=[]): [_ITEM],
        }
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, add_entities, discovery_info=None):
    lg.warning(
        "mega integration does not support yaml for lights, please use UI configuration"
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    mid = config_entry.data[CONF_ID]
    hub: MegaD = hass.data["mega"][mid]
    devices = []
    customize = hass.data.get(DOMAIN, {}).get(CONF_CUSTOM, {}).get(mid, {})
    skip = []
    if CONF_LED in customize:
        for entity_id, conf in customize[CONF_LED].items():
            ports = conf.get(CONF_PORTS) or [conf.get(CONF_PORT)]
            skip.extend(ports)
            devices.append(
                MegaRGBW(
                    mega=hub,
                    port=ports,
                    name=entity_id,
                    customize=conf,
                    id_suffix=entity_id,
                    config_entry=config_entry,
                )
            )
    for port, cfg in config_entry.data.get("light", {}).items():
        port = int_ignore(port)
        c = customize.get(port, {})
        if (
            c.get(CONF_SKIP, False)
            or port in skip
            or c.get(CONF_DOMAIN, "light") != "light"
        ):
            continue
        for data in cfg:
            hub.lg.debug(f"add light on port %s with data %s", port, data)
            light = MegaLight(mega=hub, port=port, config_entry=config_entry, **data)
            if "<" in light.name:
                continue
            devices.append(light)

    async_add_devices(devices)


class MegaLight(MegaOutPort, LightEntity):
    @property
    def supported_features(self):
        if self.dimmer:
            return LightEntityFeature.TRANSITION
        return LightEntityFeature(0)

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        if self.dimmer:
            return {ColorMode.BRIGHTNESS}
        return {ColorMode.ONOFF}

    @property
    def color_mode(self) -> ColorMode:
        if self.dimmer:
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF


class MegaRGBW(LightEntity, BaseMegaEntity):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._is_on = None
        self._brightness = None
        self._hs_color = None
        self._rgb_color: tuple[int, int, int] | None = None
        self._white_value = None
        self._task: asyncio.Task | None = None
        self._restore = None
        self.smooth: timedelta = self.customize[CONF_SMOOTH]
        self._color_order = self.customize.get(CONF_ORDER, "rgb")
        self._last_called: float = 0
        self._max_values = None

    @property
    def max_values(self) -> list:
        if self._max_values is None:
            if self.is_ws:
                self._max_values = [255] * 3
            else:
                self._max_values = [
                    255 if isinstance(x, int) else 4095 for x in self.port
                ]
        return self._max_values

    @property
    def chip(self) -> int:
        return self.customize.get(CONF_CHIP, 100)

    @property
    def is_ws(self):
        return self.customize.get(CONF_WS28XX)

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode.RGBW if len(self.port) == 4 else ColorMode.RGB}

    @property
    def color_mode(self) -> ColorMode:
        if len(self.port) == 4:
            return ColorMode.RGBW
        else:
            return ColorMode.RGB

    @property
    def white_value(self):
        if len(self.port) != 4:
            return None
        value = self._white_value
        if value is None and self._state is not None:
            rgbw = self._state.attributes.get("rgbw_color")
            if rgbw is not None:
                value = rgbw[3]
            else:
                value = self._state.attributes.get("white_value")
        return round(float(value or 0))

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        value = self._rgb_color
        if value is None and self._state is not None:
            if len(self.port) == 4:
                rgbw = self._state.attributes.get("rgbw_color")
                if rgbw is not None:
                    value = rgbw[:3]
            if value is None:
                value = self._state.attributes.get("rgb_color")
        if value is None:
            h, s = self.hs_color
            value = colorsys.hsv_to_rgb(h / 360, s / 100, 1)
            value = tuple(channel * 255 for channel in value)
        return tuple(round(channel) for channel in value[:3])

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        if len(self.port) == 4:
            return (*self.rgb_color, self.white_value)

    @property
    def brightness(self):
        return round(float(self.get_attribute("brightness", 0)))

    @property
    def hs_color(self):
        return tuple(self.get_attribute("hs_color", (0, 0)))

    @property
    def is_on(self):
        return self.get_attribute("is_on", False)

    @property
    def supported_features(self):
        return LightEntityFeature.TRANSITION

    def get_rgbw(self):
        if not self.is_on:
            return [0] * (3 if self.is_ws else len(self.port))

        color = list(self.rgb_color)
        if len(self.port) == 4:
            color.append(self.white_value)

        brightness = self.brightness / 255
        values = []
        for i, component in enumerate(color):
            component_scale = component / 255
            if not (
                i == 3
                and self.customize.get(CONF_WHITE_SEP, True)
            ):
                component_scale *= brightness
            values.append(round(component_scale * self.max_values[i]))

        if self.is_ws:
            # восстанавливаем мэпинг
            values = map_reorder_rgb(values, RGB, self._color_order)
        return values

    async def async_turn_on(self, **kwargs):
        if (time.time() - self._last_called) < 0.1:
            return
        self._last_called = time.time()
        self.lg.debug(f"turn on %s with kwargs %s", self.entity_id, kwargs)
        if self._restore is not None:
            self._restore.update(kwargs)
            kwargs = self._restore
            self._restore = None
        _before = self.get_rgbw()
        self._is_on = True
        if self._task is not None:
            self._task.cancel()
        self._task = asyncio.create_task(self.set_color(_before, **kwargs))

    async def async_turn_off(self, **kwargs):
        if (time.time() - self._last_called) < 0.1:
            return
        self._last_called = time.time()
        self._restore = {
            "brightness": self.brightness,
            (
                "rgbw_color"
                if len(self.port) == 4
                else "rgb_color"
            ): self.rgbw_color if len(self.port) == 4 else self.rgb_color,
        }
        _before = self.get_rgbw()
        self._is_on = False
        if self._task is not None:
            self._task.cancel()
        self._task = asyncio.create_task(self.set_color(_before, **kwargs))

    async def set_color(self, _before, **kwargs):
        transition = kwargs.get("transition")
        update_state = transition is not None and transition > 3
        for item, value in kwargs.items():
            if item == "rgb_color":
                self._set_rgb_color(value)
            elif item == "rgbw_color":
                self._set_rgb_color(value[:3])
                self._white_value = value[3]
            elif item == "hs_color":
                self._hs_color = tuple(value)
                rgb = colorsys.hsv_to_rgb(
                    value[0] / 360,
                    value[1] / 100,
                    1,
                )
                self._rgb_color = tuple(round(x * 255) for x in rgb)
            else:
                setattr(self, f"_{item}", value)
        _after = self.get_rgbw()
        self._update_from_rgb(_after)
        if transition is None:
            transition = self.smooth.total_seconds()
            ratio = self.calc_speed_ratio(_before, _after)
            transition = transition * ratio
        self.async_write_ha_state()
        ports = self.port if not self.is_ws else self.port * 3
        config = [(port, _before[i], _after[i]) for i, port in enumerate(ports)]
        try:
            await self.mega.smooth_dim(
                *config,
                time=transition,
                ws=self.is_ws,
                jitter=50,
                updater=partial(self._update_from_rgb, update_state=update_state),
                can_smooth_hardware=self.can_smooth_hardware,
                max_values=self.max_values,
                chip=self.chip,
            )
        except asyncio.CancelledError:
            return
        except:
            self.lg.exception("while dimming")

    def _set_rgb_color(self, rgb):
        rgb = tuple(round(x) for x in rgb)
        self._rgb_color = rgb
        h, s, _ = colorsys.rgb_to_hsv(*[x / 255 for x in rgb])
        self._hs_color = (h * 360, s * 100)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._task is not None:
            self._task.cancel()

    def _update_from_rgb(self, rgbw, update_state=False):
        if len(self.port) == 4:
            w = rgbw[-1]
            rgb = rgbw[:3]
        else:
            w = None
            rgb = rgbw
        if self.is_ws:
            rgb = map_reorder_rgb(rgb, self._color_order, RGB)

        if not self.is_on:
            if update_state:
                self.async_write_ha_state()
            return

        levels = [
            channel / self.max_values[i] * 255
            for i, channel in enumerate(rgb)
        ]
        if w is not None:
            white_level = w / self.max_values[-1] * 255
        else:
            white_level = None

        if white_level is not None and self.customize.get(CONF_WHITE_SEP, True):
            brightness = max(levels, default=0)
            if brightness:
                normalized_rgb = [x / brightness * 255 for x in levels]
            else:
                normalized_rgb = [0, 0, 0]
                brightness = 255 if white_level else 0
            normalized_white = white_level
        else:
            all_levels = levels + ([] if white_level is None else [white_level])
            brightness = max(all_levels, default=0)
            if brightness:
                normalized = [x / brightness * 255 for x in all_levels]
            else:
                normalized = [0] * len(all_levels)
            normalized_rgb = normalized[:3]
            normalized_white = normalized[3] if white_level is not None else None

        self._brightness = round(brightness)
        self._rgb_color = tuple(round(x) for x in normalized_rgb)
        h, s, _ = colorsys.rgb_to_hsv(
            *[x / 255 for x in self._rgb_color]
        )
        self._hs_color = (h * 360, s * 100)
        if normalized_white is not None:
            self._white_value = round(normalized_white)
        # print(f'updated state {self.hs_color=} {self.brightness=}')
        if update_state:
            self.async_write_ha_state()

    async def async_update(self):
        """
        Эта штука нужна для синхронизации статуса вкл/выкл с реальностью. Если все цвета сброшены в ноль, значит мега
        рестартнулась и не запомнила настройки, поэтому извещаем HA о выключении
        Если вручную править цвет на стороне меги, тут изменения отражаться не будут
        :return:
        """
        if not self.enabled:
            return
        rgbw = []
        for x in self.port:
            data = self.coordinator.data
            if not isinstance(data, dict):
                return
            data = data.get(x, None)
            if isinstance(data, dict):
                data = data.get("value")
            data = safe_int(data)
            if data is None:
                return
            rgbw.append(data)
        if sum(rgbw) == 0:
            self._is_on = False
        self.async_write_ha_state()

    def calc_speed_ratio(self, _before, _after):
        ret = None
        for i, x in enumerate(_before):
            r = abs(x - _after[i]) / self.max_values[i]
            if ret is None:
                ret = r
            else:
                ret = max([r, ret])
        return ret
