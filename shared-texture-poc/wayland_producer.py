"""Render into a GL texture on a headless EGL context and export it as DMA-BUF.

Usage: wayland_producer.py <port> <width> <height>

Talks newline-delimited JSON to the Electron main process on 127.0.0.1:<port>:
  -> {"type":"texture", ...}   once, after the texture is exported
  -> {"type":"frame", ...}     after every render (glFinish'd)
  <- {"type":"frame_done", ...} when Electron released the texture
"""

import ctypes
import glob
import json
import logging
import math
import os
import signal
import socket
import sys
import time

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
# PyOpenGL's plugin loader warns that numpy is missing; it isn't needed here.
logging.getLogger("OpenGL.plugins").setLevel(logging.ERROR)

from OpenGL import GL  # noqa: E402

# --- EGL constants -----------------------------------------------------------
EGL_NONE = 0x3038
EGL_SURFACE_TYPE = 0x3033
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_BIT = 0x0008
EGL_RED_SIZE = 0x3024
EGL_GREEN_SIZE = 0x3023
EGL_BLUE_SIZE = 0x3022
EGL_ALPHA_SIZE = 0x3021
EGL_OPENGL_API = 0x30A2
EGL_EXTENSIONS = 0x3055
EGL_CONTEXT_MAJOR_VERSION = 0x3098
EGL_CONTEXT_MINOR_VERSION = 0x30FB
EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT = 0x0001
EGL_PLATFORM_DEVICE_EXT = 0x313F
EGL_DRM_DEVICE_FILE_EXT = 0x3233
EGL_DRM_RENDER_NODE_FILE_EXT = 0x3377
EGL_GL_TEXTURE_2D_KHR = 0x30B1
EGL_GL_TEXTURE_LEVEL_KHR = 0x30BC

EGLint = ctypes.c_int32
EGLBoolean = ctypes.c_uint32
EGLenum = ctypes.c_uint32
EGLuint64KHR = ctypes.c_uint64
vp = ctypes.c_void_p

egl = ctypes.CDLL("libEGL.so.1")
egl.eglGetProcAddress.restype = vp
egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
egl.eglGetError.restype = EGLint
egl.eglInitialize.argtypes = [vp, ctypes.POINTER(EGLint), ctypes.POINTER(EGLint)]
egl.eglInitialize.restype = EGLBoolean
egl.eglTerminate.argtypes = [vp]
egl.eglQueryString.argtypes = [vp, EGLint]
egl.eglQueryString.restype = ctypes.c_char_p
egl.eglBindAPI.argtypes = [EGLenum]
egl.eglBindAPI.restype = EGLBoolean
egl.eglChooseConfig.argtypes = [vp, ctypes.POINTER(EGLint), ctypes.POINTER(vp), EGLint, ctypes.POINTER(EGLint)]
egl.eglChooseConfig.restype = EGLBoolean
egl.eglCreateContext.argtypes = [vp, vp, vp, ctypes.POINTER(EGLint)]
egl.eglCreateContext.restype = vp
egl.eglDestroyContext.argtypes = [vp, vp]
egl.eglMakeCurrent.argtypes = [vp, vp, vp, vp]
egl.eglMakeCurrent.restype = EGLBoolean


def egl_ext(name, restype, *argtypes):
    addr = egl.eglGetProcAddress(name.encode())
    if not addr:
        sys.exit(f"[producer] missing EGL function {name}")
    return ctypes.CFUNCTYPE(restype, *argtypes)(addr)


eglQueryDevicesEXT = egl_ext("eglQueryDevicesEXT", EGLBoolean, EGLint, ctypes.POINTER(vp), ctypes.POINTER(EGLint))
eglQueryDeviceStringEXT = egl_ext("eglQueryDeviceStringEXT", ctypes.c_char_p, vp, EGLint)
eglGetPlatformDisplayEXT = egl_ext("eglGetPlatformDisplayEXT", vp, EGLenum, vp, ctypes.POINTER(EGLint))
eglCreateImageKHR = egl_ext("eglCreateImageKHR", vp, vp, vp, EGLenum, vp, ctypes.POINTER(EGLint))
eglDestroyImageKHR = egl_ext("eglDestroyImageKHR", EGLBoolean, vp, vp)
eglExportDMABUFImageQueryMESA = egl_ext(
    "eglExportDMABUFImageQueryMESA", EGLBoolean,
    vp, vp, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(EGLuint64KHR))
eglExportDMABUFImageMESA = egl_ext(
    "eglExportDMABUFImageMESA", EGLBoolean,
    vp, vp, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(EGLint), ctypes.POINTER(EGLint))


def attribs(*values):
    return (EGLint * (len(values) + 1))(*values, EGL_NONE)


def check(ok, what):
    if not ok:
        sys.exit(f"[producer] {what} failed: EGL error 0x{egl.eglGetError():04x}")
    return ok


def fourcc_str(code):
    return "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))


# DRM fourcc -> electron pixelFormat
PIXEL_FORMATS = {"AB24": "rgba", "AR24": "bgra"}


def default_render_node():
    """Return (render node, reason). Prefers the GPU driving the display (boot_vga=1).

    Chromium can only import the dma-buf if it was allocated on the same GPU
    its compositor uses, which on a multi-GPU box isn't necessarily renderD128.
    """
    for boot_vga in sorted(glob.glob("/sys/class/drm/card*/device/boot_vga")):
        try:
            with open(boot_vga) as f:
                if f.read().strip() != "1":
                    continue
        except OSError:
            continue
        nodes = sorted(glob.glob(os.path.join(os.path.dirname(boot_vga), "drm", "renderD*")))
        if nodes:
            return f"/dev/dri/{os.path.basename(nodes[0])}", "boot_vga"
    return "/dev/dri/renderD128", "fallback"


def open_display(render_node):
    devices = (vp * 16)()
    count = EGLint()
    check(eglQueryDevicesEXT(16, devices, ctypes.byref(count)), "eglQueryDevicesEXT")
    for i in range(count.value):
        node = eglQueryDeviceStringEXT(devices[i], EGL_DRM_RENDER_NODE_FILE_EXT)
        node = node.decode() if node else None
        print(f"[producer] EGL device {i}: render node {node}", flush=True)
        if node == render_node:
            dpy = check(eglGetPlatformDisplayEXT(EGL_PLATFORM_DEVICE_EXT, devices[i], None), "eglGetPlatformDisplayEXT")
            major, minor = EGLint(), EGLint()
            check(egl.eglInitialize(dpy, ctypes.byref(major), ctypes.byref(minor)), "eglInitialize")
            print(f"[producer] EGL {major.value}.{minor.value} on {render_node}", flush=True)
            return dpy
    sys.exit(f"[producer] no EGL device with render node {render_node}")


def create_context(dpy):
    exts = egl.eglQueryString(dpy, EGL_EXTENSIONS).decode().split()
    for ext in ("EGL_KHR_surfaceless_context", "EGL_MESA_image_dma_buf_export", "EGL_KHR_gl_texture_2D_image"):
        if ext not in exts:
            sys.exit(f"[producer] display lacks {ext}")
    check(egl.eglBindAPI(EGL_OPENGL_API), "eglBindAPI")
    config = vp()
    n = EGLint()
    check(egl.eglChooseConfig(dpy, attribs(
        EGL_SURFACE_TYPE, 0,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
    ), ctypes.byref(config), 1, ctypes.byref(n)) and n.value > 0, "eglChooseConfig")
    ctx = check(egl.eglCreateContext(dpy, config, None, attribs(
        EGL_CONTEXT_MAJOR_VERSION, 4,
        EGL_CONTEXT_MINOR_VERSION, 5,
        EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,
    )), "eglCreateContext")
    check(egl.eglMakeCurrent(dpy, None, None, ctx), "eglMakeCurrent")
    print(f"[producer] GL {GL.glGetString(GL.GL_VERSION).decode()} / {GL.glGetString(GL.GL_RENDERER).decode()}", flush=True)
    return ctx


def create_render_target(width, height):
    tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    GL.glTexStorage2D(GL.GL_TEXTURE_2D, 1, GL.GL_RGBA8, width, height)
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
    fbo = GL.glGenFramebuffers(1)
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
    GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
    status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
    if status != GL.GL_FRAMEBUFFER_COMPLETE:
        sys.exit(f"[producer] FBO incomplete: 0x{int(status):04x}")
    GL.glViewport(0, 0, width, height)
    return tex, fbo


def export_dmabuf(dpy, ctx, tex, height):
    image = check(eglCreateImageKHR(dpy, ctx, EGL_GL_TEXTURE_2D_KHR, vp(int(tex)),
                                    attribs(EGL_GL_TEXTURE_LEVEL_KHR, 0)), "eglCreateImageKHR")
    fourcc, num_planes = ctypes.c_int(), ctypes.c_int()
    modifiers = (EGLuint64KHR * 4)()
    check(eglExportDMABUFImageQueryMESA(dpy, image, ctypes.byref(fourcc), ctypes.byref(num_planes), modifiers),
          "eglExportDMABUFImageQueryMESA")
    fds = (ctypes.c_int * 4)(-1, -1, -1, -1)
    strides, offsets = (EGLint * 4)(), (EGLint * 4)()
    check(eglExportDMABUFImageMESA(dpy, image, fds, strides, offsets), "eglExportDMABUFImageMESA")
    eglDestroyImageKHR(dpy, image)
    GL.glFinish()

    planes = []
    for i in range(num_planes.value):
        # The dma-buf's real size; tiled layouts can be larger than stride*height.
        try:
            size = os.lseek(fds[i], 0, os.SEEK_END) - offsets[i]
        except OSError:
            size = strides[i] * height
        planes.append({"fd": fds[i], "stride": strides[i], "offset": offsets[i], "size": size})
    return fourcc_str(fourcc.value), modifiers[0], planes


class LineSocket:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.buf = b""

    def send(self, msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def recv(self, timeout):
        """Return the next message, None on timeout. Raises EOFError on close."""
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: wayland_producer.py <port> <width> <height>")
    port, width, height = (int(a) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if os.environ.get("RENDER_NODE"):
        render_node, reason = os.environ["RENDER_NODE"], "env"
    else:
        render_node, reason = default_render_node()
    print(f"[producer] using render node {render_node} ({reason})", flush=True)
    dpy = open_display(render_node)
    ctx = create_context(dpy)
    tex, fbo = create_render_target(width, height)
    fourcc, modifier, planes = export_dmabuf(dpy, ctx, tex, height)
    print(f"[producer] exported {width}x{height} fourcc={fourcc} modifier=0x{modifier:016x} planes={planes}", flush=True)
    if fourcc not in PIXEL_FORMATS:
        sys.exit(f"[producer] unsupported fourcc {fourcc}")

    conn = LineSocket(port)
    try:
        conn.send({
            "type": "texture",
            "pid": os.getpid(),
            "width": width,
            "height": height,
            "fourcc": fourcc,
            "pixelFormat": PIXEL_FORMATS[fourcc],
            "modifier": str(modifier),
            "planes": planes,
        })
        seq = 0
        frame_interval = 1 / 60
        while True:
            start = time.monotonic()
            GL.glClearColor(0.5 + 0.5 * math.sin(time.time()), 0.0, 0.0, 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            GL.glFinish()
            conn.send({"type": "frame", "seq": seq, "timestamp_us": int(time.time() * 1e6)})
            # Backpressure: don't touch the texture until Electron is done reading it.
            deadline = start + 1.0
            while True:
                msg = conn.recv(max(0.0, deadline - time.monotonic()))
                if msg is None:
                    print(f"[producer] frame {seq}: no frame_done within 1s, continuing", flush=True)
                    break
                if msg.get("type") == "frame_done" and msg.get("seq") == seq:
                    break
            seq += 1
            remaining = frame_interval - (time.monotonic() - start)
            if remaining > 0:
                time.sleep(remaining)
    except (EOFError, ConnectionError):
        print("[producer] connection closed, exiting", flush=True)
    finally:
        conn.sock.close()
        for p in planes:
            os.close(p["fd"])
        GL.glDeleteFramebuffers(1, [fbo])
        GL.glDeleteTextures(1, [tex])
        egl.eglMakeCurrent(dpy, None, None, None)
        egl.eglDestroyContext(dpy, ctx)
        egl.eglTerminate(dpy)


if __name__ == "__main__":
    main()
