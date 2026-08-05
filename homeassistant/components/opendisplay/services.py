"""Service registration for the OpenDisplay integration."""

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from datetime import timedelta
from enum import IntEnum
import io
from typing import TYPE_CHECKING, Any

import aiohttp
from bleak.backends.device import BLEDevice
from opendisplay import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BuzzerActivateConfig,
    DitherMode,
    FitMode,
    OpenDisplayDevice,
    OpenDisplayError,
    RefreshMode,
    Rotation,
)
from PIL import Image as PILImage, ImageOps
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothReachabilityIntent,
    async_address_reachability_diagnostics,
    async_ble_device_from_address,
)
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import async_resolve_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.network import get_url
from homeassistant.helpers.selector import MediaSelector, MediaSelectorConfig

if TYPE_CHECKING:
    from . import OpenDisplayConfigEntry

from .const import CONF_ENCRYPTION_KEY, DOMAIN

ATTR_IMAGE = "image"
ATTR_ROTATION = "rotation"
ATTR_DITHER_MODE = "dither_mode"
ATTR_REFRESH_MODE = "refresh_mode"
ATTR_FIT_MODE = "fit_mode"
ATTR_TONE_COMPRESSION = "tone_compression"
ATTR_INSTANCE = "instance"
ATTR_FREQUENCY_HZ = "frequency_hz"
ATTR_DURATION_MS = "duration_ms"
ATTR_REPEATS = "repeats"


def _str_to_int_enum(enum_class: type[IntEnum]) -> Callable[[str], Any]:
    """Convert a lowercase enum name string to an enum member."""
    members = {m.name.lower(): m for m in enum_class}

    def validate(value: str) -> IntEnum:
        if (result := members.get(value)) is None:
            raise vol.Invalid(f"Invalid value: {value}")
        return result

    return validate


SCHEMA_UPLOAD_IMAGE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_IMAGE): MediaSelector(
            MediaSelectorConfig(accept=["image/*"])
        ),
        vol.Optional(ATTR_ROTATION, default=Rotation.ROTATE_0): vol.All(
            vol.Coerce(int), vol.Coerce(Rotation)
        ),
        vol.Optional(ATTR_DITHER_MODE, default="burkes"): _str_to_int_enum(DitherMode),
        vol.Optional(ATTR_REFRESH_MODE, default="full"): _str_to_int_enum(RefreshMode),
        vol.Optional(ATTR_FIT_MODE, default="contain"): _str_to_int_enum(FitMode),
        vol.Optional(ATTR_TONE_COMPRESSION): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=100.0)
        ),
    }
)


SCHEMA_ACTIVATE_BUZZER = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_INSTANCE, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=3)
        ),
        vol.Optional(ATTR_FREQUENCY_HZ, default=1000): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=12000)
        ),
        vol.Optional(ATTR_DURATION_MS, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1275)
        ),
        vol.Optional(ATTR_REPEATS, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=255)
        ),
    }
)


def _get_entry_for_device(call: ServiceCall) -> OpenDisplayConfigEntry:
    """Return the config entry for the device targeted by a service call."""
    device_id: str = call.data[ATTR_DEVICE_ID]
    device_registry = dr.async_get(call.hass)

    if (device := device_registry.async_get(device_id)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )

    mac_address = next(
        (conn[1] for conn in device.connections if conn[0] == CONNECTION_BLUETOOTH),
        None,
    )
    if mac_address is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device_id",
            translation_placeholders={"device_id": device_id},
        )

    entry = call.hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, mac_address
    )
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="config_entry_not_found",
            translation_placeholders={"address": mac_address},
        )

    return entry


def _load_image(path: str) -> PILImage.Image:
    """Load an image from disk and apply EXIF orientation."""
    image = PILImage.open(path)
    image.load()
    return ImageOps.exif_transpose(image)


def _load_image_from_bytes(data: bytes) -> PILImage.Image:
    """Load an image from bytes and apply EXIF orientation."""
    image = PILImage.open(io.BytesIO(data))
    image.load()
    return ImageOps.exif_transpose(image)


async def _async_download_image(hass: HomeAssistant, url: str) -> PILImage.Image:
    """Download an image from a URL and return a PIL Image."""
    if not url.startswith(("http://", "https://")):
        url = get_url(hass) + async_sign_path(
            hass, url, timedelta(minutes=5), use_content_user=True
        )
    session = async_get_clientsession(hass)
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    except aiohttp.ClientError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="media_download_error",
            translation_placeholders={"error": str(err)},
        ) from err

    return await hass.async_add_executor_job(_load_image_from_bytes, data)


def _encryption_key(hass: HomeAssistant, entry: OpenDisplayConfigEntry) -> bytes | None:
    """Return the entry's encryption key, starting reauth if it is unusable."""
    raw_key = entry.data.get(CONF_ENCRYPTION_KEY)
    if raw_key is None:
        return None

    key: bytes | None = None
    if len(raw_key) == 32:
        with contextlib.suppress(ValueError):
            key = bytes.fromhex(raw_key)
    if key is None:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        )
    return key


def _ble_device_or_raise(hass: HomeAssistant, address: str) -> BLEDevice:
    """Return the BLE device for an address, or raise with a reachability reason."""
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={
                "address": address,
                "reason": async_address_reachability_diagnostics(
                    hass,
                    address.upper(),
                    BluetoothReachabilityIntent.CONNECTION,
                ),
            },
        )
    return ble_device


async def _async_connect_and_run(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    ble_device: BLEDevice,
    action: Callable[[OpenDisplayDevice], Coroutine[Any, Any, None]],
    error_translation_key: str,
) -> None:
    """Connect to the device and run one action against it."""
    address = entry.unique_id
    assert address is not None

    encryption_key = _encryption_key(hass, entry)

    try:
        async with (
            entry.runtime_data.ble_lock,
            OpenDisplayDevice(
                mac_address=address,
                ble_device=ble_device,
                config=entry.runtime_data.device_config,
                encryption_key=encryption_key,
            ) as device,
        ):
            await action(device)
    except (AuthenticationFailedError, AuthenticationRequiredError) as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="authentication_error"
        ) from err
    except OpenDisplayError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=error_translation_key
        ) from err


async def _async_upload_image(call: ServiceCall) -> None:
    """Handle the upload_image service call."""
    entry = _get_entry_for_device(call)
    address = entry.unique_id
    assert address is not None
    # Resolved up front so an unreachable device fails before the image is fetched.
    ble_device = _ble_device_or_raise(call.hass, address)

    image_data: dict[str, Any] = call.data[ATTR_IMAGE]
    rotation: Rotation = call.data[ATTR_ROTATION]
    dither_mode: DitherMode = call.data[ATTR_DITHER_MODE]
    refresh_mode: RefreshMode = call.data[ATTR_REFRESH_MODE]
    fit_mode: FitMode = call.data[ATTR_FIT_MODE]
    tone_compression_pct: float | None = call.data.get(ATTR_TONE_COMPRESSION)
    tone_compression: float | str = (
        tone_compression_pct / 100.0 if tone_compression_pct is not None else "auto"
    )

    current = asyncio.current_task()
    if (prev := entry.runtime_data.upload_task) is not None and not prev.done():
        prev.cancel()
        await asyncio.wait({prev})
    entry.runtime_data.upload_task = current

    try:
        media = await async_resolve_media(
            call.hass, image_data["media_content_id"], None
        )

        if media.path is not None:
            pil_image = await call.hass.async_add_executor_job(
                _load_image, str(media.path)
            )
        else:
            pil_image = await _async_download_image(call.hass, media.url)

        async def _upload(device: OpenDisplayDevice) -> None:
            await device.upload_image(
                pil_image,
                refresh_mode=refresh_mode,
                dither_mode=dither_mode,
                tone=tone_compression,
                fit=fit_mode,
                rotate=rotation,
            )

        await _async_connect_and_run(
            call.hass, entry, ble_device, _upload, "upload_error"
        )
    except asyncio.CancelledError:
        return
    finally:
        if entry.runtime_data.upload_task is current:
            entry.runtime_data.upload_task = None


async def _async_activate_buzzer(call: ServiceCall) -> None:
    """Handle the activate_buzzer service call."""
    entry = _get_entry_for_device(call)
    if not entry.runtime_data.device_config.buzzers:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_buzzers",
            translation_placeholders={"device_id": call.data[ATTR_DEVICE_ID]},
        )

    address = entry.unique_id
    assert address is not None
    ble_device = _ble_device_or_raise(call.hass, address)

    instance: int = call.data[ATTR_INSTANCE]
    buzzer_config = BuzzerActivateConfig.single_tone(
        frequency_hz=call.data[ATTR_FREQUENCY_HZ],
        duration_ms=call.data[ATTR_DURATION_MS],
        repeats=call.data[ATTR_REPEATS],
    )

    async def _buzz(device: OpenDisplayDevice) -> None:
        await device.activate_buzzer(instance, buzzer_config)

    await _async_connect_and_run(call.hass, entry, ble_device, _buzz, "buzzer_error")


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register OpenDisplay services."""
    hass.services.async_register(
        DOMAIN,
        "upload_image",
        _async_upload_image,
        schema=SCHEMA_UPLOAD_IMAGE,
    )
    hass.services.async_register(
        DOMAIN,
        "activate_buzzer",
        _async_activate_buzzer,
        schema=SCHEMA_ACTIVATE_BUZZER,
    )
