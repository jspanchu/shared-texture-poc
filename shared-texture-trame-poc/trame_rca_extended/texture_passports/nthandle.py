"""Render into a D3D11 texture (WGL_NV_DX_interop2) and share it with Electron by NT handle (Windows).

The vtkOpenGLRenderWindow resolves its RenderFramebuffer into the DisplayFramebuffer in Frame().
We swap the DisplayFramebuffer's color attachment for a GL texture that aliases a D3D11 texture
through WGL_NV_DX_interop2. Each frame is then copied into a texture shared by NT handle.
"""

import ctypes
import os
import time
from ctypes import wintypes

from .abstract_texture_passport import AbstractTexturePassport
from vtkmodules.vtkRenderingOpenGL2 import vtkTextureObject

vp = ctypes.c_void_p
HRESULT = ctypes.c_long


def check_hr(hr, what):
    if hr < 0:
        raise RuntimeError(f"[producer] {what} failed: HRESULT 0x{hr & 0xffffffff:08x}")


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


def com_release(obj):
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


def _create_texture(device, width, height, misc_flags=0):
    desc = D3D11_TEXTURE2D_DESC(
        Width=width, Height=height, MipLevels=1, ArraySize=1, Format=DXGI_FORMAT_B8G8R8A8_UNORM,
        SampleCount=1, BindFlags=D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE, MiscFlags=misc_flags)
    texture = vp()
    check_hr(com_call(device, ID3D11Device_CreateTexture2D, HRESULT, ctypes.byref(desc), None, ctypes.byref(texture)),
             "CreateTexture2D")
    return texture


def _create_shared_texture(device, width, height):
    """A BGRA texture with an NT handle that Chromium can open."""
    texture = _create_texture(device, width, height,
                              D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE)
    resource = vp()
    check_hr(com_call(texture, 0, HRESULT, ctypes.byref(IID_IDXGIResource1), ctypes.byref(resource)),
             "QueryInterface(IDXGIResource1)")
    handle = wintypes.HANDLE()
    check_hr(com_call(resource, IDXGIResource1_CreateSharedHandle, HRESULT, None,
                      wintypes.DWORD(DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE), None,
                      ctypes.byref(handle)),
             "CreateSharedHandle")
    com_release(resource)
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


def _wgl_func(name, restype, *argtypes):
    addr = opengl32.wglGetProcAddress(name.encode())
    if not addr:
        raise RuntimeError(f"[producer] {name} missing: the GL driver doesn't support WGL_NV_DX_interop2")
    return ctypes.WINFUNCTYPE(restype, *argtypes)(addr)


class DXInterop:
    """WGL_NV_DX_interop2 entry points; needs a current WGL context to resolve."""

    def __init__(self):
        H = wintypes.HANDLE
        self.open_device = _wgl_func("wglDXOpenDeviceNV", H, vp)
        self.close_device = _wgl_func("wglDXCloseDeviceNV", wintypes.BOOL, H)
        self.register = _wgl_func("wglDXRegisterObjectNV", H, H, vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
        self.unregister = _wgl_func("wglDXUnregisterObjectNV", wintypes.BOOL, H, H)
        self.lock = _wgl_func("wglDXLockObjectsNV", wintypes.BOOL, H, ctypes.c_int32, ctypes.POINTER(H))
        self.unlock = _wgl_func("wglDXUnlockObjectsNV", wintypes.BOOL, H, ctypes.c_int32, ctypes.POINTER(H))


class D3DDevice:
    """The process's D3D11 device, shared by every passport, and its interop with VTK's GL context."""

    def __init__(self, adapter_index):
        factory = vp()
        check_hr(dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)), "CreateDXGIFactory1")
        adapter = vp()
        check_hr(com_call(factory, IDXGIFactory1_EnumAdapters1, HRESULT, ctypes.c_uint(adapter_index),
                          ctypes.byref(adapter)),
                 f"EnumAdapters1({adapter_index})")
        desc = DXGI_ADAPTER_DESC1()
        check_hr(com_call(adapter, IDXGIAdapter1_GetDesc1, HRESULT, ctypes.byref(desc)), "GetDesc1")
        luid = (desc.AdapterLuid.HighPart << 32) | desc.AdapterLuid.LowPart
        print(f"[producer] D3D11 adapter {adapter_index}: {desc.Description} (luid 0x{luid:x})", flush=True)

        self.device, self.context = vp(), vp()
        check_hr(d3d11.D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                         None, 0, D3D11_SDK_VERSION, ctypes.byref(self.device), None,
                                         ctypes.byref(self.context)),
                 "D3D11CreateDevice")
        com_release(adapter)
        com_release(factory)

        # An event query tells us when the D3D11 device (and so the texture) is idle.
        query_desc = D3D11_QUERY_DESC(Query=D3D11_QUERY_EVENT)
        self.query = vp()
        check_hr(com_call(self.device, ID3D11Device_CreateQuery, HRESULT, ctypes.byref(query_desc),
                          ctypes.byref(self.query)),
                 "CreateQuery")
        # Opened with the first passport: resolving the interop needs VTK's context.
        self.interop = None
        self.gl_device = None

    def open_interop(self, render_window):
        if self.interop:
            return
        render_window.MakeCurrent()
        self.interop = DXInterop()
        self.gl_device = self.interop.open_device(self.device)
        if not self.gl_device:
            raise RuntimeError(f"[producer] wglDXOpenDeviceNV failed: {ctypes.GetLastError()} "
                               "(GL and D3D11 on different GPUs? try D3D_ADAPTER)")

    def copy(self, dst, src):
        com_call(self.context, ID3D11DeviceContext_CopyResource, None, dst, src)

    def wait_idle(self):
        done = wintypes.BOOL()
        com_call(self.context, ID3D11DeviceContext_End, None, self.query)
        while com_call(self.context, ID3D11DeviceContext_GetData, HRESULT, self.query, ctypes.byref(done),
                       ctypes.c_uint(ctypes.sizeof(done)), ctypes.c_uint(0)) != 0:
            time.sleep(0)


_d3d = None


def init_device(adapter_index=0):
    """Create the D3D11 device on the DXGI adapter Chromium uses (0 unless told otherwise)."""
    global _d3d
    if _d3d is None:
        _d3d = D3DDevice(adapter_index)
    return _d3d


def _attach_texture(render_window, d3d, d3d_texture, width, height):
    """Make the D3D11 texture the color attachment 0 of the window's DisplayFramebuffer. Leaves it locked."""
    render_window.MakeCurrent()
    handle = ctypes.c_uint32()
    opengl32.glGenTextures(1, ctypes.byref(handle))
    obj = d3d.interop.register(d3d.gl_device, d3d_texture, handle.value, GL_TEXTURE_2D, WGL_ACCESS_WRITE_DISCARD_NV)
    if not obj:
        raise RuntimeError(f"[producer] wglDXRegisterObjectNV failed: {ctypes.GetLastError()}")
    objs = (wintypes.HANDLE * 1)(obj)

    tex = vtkTextureObject()
    tex.SetContext(render_window)
    tex.AssignToExistingTexture(handle.value, GL_TEXTURE_2D)
    # Only records the size: an assigned texture has no dimensions, so Resize() never
    # calls glTexImage2D (which would detach the D3D11 storage). The DisplayFramebuffer
    # resizes its attachments on every Start(), and this keeps that a no-op too.
    tex.Resize(width, height)

    display_fbo = render_window.GetDisplayFramebuffer()
    d3d.interop.lock(d3d.gl_device, 1, objs)
    display_fbo.AddColorAttachment(0, tex)
    display_fbo.Bind()
    complete = display_fbo.CheckFrameBufferStatus(GL_FRAMEBUFFER)
    display_fbo.UnBind()
    if not complete:
        raise RuntimeError("[producer] DisplayFramebuffer incomplete with the D3D11 texture")
    return tex, handle, objs


class NTHandleTexturePassport(AbstractTexturePassport):
    """VTK's D3D11 render target and the texture shared with Electron, at one size.

    GL owns the render target (it stays locked) except while a frame is copied out in
    flush_graphics_commands(), so VTK can render into it at any time.
    """

    def __init__(self, render_window, width, height):
        super().__init__(render_window, width, height)
        self.d3d = init_device()
        self.d3d.open_interop(render_window)
        # NVIDIA's interop can't register textures created with SHARED_NTHANDLE (ERROR_OPEN_FAILED),
        # so VTK renders into a private texture that gets copied into the shared one on the GPU.
        self.render_texture = _create_texture(self.d3d.device, width, height)
        self.texture, self.nt_handle = _create_shared_texture(self.d3d.device, width, height)
        self.tex, self.handle, self.objs = _attach_texture(
            render_window, self.d3d, self.render_texture, width, height)
        print(f"[producer] D3D11 texture {width}x{height} BGRA NT handle=0x{self.nt_handle:x}", flush=True)

    def to_json(self):
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
        self.d3d.interop.unlock(self.d3d.gl_device, 1, self.objs)
        self.d3d.interop.unregister(self.d3d.gl_device, self.objs[0])
        opengl32.glDeleteTextures(1, ctypes.byref(self.handle))
        kernel32.CloseHandle(self.nt_handle)
        com_release(self.texture)
        com_release(self.render_texture)

    def flush_graphics_commands(self):
        # Unlocking hands the texture back to D3D11, ordered after VTK's GL commands.
        self.d3d.interop.unlock(self.d3d.gl_device, 1, self.objs)
        self.d3d.copy(self.texture, self.render_texture)
        # The shared texture has no keyed mutex, so wait until the GPU is done before telling Electron.
        self.d3d.wait_idle()
        self.d3d.interop.lock(self.d3d.gl_device, 1, self.objs)
