"""Config flow for nespresso integration."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.components.bluetooth import (
    BluetoothServiceInfo,
    async_discovered_service_info,
    async_ble_device_from_address,
)

from .nespresso import NespressoClient
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NESPRESSO_SERVICE_UUIDS = {
    "06aa1940-f22a-11e3-9daa-0002a5d5c51b",  # Original line (Expert, Prodigio)
    "06aa1910-f22a-11e3-9daa-0002a5d5c51b",  # Vertuo line (Vertuo Pop, etc.)
}


def _auth_code_from_mac(mac: str) -> str:
    return hashlib.md5(mac.replace(":", "").encode()).hexdigest()[:16]


async def _connect_and_pair(hass: HomeAssistant, address: str) -> tuple[bool, str]:
    """Connect to device and attempt BLE pairing. Returns (paired, error_key)."""
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        return False, "cannot_connect"

    try:
        client = await asyncio.wait_for(
            establish_connection(BleakClient, ble_device, address),
            timeout=20.0,
        )
    except Exception as e:
        _LOGGER.error("Failed to connect to %s during setup: %s", address, e)
        return False, "cannot_connect"

    paired = False
    try:
        await asyncio.wait_for(client.pair(), timeout=20.0)
        paired = True
        _LOGGER.debug("BLE pairing succeeded for %s", address)
    except Exception as e:
        _LOGGER.warning("BLE pairing failed for %s: %s", address, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return paired, ("cannot_pair" if not paired else "")


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for nespresso."""

    VERSION = 1

    _discovered_devices: dict = {}

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Prompt user to hold Bluetooth button then attempt pairing."""
        assert self._discovery is not None
        name = self._discovery.name or self._discovery.address

        if user_input is not None:
            paired, error_key = await _connect_and_pair(
                self.hass, self._discovery.address
            )
            if not paired:
                return self.async_show_form(
                    step_id="bluetooth_confirm",
                    description_placeholders={"name": name},
                    errors={"base": error_key},
                )

            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: self._discovery.address,
                    CONF_TOKEN: _auth_code_from_mac(self._discovery.address),
                },
            )

        self.context["title_placeholders"] = {"name": name}
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            name = user_input[CONF_NAME]
            discovered = self._discovered_devices[name]
            assert discovered is not None
            self._discovery = discovered

            paired, error_key = await _connect_and_pair(
                self.hass, discovered.address
            )
            if not paired:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(),
                    errors={"base": error_key},
                )

            address = discovered.address
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: address,
                    CONF_TOKEN: _auth_code_from_mac(address),
                },
            )

        configured_addresses = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            address = info.address
            if address in configured_addresses:
                continue
            if NESPRESSO_SERVICE_UUIDS.intersection(info.service_uuids):
                label = info.name or address
                self._discovered_devices[label] = info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
        )

    def _user_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_NAME): vol.In(
                    [d.name for d in self._discovered_devices.values()]
                ),
            }
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Allow re-pairing and token override."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if user_input.get("repair"):
                return await self.async_step_repair()
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TOKEN,
                        default=self._config_entry.options.get(
                            CONF_TOKEN,
                            self._config_entry.data.get(CONF_TOKEN, ""),
                        ),
                    ): cv.string,
                    vol.Optional("repair", default=False): bool,
                }
            ),
        )

    async def async_step_repair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-pair step: user holds button then clicks Submit."""
        address = self._config_entry.data.get(CONF_ADDRESS)

        if user_input is not None:
            paired, error_key = await _connect_and_pair(
                self.hass, address
            )
            if not paired:
                return self.async_show_form(
                    step_id="repair",
                    description_placeholders={"address": address},
                    errors={"base": error_key},
                )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="repair",
            description_placeholders={"address": address},
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
