"""Config flow for nespresso integration."""
from __future__ import annotations

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
    BluetoothScanningMode,
    BluetoothServiceInfo,
    async_discovered_service_info,
    async_process_advertisements,
    async_ble_device_from_address
)

from .machines import supported
from .nespresso import NespressoClient
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NESPRESSO_SERVICE_UUID = "06aa1940-f22a-11e3-9daa-0002a5d5c51b"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for nespresso."""
    _discovered_devices: dict = {}

    VERSION = 1

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovery is not None

        self._set_confirm_only()
        assert self._discovery.name
        placeholders = {"name": self._discovery.name}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
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

            try:
                device = NespressoClient(mac=discovered.address)
                if user_input.get(CONF_TOKEN):
                    device.auth_code = user_input.get(CONF_TOKEN)
                ble_device = async_ble_device_from_address(self.hass, discovered.address, connectable=True)
                if ble_device is None:
                    raise CannotConnect(f"Device {discovered.address} not found via Bluetooth")
                await device.connect(ble_device)
                await device.load_model()
                await device.disconnect()
            except Exception as e:
                _LOGGER.error(f"Failed to connect to device: {e}")
                return self.async_show_form(
                    step_id="user",
                    errors={"base": "cannot_connect"}
                )

            address = discovered.address
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create_nespresso_entry(device)

        configured_addresses = self._async_current_ids()

        for info in async_discovered_service_info(self.hass, connectable=True):
            address = info.address
            if address in configured_addresses:
                continue
            if NESPRESSO_SERVICE_UUID in info.service_uuids:
                label = info.name or address
                self._discovered_devices[label] = info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): vol.In(
                        [
                            d.name
                            for d in self._discovered_devices.values()
                        ]
                    ),
                    vol.Optional(CONF_TOKEN): cv.string
                }
            )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema
        )

    def _create_nespresso_entry(self, device) -> FlowResult:
        assert self._discovery.name
        return self.async_create_entry(
            title=self._discovery.name,
            data={
                CONF_ADDRESS: device.address,
                CONF_TOKEN: device.auth_code,
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
