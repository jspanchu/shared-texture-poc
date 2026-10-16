"""A trame-rca render scheduler that renders into a texture shared with the Electron main process (EMP).

OS handles do not go through trame's websocket because that is wired to Electron render process (ERP), which
does not have access to OS API needed to import a texture.

Instead the scheduler tells EMP about the shared texture and each finished frame over texture_comm,
and EMP hands them to the renderer process. Size and input still come from the RemoteControlledArea through
the RcaViewAdapter.

Renders run on trame's event loop.
"""

import asyncio
import time
import traceback

from trame.app import asynchronous
from trame_rca.rca import VtkRemoteControlledArea

from trame_rca_extended.texture_passports import factory
from trame_rca_extended.texture_passports.texture_comm import LineConnection

FRAME_DONE_TIMEOUT_S = 1.0


def _report_failure(task):
    # Otherwise asyncio only reports it once the task is garbage collected.
    if not task.cancelled() and task.exception():
        print("[producer] render task failed:", flush=True)
        traceback.print_exception(task.exception())


class ElectronSharedTextureRenderScheduler:
    """Implements trame_rca's RcaRenderSchedulerProtocol."""

    def __init__(self, render_window, emp_port, *, target_fps=60.0, on_disconnect=None, before_render=None):
        self._rca = VtkRemoteControlledArea(render_window)
        self._render_window = render_window
        self._emp_port = emp_port
        self._target_fps = target_fps
        self._on_disconnect = on_disconnect
        # Called before every frame, e.g. to animate the scene. It may schedule_render() again.
        self._before_render = before_render
        self._push_callback = None

        self._conn = None
        self._passport = None
        self._passport_name_future = None
        self._seq = 0
        self._frame_done = None
        # Renders asked for before start() wait in here.
        self._request_event = asyncio.Event()
        self._tasks = []
        self._is_closing = False

        # Render into the FBOs only: Frame() then stops after resolving into the DisplayFramebuffer.
        render_window.SetUseOffScreenBuffers(True)
        render_window.SetShowWindow(False)
        # GL's first row is the bottom, an IOSurface's is the top.
        render_window.SetFramebufferFlipY(True)
        render_window.GetInteractor().Initialize()

    @property
    def rca(self):
        return self._rca

    @property
    def target_fps(self):
        return self._target_fps

    @target_fps.setter
    def target_fps(self, value):
        self._target_fps = value

    def set_push_callback(self, callback):
        # Frames go to EMP, never through the push callback.
        self._push_callback = callback

    def reset(self):
        """Nothing to re-initialize: a frame is complete on its own."""

    def schedule_render(self):
        self._request_event.set()

    def start(self, **_):
        """Connect to EMP and start rendering. Call once trame's event loop runs."""
        self._start_task(self._run())

    def _start_task(self, coro):
        task = asynchronous.create_task(coro)
        task.add_done_callback(_report_failure)
        self._tasks.append(task)

    async def close(self):
        if self._is_closing:
            return
        self._is_closing = True
        for task in self._tasks:
            task.cancel()
        if self._conn:
            self._conn.close()
        if self._passport:
            self._passport.release()
            self._passport = None

    async def _run(self):
        self._passport_name_future = asyncio.get_running_loop().create_future()
        self._conn = await LineConnection.open(self._emp_port)
        print(f"[producer] connected to EMP on port {self._emp_port}", flush=True)
        self._start_task(self._read())
        passport_name = await self._passport_name_future
        while not self._is_closing:
            await self._request_event.wait()
            self._request_event.clear()
            # Nothing to render into until the canvas reported its size: the window may start at 0x0.
            if 0 in self._render_window.GetSize():
                continue
            started = time.monotonic()

            await self._update_texture(passport_name)
            assert self._passport
            if self._before_render:
                self._before_render()
            self._render_window.Render()
            self._render_window.MakeCurrent()
            self._passport.flush_graphics_commands()

            # Backpressure: don't touch the texture until Electron is done reading it.
            # Input keeps setting the request event meanwhile, so it renders once afterwards.
            seq = self._seq
            self._seq += 1
            self._frame_done = asyncio.get_running_loop().create_future()
            await self._conn.send({"type": "frame", "seq": seq, "timestamp_us": int(time.time() * 1e6)})
            try:
                await asyncio.wait_for(self._frame_done, FRAME_DONE_TIMEOUT_S)
            except TimeoutError:
                print(f"[producer] frame {seq}: no frame_done within 1s, continuing", flush=True)
            self._frame_done = None

            await asyncio.sleep(max(0.0, 1.0 / self._target_fps - (time.monotonic() - started)))

    async def _update_texture(self, passport_name):
        """(Re)allocate the shared texture when the render window's size changed."""
        width, height = self._render_window.GetSize()
        if self._passport and (self._passport.width, self._passport.height) == (width, height):
            return
        if self._passport is None:
            # The first render creates the context and the framebuffers the passport patches.
            self._render_window.Render()
            self._render_window.MakeCurrent()
        passport_class = factory.texture_passport_class(passport_name)
        old, self._passport = self._passport, passport_class(self._render_window, width, height)
        if old:
            # Frames still in flight hold their own imports in Electron.
            old.release()
        await self._conn.send(self._passport.to_json())

    async def _read(self):
        try:
            while True:
                msg = await self._conn.recv()
                if (msg["type"] == "frame_done" and msg["seq"] == self._seq - 1
                        and self._frame_done and not self._frame_done.done()):
                    self._frame_done.set_result(None)
                if msg["type"] == "texture_passport_meta":
                    self._passport_name_future.set_result(msg["name"])

        except (EOFError, ConnectionError):
            print("[producer] EMP disconnected", flush=True)
            await self.close()
            if self._on_disconnect:
                self._on_disconnect()
