"""Render a VTK scene on a headless EGL context into a texture exported as DMA-BUF (Wayland).

Usage: wayland_vtk_frame_producer.py <port> <width> <height>

The vtkOpenGLRenderWindow resolves its RenderFramebuffer into the DisplayFramebuffer
in Frame(). We swap the DisplayFramebuffer's color attachment for an RGBA8 texture
exported with eglExportDMABUFImageMESA, and use offscreen buffers so Frame() stops there.
The texture is reallocated and exported again when the window is resized.

See producer_common.py for the protocol with the Electron main process and input handling;
the "texture" message carries the dma-buf's planes, as fds in this process.
"""

import ctypes
import glob
import os
import signal
import sys

from vtkmodules.vtkCommonCore import VTK_UNSIGNED_CHAR
from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
from vtkmodules.vtkRenderingCore import vtkCompositePolyDataMapper, vtkVolume, vtkActor, vtkRenderer, vtkColorTransferFunction, vtkVolumeProperty
from vtkmodules.vtkRenderingOpenGL2 import vtkEGLRenderWindow, vtkTextureObject
from vtkmodules.vtkRenderingVolumeOpenGL2 import vtkSmartVolumeMapper
from vtkmodules.vtkFiltersSources import vtkPartitionedDataSetCollectionSource
from vtkmodules.vtkImagingCore import vtkRTAnalyticSource

from producer_common import LineSocket, serve

# --- EGL -----------------------------------------------------------------------
# VTK owns the EGL display and context; this only picks the device and exports
# the texture. Everything GL goes through VTK so its vtkOpenGLState cache stays in sync.
EGL_NONE = 0x3038
EGL_EXTENSIONS = 0x3055
EGL_DRM_RENDER_NODE_FILE_EXT = 0x3377
EGL_GL_TEXTURE_2D_KHR = 0x30B1
EGL_GL_TEXTURE_LEVEL_KHR = 0x30BC
GL_FRAMEBUFFER = 0x8D40

EGLint = ctypes.c_int32
EGLBoolean = ctypes.c_uint32
EGLenum = ctypes.c_uint32
EGLuint64KHR = ctypes.c_uint64
vp = ctypes.c_void_p

egl = ctypes.CDLL("libEGL.so.1")
egl.eglGetProcAddress.restype = vp
egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
egl.eglGetError.restype = EGLint
egl.eglQueryString.argtypes = [vp, EGLint]
egl.eglQueryString.restype = ctypes.c_char_p
egl.eglGetCurrentDisplay.restype = vp
egl.eglGetCurrentContext.restype = vp


def egl_ext(name, restype, *argtypes):
    addr = egl.eglGetProcAddress(name.encode())
    if not addr:
        sys.exit(f"[producer] missing EGL function {name}")
    return ctypes.CFUNCTYPE(restype, *argtypes)(addr)


eglQueryDevicesEXT = egl_ext("eglQueryDevicesEXT", EGLBoolean, EGLint, ctypes.POINTER(vp), ctypes.POINTER(EGLint))
eglQueryDeviceStringEXT = egl_ext("eglQueryDeviceStringEXT", ctypes.c_char_p, vp, EGLint)
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


def device_index(render_node):
    """The index of render_node among the EGL devices, as vtkEGLRenderWindow counts them."""
    devices = (vp * 16)()
    count = EGLint()
    check(eglQueryDevicesEXT(16, devices, ctypes.byref(count)), "eglQueryDevicesEXT")
    for i in range(count.value):
        node = eglQueryDeviceStringEXT(devices[i], EGL_DRM_RENDER_NODE_FILE_EXT)
        node = node.decode() if node else None
        print(f"[producer] EGL device {i}: render node {node}", flush=True)
        if node == render_node:
            return i
    sys.exit(f"[producer] no EGL device with render node {render_node}")


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
    # The texture's alpha reaches Electron, so keep the background opaque.
    renderer.SetBackgroundAlpha(1.0)

    # The object factory would pick vtkXOpenGLRenderWindow through XWayland, but
    # the dma-buf has to come from an EGL context.
    render_window = vtkEGLRenderWindow()
    render_window.AddRenderer(renderer)
    return render_window


def attach_texture(render_window, width, height):
    """Make a fresh RGBA8 texture the color attachment 0 of the window's DisplayFramebuffer."""
    tex = vtkTextureObject()
    tex.SetContext(render_window)
    if not tex.Allocate2D(width, height, 4, VTK_UNSIGNED_CHAR):
        sys.exit("[producer] allocating the RGBA8 texture failed")

    display_fbo = render_window.GetDisplayFramebuffer()
    display_fbo.AddColorAttachment(0, tex)
    display_fbo.Bind()
    complete = display_fbo.CheckFrameBufferStatus(GL_FRAMEBUFFER)
    display_fbo.UnBind()
    if not complete:
        sys.exit("[producer] DisplayFramebuffer incomplete with the exported texture")
    return tex


def export_dmabuf(render_window, tex, height):
    render_window.MakeCurrent()
    dpy, ctx = egl.eglGetCurrentDisplay(), egl.eglGetCurrentContext()
    exts = egl.eglQueryString(dpy, EGL_EXTENSIONS).decode().split()
    for ext in ("EGL_MESA_image_dma_buf_export", "EGL_KHR_gl_texture_2D_image"):
        if ext not in exts:
            sys.exit(f"[producer] display lacks {ext}")

    image = check(eglCreateImageKHR(dpy, ctx, EGL_GL_TEXTURE_2D_KHR, vp(tex.GetHandle()),
                                    attribs(EGL_GL_TEXTURE_LEVEL_KHR, 0)), "eglCreateImageKHR")
    fourcc, num_planes = ctypes.c_int(), ctypes.c_int()
    modifiers = (EGLuint64KHR * 4)()
    check(eglExportDMABUFImageQueryMESA(dpy, image, ctypes.byref(fourcc), ctypes.byref(num_planes), modifiers),
          "eglExportDMABUFImageQueryMESA")
    fds = (ctypes.c_int * 4)(-1, -1, -1, -1)
    strides, offsets = (EGLint * 4)(), (EGLint * 4)()
    check(eglExportDMABUFImageMESA(dpy, image, fds, strides, offsets), "eglExportDMABUFImageMESA")
    eglDestroyImageKHR(dpy, image)
    render_window.WaitForCompletion()

    planes = []
    for i in range(num_planes.value):
        # The dma-buf's real size; tiled layouts can be larger than stride*height.
        try:
            size = os.lseek(fds[i], 0, os.SEEK_END) - offsets[i]
        except OSError:
            size = strides[i] * height
        planes.append({"fd": fds[i], "stride": strides[i], "offset": offsets[i], "size": size})
    return fourcc_str(fourcc.value), modifiers[0], planes


class ExportedTexture:
    """The texture VTK resolves into, exported as a dma-buf, at one size."""

    def __init__(self, render_window, width, height):
        self.render_window = render_window
        self.width, self.height = width, height
        render_window.MakeCurrent()
        self.tex = attach_texture(render_window, width, height)
        self.fourcc, self.modifier, self.planes = export_dmabuf(render_window, self.tex, height)
        print(f"[producer] exported {width}x{height} fourcc={self.fourcc} modifier=0x{self.modifier:016x} "
              f"planes={self.planes}", flush=True)
        if self.fourcc not in PIXEL_FORMATS:
            sys.exit(f"[producer] unsupported fourcc {self.fourcc}")

    def message(self):
        return {
            "type": "texture",
            "pid": os.getpid(),
            "width": self.width,
            "height": self.height,
            "fourcc": self.fourcc,
            "pixelFormat": PIXEL_FORMATS[self.fourcc],
            "modifier": str(self.modifier),
            "planes": self.planes,
        }

    def release(self):
        for p in self.planes:
            os.close(p["fd"])
        self.render_window.MakeCurrent()
        self.tex.ReleaseGraphicsResources(self.render_window)


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: wayland_vtk_frame_producer.py <port> <width> <height>")
    port, width, height = (int(a) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if os.environ.get("RENDER_NODE"):
        render_node, reason = os.environ["RENDER_NODE"], "env"
    else:
        render_node, reason = default_render_node()
    print(f"[producer] using render node {render_node} ({reason})", flush=True)

    # vtkEGLRenderWindow::SetDeviceIndex doesn't reach the EGL display setup; the
    # constructor only reads the device from this variable.
    os.environ["VTK_EGL_DEVICE_INDEX"] = str(device_index(render_node))
    render_window = create_vtk_pipeline()
    render_window.SetSize(width, height)
    # Render into the FBOs only: Frame() then stops after resolving into the DisplayFramebuffer.
    render_window.SetUseOffScreenBuffers(True)
    render_window.SetShowWindow(False)
    # The first render creates the context and the framebuffers we're about to patch.
    render_window.Render()
    caps = dict(line.split(":", 1) for line in render_window.ReportCapabilities().splitlines() if ":" in line)
    print(f"[producer] GL {caps['OpenGL version string'].strip()} / {caps['OpenGL renderer string'].strip()}", flush=True)

    shared = ExportedTexture(render_window, width, height)

    def render():
        render_window.Render()
        # glFinish, so the frame is complete before Electron samples the dma-buf.
        render_window.WaitForCompletion()

    def resize(width, height):
        nonlocal shared
        old, shared = shared, ExportedTexture(render_window, width, height)
        old.release()
        return shared.message()

    conn = LineSocket(port)
    try:
        conn.send(shared.message())
        serve(conn, render_window, render, resize)
    except (EOFError, ConnectionError):
        print("[producer] connection closed, exiting", flush=True)
    finally:
        conn.sock.close()
        shared.release()
        render_window.MakeCurrent()
        render_window.ReleaseGraphicsResources(render_window)
        render_window.Finalize()


if __name__ == "__main__":
    main()
