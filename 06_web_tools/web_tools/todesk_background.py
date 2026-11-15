"""Production guardrails for the currently verified ToDesk background scope.

Only attaching an existing viewer and closing that exact viewer are allowed.
Experimental native input/activation and saved-device connection paths remain
inaccessible through this class until their background effect is verified.
"""
from __future__ import annotations

from .todesk_native import NativeToDesk, NativeToDeskError


class StrictBackgroundToDesk(NativeToDesk):
    @staticmethod
    def capabilities() -> dict:
        return {"background_only": True, "input_verified": False, "auto_connect_verified": False}

    def connect(self, device_name: str) -> dict:
        if not isinstance(device_name, str) or device_name.casefold() not in self._devices:
            raise NativeToDeskError("device_not_allowed", "This remote device is not configured")
        matches = [item for item in self.inventory()
                   if item["viewer"] and item["title"].casefold() == device_name.casefold()]
        if not matches:
            raise NativeToDeskError("background_connection_unverified", "A previously connected, allowlisted ToDesk viewer is required for background attachment")
        if len(matches) != 1:
            raise NativeToDeskError("ambiguous_viewer", "More than one existing viewer matches this device")
        current = self.verify(matches[0])
        if self._api.is_iconic(current["hwnd"]):
            raise NativeToDeskError("background_viewer_minimized", "The existing viewer is minimized; strict background mode does not restore windows")
        return {**current, "attached_existing": True, "background_only": True, "host_input_used": False}

    def perform(self, record: dict, action: str, args: dict | None = None) -> dict:
        if action in {"click", "type", "key", "scroll", "drag"}:
            raise NativeToDeskError("background_input_unverified", "ToDesk background input has not been verified; no input was dispatched")
        if action != "close":
            raise NativeToDeskError("unsupported_action", "Unsupported strict background ToDesk action")
        # Parent close verifies the exact owned viewer and sends only WM_CLOSE;
        # its activation path is explicitly excluded for this action.
        return super().perform(record, action, args)
