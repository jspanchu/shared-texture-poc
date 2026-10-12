"""Render a VTK scene straight into a child window of the Electron window (X11).

Usage: x11_vtk_frame_producer.py <port> <parent-xid> <x> <y> <width> <height>

No texture sharing: Chromium can't import dma-bufs on X11, so the
vtkXOpenGLRenderWindow is created as a child of the Electron window, over the
content area, and the X server composites it. Frames never pass through Chromium.

VTK owns the GL context and the window; this script only hands it the parent XID.
Unlike the plain OpenGL producer, the child window keeps its input, so the
vtkXRenderWindowInteractor gets mouse and keyboard events over the render area.

Chromium draws into its own child window covering the whole Electron window,
so the producer keeps VTK's window on top of that sibling, re-raising it
whenever Chromium restacks. Over the TCP connection to the Electron main process
(see producer_common.py) only {"type":"resize", ...} messages matter: they carry
the content area's new size and offset in the parent, in device pixels, and the
child window follows. The producer exits when either the connection or the
parent window goes away.
"""

import ctypes
import signal
import sys
import time

from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
from vtkmodules.vtkRenderingCore import vtkCompositePolyDataMapper, vtkVolume, vtkActor, vtkRenderer, vtkRenderWindow, vtkRenderWindowInteractor, vtkColorTransferFunction, vtkVolumeProperty
from vtkmodules.vtkRenderingOpenGL2 import vtkXOpenGLRenderWindow
from vtkmodules.vtkRenderingUI import vtkXRenderWindowInteractor
from vtkmodules.vtkRenderingVolumeOpenGL2 import vtkSmartVolumeMapper
from vtkmodules.vtkFiltersSources import vtkPartitionedDataSetCollectionSource
from vtkmodules.vtkImagingCore import vtkRTAnalyticSource
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

from producer_common import LineSocket

# --- Xlib ----------------------------------------------------------------------
# VTK renders and handles input on its own display connection. This one only
# watches the parent window, so VTK's interactor never sees (and drops) its events.
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
DESTROY_NOTIFY = 17
MAP_NOTIFY = 19
CONFIGURE_NOTIFY = 22

vp = ctypes.c_void_p
XID = ctypes.c_ulong


class XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("display", vp), ("resourceid", XID), ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte), ("minor_code", ctypes.c_ubyte),
    ]


# union XEvent. For the events handled here, [0] is the type and [5] the window it's about.
XEvent = ctypes.c_long * 24
XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, vp, ctypes.POINTER(XErrorEvent))

x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = vp
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XCloseDisplay.argtypes = [vp]
x11.XRaiseWindow.argtypes = [vp, XID]
x11.XSelectInput.argtypes = [vp, XID, ctypes.c_long]
x11.XPending.argtypes = [vp]
x11.XNextEvent.argtypes = [vp, ctypes.POINTER(XEvent)]
x11.XSync.argtypes = [vp, ctypes.c_int]
x11.XSetErrorHandler.restype = vp
x11.XSetErrorHandler.argtypes = [XErrorHandler]

# X errors would otherwise kill the process. The handler is process-wide, so it
# covers VTK's connection too. The expected one is the parent window disappearing
# mid-frame when Electron closes; the main loop stops on it.
x_errors = []


@XErrorHandler
def on_x_error(_dpy, event):
    e = event.contents
    x_errors.append(f"X error {e.error_code} (request {e.request_code}.{e.minor_code}) on 0x{e.resourceid:x}")
    return 0


def to_pointer(xid):
    """VTK's Python wrapping takes void* arguments as mangled pointer strings."""
    return f"_{xid:016x}_p_void"


def from_pointer(pointer):
    return int(pointer[1:].split("_", 1)[0], 16)


def create_vtk_pipeline():
    wavelet = vtkRTAnalyticSource()
    volume_mapper = vtkSmartVolumeMapper()
    volume_mapper.SetInputConnection(wavelet.output_port)
    color_tf = vtkColorTransferFunction()
    color_tf.AddRGBPoint(0., 0., 0., 1.)
    color_tf.AddRGBPoint(255. / 2., 0., 1., 1.)
    color_tf.AddRGBPoint(255., 1., 0., 1.)
    opacity_tf = vtkPiecewiseFunction()
    opacity_tf.AddPoint(0., 0.)
    opacity_tf.AddPoint(255., 0.1)
    volume_property = vtkVolumeProperty()
    volume_property.SetColor(color_tf)
    volume_property.SetScalarOpacity(opacity_tf)
    volume_property.ShadeOn()
    volume_property.SetInterpolationTypeToLinear()
    volume_actor = vtkVolume(mapper=volume_mapper, property=volume_property)

    shapes = vtkPartitionedDataSetCollectionSource()
    cpdm = vtkCompositePolyDataMapper()
    cpdm.SetInputConnection(shapes.output_port)
    shapes_actor = vtkActor(mapper=cpdm)

    renderer = vtkRenderer()
    renderer.AddVolume(volume_actor)
    renderer.AddActor(shapes_actor)

    render_window = vtkRenderWindow(
            interactor=vtkRenderWindowInteractor(interactor_style=vtkInteractorStyleTrackballCamera()),
    )
    render_window.AddRenderer(renderer)
    return render_window


def handle_x_events(dpy, window):
    """Drain the parent's events; True once VTK's window was destroyed or an X error occurred."""
    gone = bool(x_errors)
    event = XEvent()
    while x11.XPending(dpy):
        x11.XNextEvent(dpy, ctypes.byref(event))
        kind, about = ctypes.cast(event, ctypes.POINTER(ctypes.c_int))[0], event[5]
        if kind == DESTROY_NOTIFY and about == window:
            gone = True
        elif kind in (MAP_NOTIFY, CONFIGURE_NOTIFY) and about != window and not gone:
            x11.XRaiseWindow(dpy, window)
    return gone


def handle_messages(conn, render_window, interactor):
    """Apply the queued messages from Electron. Raises EOFError once it disconnected."""
    while (msg := conn.recv(0)) is not None:
        if msg["type"] != "resize":
            continue
        width, height = msg["width"], msg["height"]
        print(f"[producer] resize to {msg['x']},{msg['y']} {width}x{height}", flush=True)
        # The menu bar above the content area can change height with the scale factor.
        render_window.SetPosition(msg["x"], msg["y"])
        # Resizes the child window, and keeps the interactor's size in sync with it.
        interactor.UpdateSize(width, height)


def main():
    if len(sys.argv) != 7:
        sys.exit("usage: x11_vtk_frame_producer.py <port> <parent-xid> <x> <y> <width> <height>")
    port, parent, x, y, width, height = (int(a, 0) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    dpy = x11.XOpenDisplay(None)
    if not dpy:
        sys.exit("[producer] cannot open X display (is DISPLAY set?)")
    x11.XSetErrorHandler(on_x_error)

    render_window = create_vtk_pipeline()
    if not isinstance(render_window, vtkXOpenGLRenderWindow):
        sys.exit(f"[producer] expected a vtkXOpenGLRenderWindow, got {render_window.GetClassName()}")
    interactor = render_window.GetInteractor()
    if not isinstance(interactor, vtkXRenderWindowInteractor):
        sys.exit(f"[producer] expected a vtkXRenderWindowInteractor, got {interactor.GetClassName()}")

    # VTK creates its window as a child of the parent, with its own colormap, so
    # Chromium's visual doesn't need to match.
    render_window.SetParentId(to_pointer(parent))
    render_window.SetPosition(x, y)
    render_window.SetSize(width, height)
    # Creates the window and selects the input events on it.
    interactor.Initialize()
    render_window.Render()
    if x_errors:
        sys.exit(f"[producer] creating the child of 0x{parent:x} failed: {x_errors[0]}")
    window = from_pointer(render_window.GetGenericWindowId())
    vsync = render_window.SetSwapControl(1)

    # Hear about Chromium's window being restacked, see handle_x_events.
    x11.XSelectInput(dpy, parent, SUBSTRUCTURE_NOTIFY_MASK)
    x11.XRaiseWindow(dpy, window)
    x11.XSync(dpy, 0)
    print(f"[producer] child window 0x{window:x} in 0x{parent:x} at {x},{y} {width}x{height}, vsync={vsync}", flush=True)
    caps = dict(line.split(":", 1) for line in render_window.ReportCapabilities().splitlines() if ":" in line)
    print(f"[producer] GL {caps['OpenGL version string'].strip()} / {caps['OpenGL renderer string'].strip()}", flush=True)

    conn = LineSocket(port)
    try:
        frames, window_start = 0, time.monotonic()
        frame_interval = 1 / 60
        while True:
            start = time.monotonic()
            if handle_x_events(dpy, window):
                print("[producer] window destroyed, exiting", flush=True)
                break
            handle_messages(conn, render_window, interactor)
            interactor.ProcessEvents()
            render_window.Render()
            frames += 1
            if start - window_start >= 1.0:
                print(f"[producer] {frames / (start - window_start):.1f} fps", flush=True)
                frames, window_start = 0, start
            if not vsync:
                remaining = frame_interval - (time.monotonic() - start)
                if remaining > 0:
                    time.sleep(remaining)
    except (EOFError, ConnectionError):
        print("[producer] connection closed, exiting", flush=True)
    finally:
        conn.sock.close()
        # Destroys VTK's window if it still exists; errors from a gone parent land in on_x_error.
        render_window.Finalize()
        x11.XSync(dpy, 0)
        x11.XCloseDisplay(dpy)


if __name__ == "__main__":
    main()
