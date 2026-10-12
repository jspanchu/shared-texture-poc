"""Render a VTK scene straight into a global IOSurface (macOS).

Usage: vtk_frame_producer.py <port> <width> <height>

The vtkOpenGLRenderWindow resolves its RenderFramebuffer into the DisplayFramebuffer
in Frame(). We swap the DisplayFramebuffer's color attachment for an IOSurface-backed
GL_TEXTURE_RECTANGLE, and use offscreen buffers so nothing is blitted to the window.

Talks newline-delimited JSON to the Electron main process on 127.0.0.1:<port>:
  -> {"type":"texture", ...}   once, with the IOSurface's global ID
  -> {"type":"frame", ...}     after every render (glFinish'd)
  <- {"type":"frame_done", ...} when Electron released the texture
"""

import ctypes
import json
import os
import signal
import socket
import sys
import time

from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
from vtkmodules.vtkRenderingCore import vtkCompositePolyDataMapper, vtkVolume, vtkActor, vtkRenderer, vtkRenderWindow, vtkRenderWindowInteractor, vtkColorTransferFunction, vtkVolumeProperty
from vtkmodules.vtkRenderingOpenGL2 import vtkTextureObject
from vtkmodules.vtkRenderingVolumeOpenGL2 import vtkSmartVolumeMapper
from vtkmodules.vtkFiltersSources import vtkPartitionedDataSetCollectionSource
from vtkmodules.vtkImagingCore import vtkRTAnalyticSource
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

import vtkmodules.vtkRenderingUI

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


# --- CGL / GL ------------------------------------------------------------------
# VTK already resolved GL from this framework, so a handful of direct calls beats
# pulling in PyOpenGL. Everything framebuffer-related goes through VTK so its
# vtkOpenGLState cache stays in sync.
cgl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
cgl.CGLGetCurrentContext.restype = vp
cgl.CGLErrorString.restype = ctypes.c_char_p
cgl.CGLErrorString.argtypes = [ctypes.c_int]
cgl.CGLTexImageIOSurface2D.argtypes = [
    vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
    ctypes.c_uint32, ctypes.c_uint32, vp, ctypes.c_uint32]
cgl.glGenTextures.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32)]
cgl.glDeleteTextures.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32)]
cgl.glBindTexture.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
cgl.glGetString.restype = ctypes.c_char_p
cgl.glGetString.argtypes = [ctypes.c_uint32]

GL_VERSION = 0x1F02
GL_RENDERER = 0x1F01
GL_TEXTURE_RECTANGLE = 0x84F5
GL_RGBA = 0x1908
GL_BGRA = 0x80E1
GL_UNSIGNED_INT_8_8_8_8_REV = 0x8367
GL_FRAMEBUFFER = 0x8D40

# IOSurface pixel format 'BGRA' -> electron pixelFormat
PIXEL_FORMAT_BGRA = 0x42475241


def check(err, what):
    if err != 0:
        sys.exit(f"[producer] {what} failed: {cgl.CGLErrorString(err).decode()} ({err})")


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
    # The IOSurface's alpha reaches Electron, so keep the background opaque.
    renderer.SetBackgroundAlpha(1.0)

    render_window = vtkRenderWindow(
            interactor=vtkRenderWindowInteractor(interactor_style=vtkInteractorStyleTrackballCamera()),
            # offscreen=True,
            # show_window=False,
    )
    render_window.AddRenderer(renderer)
    return render_window


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


def attach_surface(render_window, surface, width, height):
    """Make the IOSurface the color attachment 0 of the window's DisplayFramebuffer."""
    render_window.MakeCurrent()
    ctx = cgl.CGLGetCurrentContext()
    print(f"[producer] GL {cgl.glGetString(GL_VERSION).decode()} / {cgl.glGetString(GL_RENDERER).decode()}", flush=True)

    # CGL only binds IOSurfaces to rectangle textures.
    handle = ctypes.c_uint32()
    cgl.glGenTextures(1, ctypes.byref(handle))
    cgl.glBindTexture(GL_TEXTURE_RECTANGLE, handle.value)
    check(cgl.CGLTexImageIOSurface2D(ctx, GL_TEXTURE_RECTANGLE, GL_RGBA, width, height,
                                     GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, surface, 0),
          "CGLTexImageIOSurface2D")
    cgl.glBindTexture(GL_TEXTURE_RECTANGLE, 0)

    tex = vtkTextureObject()
    tex.SetContext(render_window)
    tex.AssignToExistingTexture(handle.value, GL_TEXTURE_RECTANGLE)
    # Only records the size: an assigned texture has no dimensions, so Resize() never
    # calls glTexImage2D (which would detach the IOSurface). The DisplayFramebuffer
    # resizes its attachments on every Start(), and this keeps that a no-op too.
    tex.Resize(width, height)

    display_fbo = render_window.GetDisplayFramebuffer()
    display_fbo.AddColorAttachment(0, tex)
    display_fbo.Bind()
    complete = display_fbo.CheckFrameBufferStatus(GL_FRAMEBUFFER)
    display_fbo.UnBind()
    if not complete:
        sys.exit("[producer] DisplayFramebuffer incomplete with the IOSurface attachment")
    return tex, handle


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
        sys.exit("usage: vtk_frame_producer.py <port> <width> <height>")
    port, width, height = (int(a) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    render_window = create_vtk_pipeline()
    render_window.SetSize(width, height)
    # Render into the FBOs only: Frame() then stops after resolving into the DisplayFramebuffer.
    render_window.SetUseOffScreenBuffers(True)
    render_window.SetShowWindow(False)
    # GL's first row is the bottom, an IOSurface's is the top.
    render_window.SetFramebufferFlipY(True)
    # The first render creates the context and the framebuffers we're about to patch.
    render_window.Render()

    surface = create_surface(width, height)
    tex, handle = attach_surface(render_window, surface, width, height)
    surface_id = iosurface.IOSurfaceGetID(surface)
    print(f"[producer] IOSurface id={surface_id} {width}x{height} BGRA "
          f"stride={iosurface.IOSurfaceGetBytesPerRow(surface)}", flush=True)

    camera = render_window.GetRenderers().GetFirstRenderer().GetActiveCamera()
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
            camera.Azimuth(1.0)
            render_window.Render()
            # IOSurfaces have no implicit fences, so finish before telling Electron.
            render_window.MakeCurrent()
            cgl.glFinish()
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
        render_window.MakeCurrent()
        render_window.ReleaseGraphicsResources(render_window)
        cgl.glDeleteTextures(1, ctypes.byref(handle))
        render_window.Finalize()
        cf.CFRelease(surface)


if __name__ == "__main__":
    main()
