"""Render into a global IOSurface on a windowless CGL context (Apple Silicon).

Usage: macos_producer.py <port> <width> <height>

Talks newline-delimited JSON to the Electron main process on 127.0.0.1:<port>:
  -> {"type":"texture", ...}   once, with the IOSurface's global ID
  -> {"type":"frame", ...}     after every render (glFinish'd)
  <- {"type":"frame_done", ...} when Electron released the texture
"""

import ctypes
import json
import logging
import math
import os
import signal
import socket
import sys
import time

# PyOpenGL's plugin loader warns that numpy is missing; it isn't needed here.
logging.getLogger("OpenGL.plugins").setLevel(logging.ERROR)

from OpenGL import GL  # noqa: E402

vp = ctypes.c_void_p

# --- CoreFoundation / IOSurface ----------------------------------------------
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
cf.CFNumberCreate.restype = vp
cf.CFNumberCreate.argtypes = [vp, ctypes.c_long, vp]
cf.CFDictionaryCreateMutable.restype = vp
cf.CFDictionaryCreateMutable.argtypes = [vp, ctypes.c_long, vp, vp]
cf.CFDictionarySetValue.argtypes = [vp, vp, vp]
cf.CFRelease.argtypes = [vp]
kCFNumberSInt32Type = 3
kCFBooleanTrue = vp.in_dll(cf, "kCFBooleanTrue")

iosurface = ctypes.CDLL("/System/Library/Frameworks/IOSurface.framework/IOSurface")
iosurface.IOSurfaceCreate.restype = vp
iosurface.IOSurfaceCreate.argtypes = [vp]
iosurface.IOSurfaceGetID.restype = ctypes.c_uint32
iosurface.IOSurfaceGetID.argtypes = [vp]
iosurface.IOSurfaceGetBytesPerRow.restype = ctypes.c_size_t
iosurface.IOSurfaceGetBytesPerRow.argtypes = [vp]


def iosurface_key(name):
    return vp.in_dll(iosurface, name)


# --- CGL -----------------------------------------------------------------------
cgl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
cgl.CGLChoosePixelFormat.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(vp), ctypes.POINTER(ctypes.c_int32)]
cgl.CGLCreateContext.argtypes = [vp, vp, ctypes.POINTER(vp)]
cgl.CGLSetCurrentContext.argtypes = [vp]
cgl.CGLDestroyPixelFormat.argtypes = [vp]
cgl.CGLDestroyContext.argtypes = [vp]
cgl.CGLErrorString.restype = ctypes.c_char_p
cgl.CGLErrorString.argtypes = [ctypes.c_int]
cgl.CGLTexImageIOSurface2D.argtypes = [
    vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
    ctypes.c_uint32, ctypes.c_uint32, vp, ctypes.c_uint32]

kCGLPFAAccelerated = 73
kCGLPFAOpenGLProfile = 99
kCGLOGLPVersion_GL4_Core = 0x4100

# IOSurface pixel format 'BGRA' -> electron pixelFormat
PIXEL_FORMAT_BGRA = 0x42475241


def check(err, what):
    if err != 0:
        sys.exit(f"[producer] {what} failed: {cgl.CGLErrorString(err).decode()} ({err})")


def create_context():
    attrs = (ctypes.c_int32 * 4)(kCGLPFAOpenGLProfile, kCGLOGLPVersion_GL4_Core, kCGLPFAAccelerated, 0)
    pix, npix = vp(), ctypes.c_int32()
    check(cgl.CGLChoosePixelFormat(attrs, ctypes.byref(pix), ctypes.byref(npix)), "CGLChoosePixelFormat")
    if not pix:
        sys.exit("[producer] no accelerated GL 4.1 core pixel format")
    ctx = vp()
    check(cgl.CGLCreateContext(pix, None, ctypes.byref(ctx)), "CGLCreateContext")
    cgl.CGLDestroyPixelFormat(pix)
    check(cgl.CGLSetCurrentContext(ctx), "CGLSetCurrentContext")
    print(f"[producer] GL {GL.glGetString(GL.GL_VERSION).decode()} / {GL.glGetString(GL.GL_RENDERER).decode()}", flush=True)
    return ctx


def create_surface(width, height):
    """A BGRA IOSurface that other processes can open with IOSurfaceLookup(id)."""
    props = cf.CFDictionaryCreateMutable(
        None, 0,
        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
    for key, value in (("kIOSurfaceWidth", width), ("kIOSurfaceHeight", height),
                       ("kIOSurfaceBytesPerElement", 4), ("kIOSurfacePixelFormat", PIXEL_FORMAT_BGRA)):
        v = ctypes.c_int32(value)
        number = cf.CFNumberCreate(None, kCFNumberSInt32Type, ctypes.byref(v))
        cf.CFDictionarySetValue(props, iosurface_key(key), number)
        cf.CFRelease(number)
    # Deprecated, but the only way to share an IOSurface without passing a Mach port.
    cf.CFDictionarySetValue(props, iosurface_key("kIOSurfaceIsGlobal"), kCFBooleanTrue)
    surface = iosurface.IOSurfaceCreate(props)
    cf.CFRelease(props)
    if not surface:
        sys.exit("[producer] IOSurfaceCreate failed")
    return surface


def create_render_target(ctx, surface, width, height):
    # CGL only binds IOSurfaces to rectangle textures.
    tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_RECTANGLE, tex)
    check(cgl.CGLTexImageIOSurface2D(ctx, GL.GL_TEXTURE_RECTANGLE, GL.GL_RGBA, width, height,
                                     GL.GL_BGRA, GL.GL_UNSIGNED_INT_8_8_8_8_REV, surface, 0),
          "CGLTexImageIOSurface2D")
    GL.glBindTexture(GL.GL_TEXTURE_RECTANGLE, 0)
    fbo = GL.glGenFramebuffers(1)
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
    GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_RECTANGLE, tex, 0)
    status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
    if status != GL.GL_FRAMEBUFFER_COMPLETE:
        sys.exit(f"[producer] FBO incomplete: 0x{int(status):04x}")
    GL.glViewport(0, 0, width, height)
    return tex, fbo


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
        sys.exit("usage: macos_producer.py <port> <width> <height>")
    port, width, height = (int(a) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    ctx = create_context()
    surface = create_surface(width, height)
    tex, fbo = create_render_target(ctx, surface, width, height)
    surface_id = iosurface.IOSurfaceGetID(surface)
    print(f"[producer] IOSurface id={surface_id} {width}x{height} BGRA "
          f"stride={iosurface.IOSurfaceGetBytesPerRow(surface)}", flush=True)

    conn = LineSocket(port)
    try:
        conn.send({
            "type": "texture",
            "pid": os.getpid(),
            "width": width,
            "height": height,
            "pixelFormat": "bgra",
            "ioSurfaceId": surface_id,
        })
        seq = 0
        frame_interval = 1 / 60
        while True:
            start = time.monotonic()
            GL.glClearColor(0.5 + 0.5 * math.sin(time.time()), 0.0, 0.0, 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            # IOSurfaces have no implicit fences, so finish before telling Electron.
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
        GL.glDeleteFramebuffers(1, [fbo])
        GL.glDeleteTextures(1, [tex])
        cgl.CGLSetCurrentContext(None)
        cgl.CGLDestroyContext(ctx)
        cf.CFRelease(surface)


if __name__ == "__main__":
    main()
