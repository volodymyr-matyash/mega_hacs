"""Compatibility tests for current Home Assistant releases."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pkgutil
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.http import KEY_HASS
from homeassistant.components.light import ColorMode, valid_supported_color_modes
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed

import custom_components.mega as mega_integration
from custom_components.mega import hub as mega_hub
from custom_components.mega.config_flow import ConfigFlow, OptionsFlowHandler
from custom_components.mega.const import (
    CONF_ALL,
    CONF_FAKE_RESPONSE,
    CONF_NPORTS,
    CONF_RANGE,
    CONF_RELOAD,
    CONF_SCAN_INTERVAL,
    CONF_SMOOTH,
    CONF_WHITE_SEP,
    CONF_WS28XX,
    CONFIG_OPTION_KEYS,
    DOMAIN,
)
from custom_components.mega.http import MegaView
from custom_components.mega.light import MegaLight, MegaRGBW
from custom_components.mega.tools import PriorityLock


def test_all_modules_import() -> None:
    """Every integration module imports against the installed HA version."""
    package = importlib.import_module("custom_components.mega")
    for module in pkgutil.iter_modules(package.__path__, f"{package.__name__}."):
        importlib.import_module(module.name)


def _rgb_light(
    port,
    *,
    brightness=255,
    rgb=(255, 0, 0),
    white=0,
    ws=False,
    white_sep=False,
    order="rgb",
    max_values=None,
):
    light = object.__new__(MegaRGBW)
    light.port = port
    light._is_on = True
    light._brightness = brightness
    light._rgb_color = rgb
    light._white_value = white
    light._hs_color = (0, 100)
    light._state = None
    light._customize = {
        CONF_WS28XX: ws,
        CONF_WHITE_SEP: white_sep,
    }
    light._color_order = order
    light._max_values = max_values
    return light


@pytest.mark.parametrize(
    ("light", "expected"),
    [
        (_rgb_light([1, 2, 3], rgb=(0, 0, 0)), [0, 0, 0]),
        (_rgb_light([1, 2, 3], rgb=(128, 0, 0)), [128, 0, 0]),
        (_rgb_light([1, 2, 3], brightness=128), [128, 0, 0]),
        (
            _rgb_light([1, 2, 3, 4], rgb=(0, 0, 0), white=255),
            [0, 0, 0, 255],
        ),
        (
            _rgb_light(
                [1, 2, 3, 4],
                brightness=128,
                white=128,
            ),
            [128, 0, 0, 64],
        ),
        (
            _rgb_light(
                ["1e0", "1e1", "1e2"],
                brightness=128,
                max_values=[4095, 4095, 4095],
            ),
            [2056, 0, 0],
        ),
        (
            _rgb_light([1], rgb=(10, 20, 30), ws=True, order="grb"),
            [20, 10, 30],
        ),
    ],
)
def test_rgb_hardware_values_round_trip(light, expected) -> None:
    """RGB(W) commands preserve physical channel values and channel order."""
    values = light.get_rgbw()
    assert values == expected

    light._update_from_rgb(values)
    assert light.get_rgbw() == expected


def test_separate_white_channel_round_trip() -> None:
    """The documented independent white-channel mode remains compatible."""
    light = _rgb_light(
        [1, 2, 3, 4],
        brightness=64,
        white=128,
        white_sep=True,
    )
    values = light.get_rgbw()
    assert values == [64, 0, 0, 128]

    light._update_from_rgb(values)
    assert light.get_rgbw() == values


def test_rgbw_restore_prefers_exact_rgbw_state() -> None:
    """Restoring pure white must not energize all RGB channels."""
    light = _rgb_light([1, 2, 3, 4], rgb=(0, 0, 0), white=0)
    light._rgb_color = None
    light._white_value = None
    light._state = SimpleNamespace(
        attributes={
            "rgb_color": (255, 255, 255),
            "rgbw_color": (0, 0, 0, 255),
        }
    )

    assert light.rgbw_color == (0, 0, 0, 255)
    assert light.get_rgbw() == [0, 0, 0, 255]


def test_light_color_modes_are_valid() -> None:
    """HA 2026 validates these modes while writing entity state."""
    dimmer = object.__new__(MegaLight)
    dimmer.dimmer = True
    switch = object.__new__(MegaLight)
    switch.dimmer = False

    assert valid_supported_color_modes(dimmer.supported_color_modes) == {
        ColorMode.BRIGHTNESS
    }
    assert valid_supported_color_modes(switch.supported_color_modes) == {
        ColorMode.ONOFF
    }
    assert valid_supported_color_modes(
        _rgb_light([1, 2, 3]).supported_color_modes
    ) == {ColorMode.RGB}
    assert valid_supported_color_modes(
        _rgb_light([1, 2, 3, 4]).supported_color_modes
    ) == {ColorMode.RGBW}


def test_light_constructors_keep_legacy_unique_ids() -> None:
    """The rescue release must not create replacement entities in HA."""
    hub = SimpleNamespace(
        id="main",
        entities=[],
        new_naming=False,
        ds2413_ports=set(),
        updater=SimpleNamespace(last_update_success=True),
        subscribe=Mock(),
        smooth=[],
        online=True,
        values={},
        last_long={},
        fw="1.0",
    )

    output = MegaLight(mega=hub, port=12)
    rgbw = MegaRGBW(
        mega=hub,
        port=[1, 2, 3, 4],
        id_suffix="kitchen",
        name="Kitchen",
        customize={
            CONF_SMOOTH: timedelta(seconds=1),
            CONF_WHITE_SEP: True,
        },
    )

    assert output.unique_id == "mega_main_12"
    assert rgbw.unique_id == "mega_main_kitchen"
    hub.subscribe.assert_called_once()


@pytest.mark.asyncio
async def test_extender_transition_uses_hardware_scale_without_zero_division() -> None:
    """A no-op transition on a 12-bit dimmer remains safe and correctly scaled."""
    hub = SimpleNamespace(
        id="main",
        entities=[],
        new_naming=False,
        ds2413_ports=set(),
        updater=SimpleNamespace(last_update_success=True),
        subscribe=Mock(),
        smooth=[],
        online=True,
        values={"1e0": "2048"},
        last_long={},
        fw="1.0",
        smooth_dim=AsyncMock(),
        request=AsyncMock(),
    )
    light = MegaLight(
        mega=hub,
        port="1e0",
        dimmer=True,
        dimmer_scale=16,
    )
    light.get_state = AsyncMock()

    await light.async_turn_on(brightness=128, transition=1)
    await light.task

    config = hub.smooth_dim.await_args.args[0]
    assert config == ("1e0", 2048, 2048)

    hub.values["1e0"] = 0
    assert light.brightness == 128
    assert light.is_on is False


@pytest.mark.asyncio
async def test_transition_respects_custom_dimmer_range() -> None:
    """Smooth transitions start from the physical custom-range value."""
    hub = SimpleNamespace(
        id="main",
        entities=[],
        new_naming=False,
        ds2413_ports=set(),
        updater=SimpleNamespace(last_update_success=True),
        subscribe=Mock(),
        smooth=[],
        online=True,
        values={1: {"value": 100}},
        last_long={},
        fw="1.0",
        lg=logging.getLogger("test.megad.range"),
        smooth_dim=AsyncMock(),
        request=AsyncMock(),
    )
    light = MegaLight(mega=hub, port=1, dimmer=True)
    light._customize = {CONF_RANGE: [10, 200]}
    light.get_state = AsyncMock()

    await light.async_turn_on(brightness=200, transition=1)
    await light.task

    config = hub.smooth_dim.await_args.args[0]
    assert config == (1, 100, 159)


@pytest.mark.asyncio
async def test_noop_hardware_transition_returns_without_dividing_by_zero() -> None:
    """Unchanged hardware channels do not calculate an infinite transition."""
    hub = object.__new__(mega_hub.MegaD)
    hub.request = AsyncMock()
    update = Mock()

    await hub.smooth_dim(
        (1, 128, 128),
        time=1,
        updater=update,
        can_smooth_hardware=True,
        max_values=[255],
    )

    update.assert_called_once_with((128,))
    hub.request.assert_not_awaited()


class _FakeConfigEntries:
    def __init__(self, entry=None):
        self.entry = entry
        self.updated = []
        self.reloads = []

    def async_get_known_entry(self, entry_id):
        assert self.entry.entry_id == entry_id
        return self.entry

    def async_update_entry(self, entry, **kwargs):
        self.updated.append((entry, kwargs))
        for key, value in kwargs.items():
            if key in {"data", "options"}:
                value = MappingProxyType(value)
            object.__setattr__(entry, key, value)
        return True

    def async_schedule_reload(self, entry_id):
        self.reloads.append(entry_id)


def _options_flow(entry, hass):
    flow = OptionsFlowHandler()
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "options-flow"
    flow.context = {}
    return flow


def _config_entry(*, entry_id, data, options=None, version=26):
    return ConfigEntry(
        data=data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options=options or {},
        source="user",
        subentries_data=(),
        title="MegaD",
        unique_id=None,
        version=version,
    )


@pytest.mark.asyncio
async def test_options_flow_keeps_only_scalar_options() -> None:
    """Legacy structural options cannot override the config entry data."""
    entry = _config_entry(
        entry_id="entry-1",
        data={
            "id": "main",
            CONF_PASSWORD: "secret",
            "light": {1: [{}]},
        },
        options={
            CONF_SCAN_INTERVAL: 10,
            "light": {99: [{}]},
            CONF_PASSWORD: "stale-secret",
        },
    )
    config_entries = _FakeConfigEntries(entry)
    hass = SimpleNamespace(config_entries=config_entries, data={DOMAIN: {}})
    flow = _options_flow(entry, hass)

    result = await flow.async_step_init(
        {
            CONF_RELOAD: False,
            CONF_SCAN_INTERVAL: 30,
            CONF_FAKE_RESPONSE: True,
        }
    )

    assert result["data"] == {
        CONF_SCAN_INTERVAL: 30,
        CONF_FAKE_RESPONSE: True,
    }
    assert set(result["data"]) <= CONFIG_OPTION_KEYS
    assert CONF_PASSWORD not in result["data"]
    assert "light" not in result["data"]


@pytest.mark.asyncio
async def test_options_rescan_updates_data_and_options_atomically() -> None:
    """A rescan stores entity structure in data, not in options."""
    entry = _config_entry(
        entry_id="entry-2",
        data={"id": "main", CONF_PASSWORD: "secret", CONF_NPORTS: 37},
        options={},
    )
    scanned = {
        "id": "main",
        CONF_PASSWORD: "secret",
        CONF_NPORTS: 64,
        "light": {10: [{}]},
    }
    hub = SimpleNamespace(reload=AsyncMock(return_value=scanned))
    config_entries = _FakeConfigEntries(entry)
    hass = SimpleNamespace(
        config_entries=config_entries,
        data={DOMAIN: {"main": hub}},
    )
    flow = _options_flow(entry, hass)

    result = await flow.async_step_init(
        {
            CONF_RELOAD: True,
            CONF_NPORTS: 64,
            CONF_SCAN_INTERVAL: 15,
        }
    )

    assert result["data"] == {CONF_NPORTS: 64, CONF_SCAN_INTERVAL: 15}
    _, update = config_entries.updated[-1]
    assert update["data"]["light"] == {10: [{}]}
    assert update["data"][CONF_PASSWORD] == "secret"
    assert update["options"] == result["data"]
    base_config = hub.reload.await_args.kwargs["base_config"]
    assert base_config[CONF_NPORTS] == 64


@pytest.mark.asyncio
async def test_legacy_migration_preserves_password(monkeypatch) -> None:
    """Migration uses the supported ConfigEntry API and preserves credentials."""
    entry = _config_entry(
        entry_id="entry-3",
        version=25,
        data={"id": "main", CONF_PASSWORD: "secret", "light": {1: [{}]}},
        options={},
    )
    hub = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        get_config=AsyncMock(return_value={"light": {2: [{}]}}),
    )

    async def get_hub(_hass, _entry):
        return hub

    monkeypatch.setattr(mega_integration, "get_hub", get_hub)
    config_entries = _FakeConfigEntries(entry)
    hass = SimpleNamespace(config_entries=config_entries)

    assert await mega_integration.async_migrate_entry(hass, entry)
    _, update = config_entries.updated[-1]
    assert update["version"] == ConfigFlow.VERSION == 26
    assert update["data"][CONF_PASSWORD] == "secret"
    hub.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_setup_removes_runtime_hub(monkeypatch) -> None:
    """A failed platform setup cannot leave a ghost HTTP hub behind."""
    entry = SimpleNamespace(
        entry_id="entry-4",
        data={"id": "main"},
        add_update_listener=Mock(),
        async_on_unload=Mock(),
    )
    updater = SimpleNamespace(async_refresh=AsyncMock())
    hub = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        updater=updater,
        register_http=Mock(),
    )

    async def add_mega(_hass, _entry):
        return hub

    monkeypatch.setattr(mega_integration, "_add_mega", add_mega)
    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(side_effect=RuntimeError("boom")),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    hass = SimpleNamespace(
        data={DOMAIN: {CONF_ALL: {}}},
        config_entries=config_entries,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await mega_integration.async_setup_entry(hass, entry)

    hub.stop.assert_awaited_once()
    config_entries.async_unload_platforms.assert_awaited_once()
    hub.register_http.assert_not_called()
    assert "main" not in hass.data[DOMAIN]
    assert "main" not in hass.data[DOMAIN][CONF_ALL]


@pytest.mark.asyncio
async def test_cancelled_setup_removes_runtime_hub(monkeypatch) -> None:
    """HA shutdown during setup cannot leave a live MegaD hub behind."""
    entry = SimpleNamespace(
        entry_id="entry-cancelled",
        data={"id": "main"},
        add_update_listener=Mock(),
        async_on_unload=Mock(),
    )
    hub = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        updater=SimpleNamespace(async_refresh=AsyncMock()),
        register_http=Mock(),
    )

    async def add_mega(_hass, _entry):
        return hub

    monkeypatch.setattr(mega_integration, "_add_mega", add_mega)
    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(
            side_effect=asyncio.CancelledError
        ),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    hass = SimpleNamespace(
        data={DOMAIN: {CONF_ALL: {}}},
        config_entries=config_entries,
    )

    with pytest.raises(asyncio.CancelledError):
        await mega_integration.async_setup_entry(hass, entry)

    hub.stop.assert_awaited_once()
    config_entries.async_unload_platforms.assert_awaited_once()
    assert "main" not in hass.data[DOMAIN]
    assert "main" not in hass.data[DOMAIN][CONF_ALL]


@pytest.mark.asyncio
async def test_setup_and_unload_existing_entry(monkeypatch) -> None:
    """An existing v26 entry loads without migration and unloads cleanly."""
    entry = _config_entry(
        entry_id="entry-loaded",
        data={"id": "main", CONF_PASSWORD: "secret", "light": {1: [{}]}},
    )
    events = []
    hub = SimpleNamespace(
        start=AsyncMock(side_effect=lambda: events.append("start")),
        stop=AsyncMock(side_effect=lambda: events.append("stop")),
        updater=SimpleNamespace(
            async_refresh=AsyncMock(side_effect=lambda: events.append("refresh"))
        ),
        register_http=Mock(side_effect=lambda: events.append("register_http")),
    )

    async def add_mega(_hass, _entry):
        return hub

    monkeypatch.setattr(mega_integration, "_add_mega", add_mega)

    async def forward(_entry, _platforms):
        events.append("forward")

    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(side_effect=forward),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    hass = SimpleNamespace(
        data={DOMAIN: {CONF_ALL: {}}},
        config_entries=config_entries,
    )

    assert await mega_integration.async_setup_entry(hass, entry)
    assert events == ["start", "forward", "refresh", "register_http"]
    assert hass.data[DOMAIN]["main"] is hub

    assert await mega_integration.async_unload_entry(hass, entry)
    assert events[-1] == "stop"
    assert "main" not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_invalid_runtime_password_requests_reauthentication(monkeypatch) -> None:
    """A rejected password gets HA's standard reauthentication state."""
    entry = _config_entry(
        entry_id="entry-auth",
        data={"id": "main", CONF_PASSWORD: "wrong"},
    )
    hub = SimpleNamespace(authenticate=AsyncMock(return_value=False))

    async def get_hub(_hass, _entry):
        return hub

    monkeypatch.setattr(mega_integration, "get_hub", get_hub)

    with pytest.raises(ConfigEntryAuthFailed):
        await mega_integration._add_mega(SimpleNamespace(), entry)


@pytest.mark.asyncio
async def test_unload_platforms_without_runtime_hub() -> None:
    """Platforms are unloaded even after partial runtime cleanup."""
    entry = SimpleNamespace(entry_id="entry-5", data={"id": "missing"})
    config_entries = SimpleNamespace(
        async_unload_platforms=AsyncMock(return_value=True)
    )
    hass = SimpleNamespace(
        data={DOMAIN: {CONF_ALL: {}}},
        config_entries=config_entries,
    )

    assert await mega_integration.async_unload_entry(hass, entry)
    config_entries.async_unload_platforms.assert_awaited_once()


@pytest.mark.asyncio
async def test_hub_reload_uses_new_port_count_and_ignores_stale_options() -> None:
    """The same rescan uses a newly selected nports value."""
    hub = object.__new__(mega_hub.MegaD)
    hub.nports = 37
    hub.lg = logging.getLogger("test.megad.reload")
    hub.config = _config_entry(
        entry_id="entry-6",
        data={"id": "main", CONF_PASSWORD: "secret"},
        options={"light": {99: [{}]}},
    )
    get_config = AsyncMock(return_value={"light": {10: [{}]}})
    hub.get_config = get_config
    config_entries = _FakeConfigEntries(hub.config)
    hub.hass = SimpleNamespace(config_entries=config_entries)

    result = await hub.reload(
        reload_entry=False,
        base_config={
            "id": "main",
            CONF_PASSWORD: "secret",
            CONF_NPORTS: 64,
        },
    )

    get_config.assert_awaited_once_with(nports=64)
    assert result["light"] == {10: [{}]}
    assert result[CONF_PASSWORD] == "secret"

    get_config.reset_mock()
    result = await hub.reload(reload_entry=False)
    get_config.assert_awaited_once_with(nports=37)
    assert result["light"] == {10: [{}]}


@pytest.mark.asyncio
async def test_http_fake_response_returns_aiohttp_response() -> None:
    """The callback handler always returns a valid aiohttp Response."""
    view = MegaView({})
    view.protected = False
    callback = Mock()
    view.callbacks["main"][1].append(callback)
    hub = SimpleNamespace(
        id="main",
        force_d=True,
        fake_response=True,
        update_all=False,
        binary_sensors=[1],
        extenders=[],
        ext_in={},
        ext_act={},
        values={},
        new_naming=True,
        def_response=None,
        lg=logging.getLogger("test.megad.http"),
        request=AsyncMock(),
    )
    view.hubs["192.0.2.5"] = hub
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=Mock()),
    )
    request = SimpleNamespace(
        remote="192.0.2.5",
        headers={},
        query={"pt": "1", "m": "1"},
        app={KEY_HASS: hass},
    )

    response = await view.get(request)

    assert response.status == 200
    assert response.text == ""
    callback.assert_called_once()
    hub.request.assert_awaited_once_with(pt=1, cmd="d")


@pytest.mark.asyncio
async def test_request_logs_do_not_expose_password(monkeypatch, caplog) -> None:
    """HTTP credentials remain out of debug and timeout messages."""
    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self, **_kwargs):
            return "ok"

    monkeypatch.setattr(mega_hub.aiohttp, "request", lambda *_a, **_k: FakeResponse())
    hub = object.__new__(mega_hub.MegaD)
    hub.host = "192.0.2.5"
    hub.sec = "super-secret"
    hub.lg = logging.getLogger("test.megad.secret")
    hub._http_lck = PriorityLock()

    with caplog.at_level(logging.DEBUG):
        assert await hub.request(cmd="all") == "ok"

    assert "super-secret" not in caplog.text


def test_manifest_targets_current_ha_without_changing_domain() -> None:
    """Package metadata keeps the installed domain and pinned dependencies."""
    root = Path(__file__).parents[1]
    manifest = json.loads(
        (root / "custom_components" / "mega" / "manifest.json").read_text()
    )
    hacs = json.loads((root / "hacs.json").read_text())

    assert manifest["domain"] == DOMAIN
    assert manifest["version"] == "v1.2.0b1"
    assert manifest["iot_class"] == "local_push"
    assert "beautifulsoup4==4.13.3" in manifest["requirements"]
    assert "lxml==6.1.1" in manifest["requirements"]
    assert hacs["homeassistant"] == "2026.8.0"
