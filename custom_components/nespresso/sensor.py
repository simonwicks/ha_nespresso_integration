"""
Support for Nespresso Connected mmachine.
https://www.nespresso.com

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.Nespresso/
"""
import logging
from datetime import timedelta, datetime

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.const import (ATTR_DEVICE_CLASS, ATTR_ICON, CONF_ADDRESS,
                                 CONF_NAME, CONF_RESOURCE, CONF_SCAN_INTERVAL,
                                 CONF_UNIT_SYSTEM,
                                 EVENT_HOMEASSISTANT_STOP, STATE_UNKNOWN,
                                 CONF_TOKEN)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.bluetooth import async_ble_device_from_address

from .nespresso import NespressoClient
from .machines import Temprature, BrewType
from bleak import BleakClient
from bleak_retry_connector import establish_connection

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)

DEVICE_CLASS_CAPS='caps'
CAPS_UNITS = 'caps'

from .const import DOMAIN

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_ADDRESS, default=''): cv.string,
    vol.Required(CONF_TOKEN): cv.string,
    vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL): cv.time_period,
})


class Sensor:
    def __init__(self, unit, unit_scale, device_class, icon):
        self.unit = unit
        self.unit_scale = unit_scale
        self.device_class = device_class
        self.icon = icon

    def set_unit_scale(self, unit, unit_scale):
        self.unit = unit
        self.unit_scale = unit_scale

    def get_extra_attributes(self, data):
        return {}


DEVICE_SENSOR_SPECIFICS = { "state":Sensor(None, None, None, None),
                            "water_is_empty":Sensor(None, None, None, 'mdi:water-off'),
                            "descaling_needed":Sensor(None, None, None, 'mdi:silverware-clean'),
                            "capsule_mechanism_jammed":Sensor(None, None, None, None),
                            "always_1":Sensor(None, None, None, 'mdi:numeric-1'),
                            "water_temp_low":Sensor(None, None, None, 'mdi:snowflake-alert'),
                            "awake":Sensor(None, None, None, 'mdi:sleep-off'),
                            "water_engadged":Sensor(None, None, None, None),
                            "sleeping":Sensor(None, None, None, 'mdi:sleep'),
                            "tray_sensor_during_brewing":Sensor(None, None, None, None),
                            "tray_open_tray_sensor_full":Sensor(None, None, None, 'mdi:coffee-off-outline'),
                            "capsule_engaged":Sensor(None, None, None, None),
                            "Fault":Sensor(None, None, None, 'mdi:alert-circle-outline'),
                            "descaling_counter":Sensor(None, None, None, 'mdi:silverware-clean'),
                            "water_hardness":Sensor(None, None, None, 'mdi:water-percent'),
                            "slider":Sensor(None, None, BinarySensorDeviceClass.DOOR, 'mdi:gate-and'),
                            "caps_number": Sensor(CAPS_UNITS, None, DEVICE_CLASS_CAPS, 'mdi:counter'),
                            "water_fresh": Sensor(None, None, None, None)
                           }


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback, discovery_info=None) -> None:
    """Set up the Nespresso sensor."""
    scan_interval = SCAN_INTERVAL
    mac = config.data.get(CONF_ADDRESS)
    auth = config.data.get(CONF_TOKEN)

    _LOGGER.debug("Create the top level device..")
    device_registry = dr.async_get(hass)

    _LOGGER.debug("Searching for Nespresso sensors...")
    try:
        Nespressodetect = NespressoClient(scan_interval, auth, mac)
        ble_device = async_ble_device_from_address(hass, mac, connectable=True)
        if ble_device is None:
            _LOGGER.error("Device %s not found via Bluetooth — is it in range?", mac)
            raise ConfigEntryNotReady(f"Nespresso device {mac} not found via Bluetooth")
        _LOGGER.debug("BLE device found: %s, attempting connect", ble_device.name)
        connected = await Nespressodetect.connect(ble_device)
        if not connected:
            _LOGGER.error("connect() returned False for %s", mac)
            raise ConfigEntryNotReady(f"Failed to connect to Nespresso device {mac}")
    except ConfigEntryNotReady:
        raise
    except Exception as e:
        _LOGGER.error("Exception during connect for %s: %s", mac, e, exc_info=True)
        raise ConfigEntryNotReady(f"Failed to connect to Nespresso device: {e}") from e

    try:
        _LOGGER.debug("Getting info about device(s)")
        devices_info = await Nespressodetect.get_info()
        for mac, dev in devices_info.items():
            _LOGGER.info("{}: {}".format(mac, dev))

        NespressoDeviceEntry = device_registry.async_get_or_create(
            config_entry_id=config.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, mac)},
            identifiers={(DOMAIN, mac)},
            manufacturer=getattr(dev, 'manufacturer', 'Nespresso'),
            suggested_area="Kitchen",
            name=dev.name,
            model=dev.model.name if dev.model else None,
            sw_version=dev.fw_version,
            hw_version=dev.hw_version,
            serial_number=dev.serial,
        )


        _LOGGER.debug("Getting sensors")
        devices_sensors = await Nespressodetect.get_sensors()
        for mac, sensors in devices_sensors.items():
            for sensor in sensors:
                _LOGGER.debug("{}: Found sensor UUID: {}".format(mac, sensor))

        _LOGGER.debug("Get initial sensor data to populate HA entities")
        ha_entities = []
        sensordata = await Nespressodetect.get_sensor_data()
        if not sensordata:
            _LOGGER.warning("No sensor data returned from device — no characteristic UUIDs matched. The device may use different GATT characteristics.")
        else:
            for mac, data in sensordata.items():
                for name, val in data.items():
                    _LOGGER.debug("{}: {}: {}".format(mac, name, val))
                    if name not in DEVICE_SENSOR_SPECIFICS:
                        _LOGGER.warning("Unknown sensor '%s' with value '%s' — skipping", name, val)
                        continue
                    ha_entities.append(NespressoSensor(mac, auth, name, Nespressodetect, devices_info[mac].manufacturer,
                                                       DEVICE_SENSOR_SPECIFICS[name], NespressoDeviceEntry))

        await Nespressodetect.disconnect()
    except Exception:
        _LOGGER.exception("Failed initial setup.")
        return

    async_add_entities(ha_entities, True)

    async def brew(call):
        """Send a brew command."""
        try:
            brewType = BrewType[call.data.get('brew_type').upper()] if call.data.get('brew_type') else None
            temprature = Temprature[call.data.get('brew_temp').upper()] if call.data.get('brew_temp') else Temprature.MEDIUM
            coffee_ml = call.data.get('coffee_ml')
            water_ml = call.data.get('water_ml')
        except KeyError:
            brewType = None
            _LOGGER.debug(f"Brew Failed - Recipe: {brewType}, Temp: {temprature}")

        try:
            ble_device = async_ble_device_from_address(hass, mac, connectable=True)
            if ble_device is None:
                _LOGGER.error(f"Nespresso device {mac} not found via Bluetooth")
                return None
            conn_status = await Nespressodetect.connect(ble_device)
            if conn_status:
                if coffee_ml and water_ml:
                    response = await Nespressodetect.brew_custom(coffee_ml=coffee_ml, water_ml=water_ml, temp=temprature)
                else:
                    response = await Nespressodetect.brew_predefined(brew=brewType, temp=temprature)
                await Nespressodetect.disconnect()
                return response
            _LOGGER.error(f"Connection failed with {ble_device.name}")
            return None
        except Exception:
            _LOGGER.debug(f"Brew Failed - Recipe: {brewType}, Temp: {temprature}")

        return None

    async def caps(call):
        """Update the caps counter"""
        caps = call.data.get('caps')

        try:
            ble_device = async_ble_device_from_address(hass, mac, connectable=True)
            if ble_device is None:
                _LOGGER.error(f"Nespresso device {mac} not found via Bluetooth")
                return None
            conn_status = await Nespressodetect.connect(ble_device)
            if conn_status:
                if caps:
                    caps = int(round(caps))
                    await Nespressodetect.update_caps_counter(caps)
                    await Nespressodetect.disconnect()
                    Nespressodetect.sensordata[mac]['caps_number'] = caps
                    _LOGGER.debug(f'Cap Counter updated')
                    return True
            _LOGGER.error(f"Connection failed with {ble_device.name}")
            return None
        except Exception as e:
            _LOGGER.exception("Updating caps counter failed: %s", e)

        return None


    if not hass.services.has_service(DOMAIN, "coffee"):
        hass.services.async_register(DOMAIN, "coffee", brew)
    if not hass.services.has_service(DOMAIN, "caps"):
        hass.services.async_register(DOMAIN, "caps", caps)

class NespressoSensor(SensorEntity):
    """General Representation of an Nespresso sensor."""
    def __init__(self, mac, auth, name, device, device_info, sensor_specifics, device_entry):
        """Initialize a sensor."""
        self._device_entry = device_entry
        self.device = device
        self._mac = mac
        self.auth = auth
        self._name = '{}-{}'.format(device_info, name)
        _LOGGER.debug("Added sensor entity {}".format(self._name))
        self._sensor_name = name
        self._device_class = sensor_specifics.device_class
        self._state = STATE_UNKNOWN
        self._sensor_specifics = sensor_specifics

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={
                (DOMAIN, self._mac)
            },
            name=self.name
        )

    @property
    def name(self):
        """Return the name of the sensor."""
        return self.friendly_name

    @property
    def friendly_name(self):
        """Return the friendly name of the sensor"""
        return' '.join(word.capitalize() for word in self._sensor_name.split('_'))

    @property
    def native_value(self):
        """Return the state of the device."""
        return self._state

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return self._sensor_specifics.icon

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return self._sensor_specifics.device_class

    @property
    def native_unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._sensor_specifics.unit

    @property
    def unique_id(self):
        return self._name

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor."""
        return self._sensor_specifics.get_extra_attributes(self._state)

    async def async_update(self) -> None:
        """Fetch new state data for the sensor asynchronously.
        This is the only method that should fetch new data for Home Assistant.
        """
        now = datetime.now()
        if self.device.data_last_updated is None or now - self.device.data_last_updated > SCAN_INTERVAL:
            async with self.device.data_update_lock:
                if self.device.data_last_updated is None or now - self.device.data_last_updated > SCAN_INTERVAL:
                    ble_device = async_ble_device_from_address(self.hass, self._mac, connectable=True)
                    if ble_device is None:
                        _LOGGER.warning("Nespresso device %s not found via Bluetooth, skipping update", self._mac)
                        return
                    await self.device.connect(ble_device)
                    await self.device.get_sensor_data()
                    await self.device.disconnect()
        value = self.device.sensordata[self._mac][self._sensor_name]

        if type(value) is str:
            self._state = ' '.join(word.capitalize() for word in value.split('_'))
        elif self._sensor_specifics.unit_scale is None:
            self._state = value
        else:
            self._state = round(float(value * self._sensor_specifics.unit_scale), 2)

        end = datetime.now()
        _LOGGER.debug(f'async_update() took {end - now}')
        _LOGGER.debug("State {} {}".format(self._name, self._state))
