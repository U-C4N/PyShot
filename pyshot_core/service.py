from . import __version__
from . import capture
from .errors import PyShotError
from .recording import RecordingController, helper_query


class Service:
    def __init__(self):
        self.recordings = RecordingController()

    def call(self, owner, method, params):
        if method == "get_capabilities":
            try:
                native = helper_query("capabilities")
            except PyShotError as exc:
                native = {"available": False, "error": exc.as_dict()}
            audio = {"default_mode": "none", "microphone": False, "system": False, "both": False}
            if native.get("available"):
                try:
                    devices = helper_query("audio-devices")
                    audio["microphone"] = bool(devices.get("microphones"))
                    audio["system"] = bool(devices.get("system_audio"))
                    audio["both"] = audio["microphone"] and audio["system"]
                except PyShotError as exc:
                    audio["error"] = exc.as_dict()
            return {"version": __version__, "screenshots": True, "recording": native,
                    "audio": audio,
                    "targets": ["monitor", "region", "window"], "fps": 30,
                    "max_recording_frames": 8, "transport": "local-user-named-pipe"}
        if method == "list_displays":
            return {"displays": capture.list_displays()}
        if method == "list_windows":
            return {"windows": capture.list_windows()}
        if method == "list_audio_devices":
            return helper_query("audio-devices")
        if method == "capture_screenshot":
            return capture.capture_screenshot(**params)
        if method == "recording_start":
            return self.recordings.start(owner=owner, **params)
        if method == "recording_status":
            return self.recordings.status(**params)
        if method == "recording_stop":
            return self.recordings.stop(**params)
        if method == "recording_frames":
            return self.recordings.frames(**params)
        raise PyShotError("unknown_method", "Unknown method")

    def disconnect(self, owner):
        self.recordings.disconnect(owner)

    def close(self):
        self.recordings.close()
