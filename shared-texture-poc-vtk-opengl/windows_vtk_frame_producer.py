"""Render a VTK scene into a D3D11 texture (WGL_NV_DX_interop2) and share it with Electron (Windows).

Usage: windows_vtk_frame_producer.py <port> <width> <height>

The vtkOpenGLRenderWindow resolves its RenderFramebuffer into the DisplayFramebuffer
in Frame(). We swap the DisplayFramebuffer's color attachment for a GL texture that
aliases a D3D11 texture through WGL_NV_DX_interop2, and use offscreen buffers so
Frame() stops there. Each frame is then copied into a texture shared by NT handle.
Both textures are reallocated when the window is resized.

See producer_common.py for the protocol with the Electron main process and input handling;
the "texture" message carries the shared texture's NT handle in this process.

Environment:
  D3D_ADAPTER=<index>  DXGI adapter to allocate on (default 0, the one Chromium uses)
"""

import ctypes
import os
import signal
import sys
import time
from ctypes import wintypes

from producer_common import LineSocket, center_pixel, serve

from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
from vtkmodules.vtkRenderingCore import vtkCompositePolyDataMapper, vtkVolume, vtkActor, vtkRenderer, vtkRenderWindow, vtkColorTransferFunction, vtkVolumeProperty
from vtkmodules.vtkRenderingOpenGL2 import vtkTextureObject
from vtkmodules.vtkRenderingVolumeOpenGL2 import vtkSmartVolumeMapper
from vtkmodules.vtkFiltersSources import vtkPartitionedDataSetCollectionSource
from vtkmodules.vtkImagingCore import vtkRTAnalyticSource

vp = ctypes.c_void_p
HRESULT = ctypes.c_long


def check_hr(hr, what):
    if hr < 0:
        sys.exit(f"[producer] {what} failed: HRESULT 0x{hr & 0xffffffff:08x}")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        a, b, c, d, e = s.split("-")
        super().__init__(int(a, 16), int(b, 16), int(c, 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(d + e)))


IID_IDXGIFactory1 = GUID("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIResource1 = GUID("30961379-4609-4a41-998e-54fe567ee0c1")


def com_call(obj, index, restype, *args):
    """Call method `index` of a COM object's vtable (indices from the Windows SDK headers)."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(vp))).contents
    fn = ctypes.WINFUNCTYPE(restype, vp, *(type(a) if isinstance(a, ctypes._SimpleCData) else vp for a in args))(vtbl[index])
    return fn(obj, *args)


def release(obj):
    com_call(obj, 2, ctypes.c_ulong)  # IUnknown::Release


# --- D3D11 / DXGI ----------------------------------------------------------------
dxgi = ctypes.WinDLL("dxgi")
d3d11 = ctypes.WinDLL("d3d11")
kernel32 = ctypes.WinDLL("kernel32")
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID), ("Flags", ctypes.c_uint)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint), ("MipLevels", ctypes.c_uint),
                ("ArraySize", ctypes.c_uint), ("Format", ctypes.c_uint), ("SampleCount", ctypes.c_uint),
                ("SampleQuality", ctypes.c_uint), ("Usage", ctypes.c_uint), ("BindFlags", ctypes.c_uint),
                ("CPUAccessFlags", ctypes.c_uint), ("MiscFlags", ctypes.c_uint)]


class D3D11_QUERY_DESC(ctypes.Structure):
    _fields_ = [("Query", ctypes.c_uint), ("MiscFlags", ctypes.c_uint)]


DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20
D3D11_RESOURCE_MISC_SHARED = 0x2
D3D11_RESOURCE_MISC_SHARED_NTHANDLE = 0x800
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_QUERY_EVENT = 0
DXGI_SHARED_RESOURCE_READ = 0x80000000
DXGI_SHARED_RESOURCE_WRITE = 1

# vtable indices
IDXGIFactory1_EnumAdapters1 = 12
IDXGIAdapter1_GetDesc1 = 10
ID3D11Device_CreateTexture2D = 5
ID3D11Device_CreateQuery = 24
ID3D11DeviceContext_End = 28
ID3D11DeviceContext_GetData = 29
ID3D11DeviceContext_CopyResource = 47
IDXGIResource1_CreateSharedHandle = 13


def create_device():
    factory = vp()
    check_hr(dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)), "CreateDXGIFactory1")
    index = int(os.environ.get("D3D_ADAPTER", "0"))
    adapter = vp()
    check_hr(com_call(factory, IDXGIFactory1_EnumAdapters1, HRESULT, ctypes.c_uint(index), ctypes.byref(adapter)),
             f"EnumAdapters1({index})")
    desc = DXGI_ADAPTER_DESC1()
    check_hr(com_call(adapter, IDXGIAdapter1_GetDesc1, HRESULT, ctypes.byref(desc)), "GetDesc1")
    luid = (desc.AdapterLuid.HighPart << 32) | desc.AdapterLuid.LowPart
    print(f"[producer] D3D11 adapter {index}: {desc.Description} (luid 0x{luid:x})", flush=True)

    device, context = vp(), vp()
    check_hr(d3d11.D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                     None, 0, D3D11_SDK_VERSION, ctypes.byref(device), None, ctypes.byref(context)),
             "D3D11CreateDevice")
    release(adapter)
    release(factory)
    return device, context


def create_texture(device, width, height, misc_flags=0):
    desc = D3D11_TEXTURE2D_DESC(
        Width=width, Height=height, MipLevels=1, ArraySize=1, Format=DXGI_FORMAT_B8G8R8A8_UNORM,
        SampleCount=1, BindFlags=D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE, MiscFlags=misc_flags)
    texture = vp()
    check_hr(com_call(device, ID3D11Device_CreateTexture2D, HRESULT, ctypes.byref(desc), None, ctypes.byref(texture)),
             "CreateTexture2D")
    return texture


def create_shared_texture(device, width, height):
    """A BGRA texture with an NT handle that Chromium can open."""
    texture = create_texture(device, width, height,
                             D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE)
    resource = vp()
    check_hr(com_call(texture, 0, HRESULT, ctypes.byref(IID_IDXGIResource1), ctypes.byref(resource)),
             "QueryInterface(IDXGIResource1)")
    handle = wintypes.HANDLE()
    check_hr(com_call(resource, IDXGIResource1_CreateSharedHandle, HRESULT, None,
                      wintypes.DWORD(DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE), None,
                      ctypes.byref(handle)),
             "CreateSharedHandle")
    release(resource)
    return texture, handle.value


# --- WGL / GL --------------------------------------------------------------------
# VTK owns the WGL context; this only registers the D3D11 texture with it.
# Everything framebuffer-related goes through VTK so its vtkOpenGLState cache stays in sync.
opengl32 = ctypes.WinDLL("opengl32")
opengl32.wglGetProcAddress.restype = vp
opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]
opengl32.glGenTextures.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32)]
opengl32.glDeleteTextures.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32)]

GL_TEXTURE_2D = 0x0DE1
GL_FRAMEBUFFER = 0x8D40
WGL_ACCESS_WRITE_DISCARD_NV = 0x0002


def wgl_func(name, restype, *argtypes):
    addr = opengl32.wglGetProcAddress(name.encode())
    if not addr:
        sys.exit(f"[producer] {name} missing: the GL driver doesn't support WGL_NV_DX_interop2")
    return ctypes.WINFUNCTYPE(restype, *argtypes)(addr)


class DXInterop:
    """WGL_NV_DX_interop2 entry points; needs a current WGL context to resolve."""

    def __init__(self):
        H = wintypes.HANDLE
        self.open_device = wgl_func("wglDXOpenDeviceNV", H, vp)
        self.close_device = wgl_func("wglDXCloseDeviceNV", wintypes.BOOL, H)
        self.register = wgl_func("wglDXRegisterObjectNV", H, H, vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
        self.unregister = wgl_func("wglDXUnregisterObjectNV", wintypes.BOOL, H, H)
        self.lock = wgl_func("wglDXLockObjectsNV", wintypes.BOOL, H, ctypes.c_int32, ctypes.POINTER(H))
        self.unlock = wgl_func("wglDXUnlockObjectsNV", wintypes.BOOL, H, ctypes.c_int32, ctypes.POINTER(H))


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

    # The object factory picks vtkWin32OpenGLRenderWindow; its hidden window provides the WGL context.
    render_window = vtkRenderWindow()
    render_window.AddRenderer(renderer)
    return render_window


def attach_texture(render_window, interop, gl_device, d3d_texture, width, height):
    """Make the D3D11 texture the color attachment 0 of the window's DisplayFramebuffer."""
    render_window.MakeCurrent()
    handle = ctypes.c_uint32()
    opengl32.glGenTextures(1, ctypes.byref(handle))
    obj = interop.register(gl_device, d3d_texture, handle.value, GL_TEXTURE_2D, WGL_ACCESS_WRITE_DISCARD_NV)
    if not obj:
        sys.exit(f"[producer] wglDXRegisterObjectNV failed: {ctypes.GetLastError()}")
    objs = (wintypes.HANDLE * 1)(obj)

    tex = vtkTextureObject()
    tex.SetContext(render_window)
    tex.AssignToExistingTexture(handle.value, GL_TEXTURE_2D)
    # Only records the size: an assigned texture has no dimensions, so Resize() never
    # calls glTexImage2D (which would detach the D3D11 storage). The DisplayFramebuffer
    # resizes its attachments on every Start(), and this keeps that a no-op too.
    tex.Resize(width, height)

    display_fbo = render_window.GetDisplayFramebuffer()
    interop.lock(gl_device, 1, objs)
    display_fbo.AddColorAttachment(0, tex)
    display_fbo.Bind()
    complete = display_fbo.CheckFrameBufferStatus(GL_FRAMEBUFFER)
    display_fbo.UnBind()
    interop.unlock(gl_device, 1, objs)
    if not complete:
        sys.exit("[producer] DisplayFramebuffer incomplete with the D3D11 texture")
    return tex, handle, objs


class SharedTexture:
    """VTK's D3D11 render target and the texture shared with Electron, at one size."""

    def __init__(self, render_window, device, interop, gl_device, width, height):
        self.render_window, self.interop, self.gl_device = render_window, interop, gl_device
        self.width, self.height = width, height
        # NVIDIA's interop can't register textures created with SHARED_NTHANDLE (ERROR_OPEN_FAILED),
        # so VTK renders into a private texture that gets copied into the shared one on the GPU.
        self.render_texture = create_texture(device, width, height)
        self.texture, self.nt_handle = create_shared_texture(device, width, height)
        self.tex, self.handle, self.objs = attach_texture(
            render_window, interop, gl_device, self.render_texture, width, height)
        print(f"[producer] D3D11 texture {width}x{height} BGRA NT handle=0x{self.nt_handle:x}", flush=True)

    def message(self):
        return {
            "type": "texture",
            "pid": os.getpid(),
            "width": self.width,
            "height": self.height,
            "pixelFormat": "bgra",
            "ntHandle": self.nt_handle,
        }

    def release(self):
        self.render_window.MakeCurrent()
        self.interop.unregister(self.gl_device, self.objs[0])
        opengl32.glDeleteTextures(1, ctypes.byref(self.handle))
        kernel32.CloseHandle(self.nt_handle)
        release(self.texture)
        release(self.render_texture)


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: windows_vtk_frame_producer.py <port> <width> <height>")
    port, width, height = (int(a) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    render_window = create_vtk_pipeline()
    render_window.SetSize(width, height)
    # Render into the FBOs only: Frame() then stops after resolving into the DisplayFramebuffer.
    render_window.SetUseOffScreenBuffers(True)
    render_window.SetShowWindow(False)
    # The first render creates the context and the framebuffers we're about to patch.
    render_window.Render()
    caps = dict(line.split(":", 1) for line in render_window.ReportCapabilities().splitlines() if ":" in line)
    print(f"[producer] GL {caps['OpenGL version string'].strip()} / {caps['OpenGL renderer string'].strip()}", flush=True)

    device, d3d_ctx = create_device()
    render_window.MakeCurrent()
    interop = DXInterop()
    gl_device = interop.open_device(device)
    if not gl_device:
        sys.exit(f"[producer] wglDXOpenDeviceNV failed: {ctypes.GetLastError()} "
                 "(GL and D3D11 on different GPUs? try D3D_ADAPTER)")
    shared = SharedTexture(render_window, device, interop, gl_device, width, height)

    # An event query tells us when the D3D11 device (and so the texture) is idle.
    query_desc = D3D11_QUERY_DESC(Query=D3D11_QUERY_EVENT)
    query = vp()
    check_hr(com_call(device, ID3D11Device_CreateQuery, HRESULT, ctypes.byref(query_desc), ctypes.byref(query)),
             "CreateQuery")
    done = wintypes.BOOL()

    def render():
        render_window.MakeCurrent()
        interop.lock(gl_device, 1, shared.objs)
        render_window.Render()
        # Unlocking hands the texture back to D3D11, ordered after VTK's GL commands.
        interop.unlock(gl_device, 1, shared.objs)
        com_call(d3d_ctx, ID3D11DeviceContext_CopyResource, None, shared.texture, shared.render_texture)
        # The shared texture has no keyed mutex, so wait until the GPU is done before telling Electron.
        com_call(d3d_ctx, ID3D11DeviceContext_End, None, query)
        while com_call(d3d_ctx, ID3D11DeviceContext_GetData, HRESULT, query, ctypes.byref(done),
                       ctypes.c_uint(ctypes.sizeof(done)), ctypes.c_uint(0)) != 0:
            time.sleep(0)

    def read_center_pixel(render_window):
        # The D3D11 texture is only readable from GL while locked.
        render_window.MakeCurrent()
        interop.lock(gl_device, 1, shared.objs)
        try:
            return center_pixel(render_window)
        finally:
            interop.unlock(gl_device, 1, shared.objs)

    def resize(width, height):
        nonlocal shared
        old, shared = shared, SharedTexture(render_window, device, interop, gl_device, width, height)
        old.release()
        return shared.message()

    conn = LineSocket(port)
    try:
        conn.send(shared.message())
        serve(conn, render_window, render, resize, read_center_pixel)
    except (EOFError, ConnectionError):
        print("[producer] connection closed, exiting", flush=True)
    finally:
        conn.sock.close()
        render_window.MakeCurrent()
        render_window.ReleaseGraphicsResources(render_window)
        shared.release()
        interop.close_device(gl_device)
        release(query)
        release(d3d_ctx)
        release(device)
        render_window.Finalize()


if __name__ == "__main__":
    main()
