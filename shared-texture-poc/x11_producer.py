"""Render with GLX straight into a child window of the Electron window (X11).

Usage: x11_producer.py <port> <parent-xid> <x> <y> <width> <height>

No texture sharing: Chromium can't import dma-bufs on X11 (see README), so the
producer creates an X window inside the Electron window, over the content area,
and the X server composites it. Frames never pass through Chromium.

Chromium draws into its own child window covering the whole Electron window,
so the producer keeps its window on top of that sibling, re-raising it whenever
Chromium restacks. The child window has an empty input shape, so mouse events fall through to the
page underneath. The TCP connection to the Electron main process only ties the
two lifetimes together; the producer exits when either the connection or the
parent window goes away.
"""

import ctypes
import logging
import math
import os
import signal
import socket
import sys
import time

os.environ.setdefault("PYOPENGL_PLATFORM", "glx")
# PyOpenGL's plugin loader warns that numpy is missing; it isn't needed here.
logging.getLogger("OpenGL.plugins").setLevel(logging.ERROR)

from OpenGL import GL  # noqa: E402

# --- GLX / Xlib constants -----------------------------------------------------
GLX_DOUBLEBUFFER = 5
GLX_RED_SIZE = 8
GLX_GREEN_SIZE = 9
GLX_BLUE_SIZE = 10
GLX_DRAWABLE_TYPE = 0x8010
GLX_RENDER_TYPE = 0x8011
GLX_X_RENDERABLE = 0x8012
GLX_RGBA_TYPE = 0x8014
GLX_WINDOW_BIT = 0x0001
GLX_RGBA_BIT = 0x0001

CW_BACK_PIXMAP = 1 << 0
CW_BORDER_PIXEL = 1 << 3
CW_EVENT_MASK = 1 << 11
CW_COLORMAP = 1 << 13
STRUCTURE_NOTIFY_MASK = 1 << 17
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
INPUT_OUTPUT = 1
ALLOC_NONE = 0
DESTROY_NOTIFY = 17
MAP_NOTIFY = 19
CONFIGURE_NOTIFY = 22
SHAPE_INPUT = 2

vp = ctypes.c_void_p
XID = ctypes.c_ulong


class XVisualInfo(ctypes.Structure):
    _fields_ = [
        ("visual", vp), ("visualid", ctypes.c_ulong), ("screen", ctypes.c_int), ("depth", ctypes.c_int),
        ("c_class", ctypes.c_int), ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong), ("colormap_size", ctypes.c_int), ("bits_per_rgb", ctypes.c_int),
    ]


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", XID), ("background_pixel", ctypes.c_ulong), ("border_pixmap", XID),
        ("border_pixel", ctypes.c_ulong), ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int), ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("colormap", XID), ("cursor", XID),
    ]


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
x11.XDefaultScreen.argtypes = [vp]
x11.XRootWindow.restype = XID
x11.XRootWindow.argtypes = [vp, ctypes.c_int]
x11.XCreateColormap.restype = XID
x11.XCreateColormap.argtypes = [vp, XID, vp, ctypes.c_int]
x11.XFreeColormap.argtypes = [vp, XID]
x11.XCreateWindow.restype = XID
x11.XCreateWindow.argtypes = [
    vp, XID, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
    ctypes.c_uint, vp, ctypes.c_ulong, ctypes.POINTER(XSetWindowAttributes)]
x11.XMapWindow.argtypes = [vp, XID]
x11.XRaiseWindow.argtypes = [vp, XID]
x11.XSelectInput.argtypes = [vp, XID, ctypes.c_long]
x11.XDestroyWindow.argtypes = [vp, XID]
x11.XPending.argtypes = [vp]
x11.XNextEvent.argtypes = [vp, ctypes.POINTER(XEvent)]
x11.XSync.argtypes = [vp, ctypes.c_int]
x11.XFree.argtypes = [vp]
x11.XSetErrorHandler.restype = vp
x11.XSetErrorHandler.argtypes = [XErrorHandler]

xfixes = ctypes.CDLL("libXfixes.so.3")
xfixes.XFixesCreateRegion.restype = XID
xfixes.XFixesCreateRegion.argtypes = [vp, vp, ctypes.c_int]
xfixes.XFixesSetWindowShapeRegion.argtypes = [vp, XID, ctypes.c_int, ctypes.c_int, ctypes.c_int, XID]
xfixes.XFixesDestroyRegion.argtypes = [vp, XID]

glx = ctypes.CDLL("libGL.so.1")
glx.glXChooseFBConfig.restype = ctypes.POINTER(vp)
glx.glXChooseFBConfig.argtypes = [vp, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
glx.glXGetVisualFromFBConfig.restype = ctypes.POINTER(XVisualInfo)
glx.glXGetVisualFromFBConfig.argtypes = [vp, vp]
glx.glXCreateNewContext.restype = vp
glx.glXCreateNewContext.argtypes = [vp, vp, ctypes.c_int, vp, ctypes.c_int]
glx.glXDestroyContext.argtypes = [vp, vp]
glx.glXMakeCurrent.argtypes = [vp, XID, vp]
glx.glXSwapBuffers.argtypes = [vp, XID]
glx.glXGetProcAddressARB.restype = vp
glx.glXGetProcAddressARB.argtypes = [ctypes.c_char_p]

# X errors would otherwise kill the process. The expected one is the parent
# window disappearing mid-frame when Electron closes; the main loop stops on it.
x_errors = []


@XErrorHandler
def on_x_error(_dpy, event):
    e = event.contents
    x_errors.append(f"X error {e.error_code} (request {e.request_code}.{e.minor_code}) on 0x{e.resourceid:x}")
    return 0


def create_window(dpy, parent, x, y, width, height):
    """Create a GLX-capable child window. Returns (window, colormap, fbconfig)."""
    screen = x11.XDefaultScreen(dpy)
    n = ctypes.c_int()
    attribs = (ctypes.c_int * 17)(
        GLX_X_RENDERABLE, 1,
        GLX_DRAWABLE_TYPE, GLX_WINDOW_BIT,
        GLX_RENDER_TYPE, GLX_RGBA_BIT,
        GLX_RED_SIZE, 8, GLX_GREEN_SIZE, 8, GLX_BLUE_SIZE, 8,
        GLX_DOUBLEBUFFER, 1,
        0, 0, 0)  # None-terminated
    configs = glx.glXChooseFBConfig(dpy, screen, attribs, ctypes.byref(n))
    if not configs or n.value == 0:
        sys.exit("[producer] no matching GLX framebuffer config")
    fbconfig = configs[0]
    x11.XFree(configs)
    info = glx.glXGetVisualFromFBConfig(dpy, fbconfig)
    if not info:
        sys.exit("[producer] GLX config has no X visual")
    visual, depth = info.contents.visual, info.contents.depth

    # The parent's visual (Chromium's) may differ from ours, so the window needs
    # its own colormap and an explicit border pixel or XCreateWindow fails with BadMatch.
    colormap = x11.XCreateColormap(dpy, x11.XRootWindow(dpy, screen), visual, ALLOC_NONE)
    attrs = XSetWindowAttributes(background_pixmap=0, border_pixel=0, colormap=colormap,
                                 event_mask=STRUCTURE_NOTIFY_MASK)
    window = x11.XCreateWindow(dpy, parent, x, y, width, height, 0, depth, INPUT_OUTPUT, visual,
                               CW_BACK_PIXMAP | CW_BORDER_PIXEL | CW_COLORMAP | CW_EVENT_MASK, ctypes.byref(attrs))
    x11.XFree(info)

    # Empty input region: clicks and wheel events go to the page underneath.
    region = xfixes.XFixesCreateRegion(dpy, None, 0)
    xfixes.XFixesSetWindowShapeRegion(dpy, window, SHAPE_INPUT, 0, 0, region)
    xfixes.XFixesDestroyRegion(dpy, region)

    # Hear about Chromium's window being restacked, see handle_x_events.
    x11.XSelectInput(dpy, parent, SUBSTRUCTURE_NOTIFY_MASK)
    x11.XMapWindow(dpy, window)
    x11.XRaiseWindow(dpy, window)
    x11.XSync(dpy, 0)
    if x_errors:
        sys.exit(f"[producer] creating the child of 0x{parent:x} failed: {x_errors[0]}")
    return window, colormap, fbconfig


def set_swap_interval(dpy, window, interval):
    addr = glx.glXGetProcAddressARB(b"glXSwapIntervalEXT")
    if not addr:
        return False
    ctypes.CFUNCTYPE(None, vp, XID, ctypes.c_int)(addr)(dpy, window, interval)
    return True


def handle_x_events(dpy, window):
    """Drain X events; True once our window was destroyed or an X error occurred."""
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


def connection_closed(sock):
    try:
        return sock.recv(4096) == b""
    except BlockingIOError:
        return False


def main():
    if len(sys.argv) != 7:
        sys.exit("usage: x11_producer.py <port> <parent-xid> <x> <y> <width> <height>")
    port, parent, x, y, width, height = (int(a, 0) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    dpy = x11.XOpenDisplay(None)
    if not dpy:
        sys.exit("[producer] cannot open X display (is DISPLAY set?)")
    x11.XSetErrorHandler(on_x_error)
    window, colormap, fbconfig = create_window(dpy, parent, x, y, width, height)
    ctx = glx.glXCreateNewContext(dpy, fbconfig, GLX_RGBA_TYPE, None, 1)
    if not ctx or not glx.glXMakeCurrent(dpy, window, ctx):
        sys.exit("[producer] GLX context creation failed")
    vsync = set_swap_interval(dpy, window, 1)
    print(f"[producer] child window 0x{window:x} in 0x{parent:x} at {x},{y} {width}x{height}, vsync={vsync}", flush=True)
    print(f"[producer] GL {GL.glGetString(GL.GL_VERSION).decode()} / {GL.glGetString(GL.GL_RENDERER).decode()}", flush=True)
    GL.glViewport(0, 0, width, height)

    sock = socket.create_connection(("127.0.0.1", port))
    sock.setblocking(False)
    window_alive = True
    try:
        frames, window_start = 0, time.monotonic()
        frame_interval = 1 / 60
        while True:
            start = time.monotonic()
            if handle_x_events(dpy, window):
                window_alive = False
                print("[producer] window destroyed, exiting", flush=True)
                break
            if connection_closed(sock):
                print("[producer] connection closed, exiting", flush=True)
                break
            GL.glClearColor(0.5 + 0.5 * math.sin(time.time()), 0.0, 0.0, 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            glx.glXSwapBuffers(dpy, window)
            frames += 1
            if start - window_start >= 1.0:
                print(f"[producer] {frames / (start - window_start):.1f} fps", flush=True)
                frames, window_start = 0, start
            if not vsync:
                remaining = frame_interval - (time.monotonic() - start)
                if remaining > 0:
                    time.sleep(remaining)
    except ConnectionError:
        print("[producer] connection closed, exiting", flush=True)
    finally:
        sock.close()
        glx.glXMakeCurrent(dpy, 0, None)
        glx.glXDestroyContext(dpy, ctx)
        if window_alive:
            x11.XDestroyWindow(dpy, window)
        x11.XFreeColormap(dpy, colormap)
        x11.XSync(dpy, 0)
        x11.XCloseDisplay(dpy)


if __name__ == "__main__":
    main()
