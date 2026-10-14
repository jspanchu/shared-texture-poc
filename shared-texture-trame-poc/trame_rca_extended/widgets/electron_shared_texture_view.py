from pathlib import Path

from trame_client.widgets.core import AbstractElement

module = {
    "serve": {"__shared_texture": str(Path(__file__).with_name("serve").resolve())},
    "scripts": ["__shared_texture/electron_shared_texture_view.js"],
    "vue_use": ["trame_shared_texture"],
}


class ElectronSharedTextureView(AbstractElement):
    """The canvas Electron draws the shared texture into.

    Put it in a RemoteControlledArea's slot: the area reports the size to render at and
    forwards input to the RcaViewAdapter, the canvas only shows frames.
    """

    def __init__(self, **kwargs):
        super().__init__("electron-shared-texture-view", **kwargs)
        # Once a second, with { fps, frames, info } as $event.
        self._event_names += ["stats"]
        if self.server:
            self.server.enable_module(module)
