"""Render with WGL into a D3D11 texture (WGL_NV_DX_interop2) and share it with Electron.

Usage: windows_producer.py <port> <width> <height>

Talks newline-delimited JSON to the Electron main process on 127.0.0.1:<port>:
  -> {"type":"texture", ...}   once, with the texture's NT handle in this process
  -> {"type":"frame", ...}     after every render (finished on the GPU)
  <- {"type":"frame_done", ...} when Electron released the texture

Environment:
  D3D_ADAPTER=<index>  DXGI adapter to allocate on (default 0, the one Chromium uses)
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
from ctypes import wintypes

# PyOpenGL's plugin loader warns that numpy is missing; it isn't needed here.
logging.getLogger("OpenGL.plugins").setLevel(logging.ERROR)

from OpenGL import GL  # noqa: E402

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


# --- WGL -------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
opengl32 = ctypes.WinDLL("opengl32")
kernel32 = ctypes.WinDLL("kernel32")

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DestroyWindow.argtypes = [wintypes.HWND]
gdi32.ChoosePixelFormat.argtypes = [wintypes.HDC, vp]
gdi32.SetPixelFormat.argtypes = [wintypes.HDC, ctypes.c_int, vp]
opengl32.wglCreateContext.restype = vp
opengl32.wglCreateContext.argtypes = [wintypes.HDC]
opengl32.wglMakeCurrent.argtypes = [wintypes.HDC, vp]
opengl32.wglDeleteContext.argtypes = [vp]
opengl32.wglGetProcAddress.restype = vp
opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class PIXELFORMATDESCRIPTOR(ctypes.Structure):
    _fields_ = [("nSize", wintypes.WORD), ("nVersion", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("iPixelType", wintypes.BYTE), ("cColorBits", wintypes.BYTE)] + \
               [(n, wintypes.BYTE) for n in ("cRedBits", "cRedShift", "cGreenBits", "cGreenShift", "cBlueBits",
                                             "cBlueShift", "cAlphaBits", "cAlphaShift", "cAccumBits",
                                             "cAccumRedBits", "cAccumGreenBits", "cAccumBlueBits",
                                             "cAccumAlphaBits", "cDepthBits", "cStencilBits", "cAuxBuffers",
                                             "iLayerType", "bReserved")] + \
               [("dwLayerMask", wintypes.DWORD), ("dwVisibleMask", wintypes.DWORD), ("dwDamageMask", wintypes.DWORD)]


PFD_DOUBLEBUFFER = 0x1
PFD_DRAW_TO_WINDOW = 0x4
PFD_SUPPORT_OPENGL = 0x20
WS_POPUP = 0x80000000

WGL_ACCESS_WRITE_DISCARD_NV = 0x0002


def create_context():
    """WGL needs a window DC even when rendering only to FBOs; this one is never shown."""
    hwnd = user32.CreateWindowExW(0, "STATIC", "producer", WS_POPUP, 0, 0, 1, 1, None, None, None, None)
    if not hwnd:
        sys.exit(f"[producer] CreateWindowExW failed: {ctypes.GetLastError()}")
    hdc = user32.GetDC(hwnd)
    pfd = PIXELFORMATDESCRIPTOR(nSize=ctypes.sizeof(PIXELFORMATDESCRIPTOR), nVersion=1,
                                dwFlags=PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER, cColorBits=32)
    fmt = gdi32.ChoosePixelFormat(hdc, ctypes.byref(pfd))
    if not fmt or not gdi32.SetPixelFormat(hdc, fmt, ctypes.byref(pfd)):
        sys.exit("[producer] no OpenGL pixel format")
    ctx = opengl32.wglCreateContext(hdc)
    if not ctx or not opengl32.wglMakeCurrent(hdc, ctx):
        sys.exit("[producer] wglCreateContext failed")
    print(f"[producer] GL {GL.glGetString(GL.GL_VERSION).decode()} / {GL.glGetString(GL.GL_RENDERER).decode()}", flush=True)
    return hwnd, hdc, ctx


def wgl_func(name, restype, *argtypes):
    addr = opengl32.wglGetProcAddress(name.encode())
    if not addr:
        sys.exit(f"[producer] {name} missing: the GL driver doesn't support WGL_NV_DX_interop2")
    return ctypes.WINFUNCTYPE(restype, *argtypes)(addr)


class DXInterop:
    def __init__(self):
        H = wintypes.HANDLE
        self.open_device = wgl_func("wglDXOpenDeviceNV", H, vp)
        self.close_device = wgl_func("wglDXCloseDeviceNV", wintypes.BOOL, H)
        self.register = wgl_func("wglDXRegisterObjectNV", H, H, vp, GL.GLuint, GL.GLenum, GL.GLenum)
        self.unregister = wgl_func("wglDXUnregisterObjectNV", wintypes.BOOL, H, H)
        self.lock = wgl_func("wglDXLockObjectsNV", wintypes.BOOL, H, GL.GLint, ctypes.POINTER(H))
        self.unlock = wgl_func("wglDXUnlockObjectsNV", wintypes.BOOL, H, GL.GLint, ctypes.POINTER(H))


def create_render_target(interop, gl_device, texture, width, height):
    tex = GL.glGenTextures(1)
    obj = interop.register(gl_device, texture, tex, GL.GL_TEXTURE_2D, WGL_ACCESS_WRITE_DISCARD_NV)
    if not obj:
        sys.exit(f"[producer] wglDXRegisterObjectNV failed: {ctypes.GetLastError()}")
    objs = (wintypes.HANDLE * 1)(obj)
    fbo = GL.glGenFramebuffers(1)
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
    interop.lock(gl_device, 1, objs)
    GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
    status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
    interop.unlock(gl_device, 1, objs)
    if status != GL.GL_FRAMEBUFFER_COMPLETE:
        sys.exit(f"[producer] FBO incomplete: 0x{int(status):04x}")
    GL.glViewport(0, 0, width, height)
    return tex, objs, fbo


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
        sys.exit("usage: windows_producer.py <port> <width> <height>")
    port, width, height = (int(a) for a in sys.argv[1:])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    hwnd, hdc, ctx = create_context()
    device, d3d_ctx = create_device()
    # NVIDIA's interop can't register textures created with SHARED_NTHANDLE (ERROR_OPEN_FAILED),
    # so GL renders into a private texture that gets copied into the shared one on the GPU.
    render_texture = create_texture(device, width, height)
    texture, nt_handle = create_shared_texture(device, width, height)

    interop = DXInterop()
    gl_device = interop.open_device(device)
    if not gl_device:
        sys.exit(f"[producer] wglDXOpenDeviceNV failed: {ctypes.GetLastError()} "
                 "(GL and D3D11 on different GPUs? try D3D_ADAPTER)")
    tex, objs, fbo = create_render_target(interop, gl_device, render_texture, width, height)

    # An event query tells us when the D3D11 device (and so the texture) is idle.
    query_desc = D3D11_QUERY_DESC(Query=D3D11_QUERY_EVENT)
    query = vp()
    check_hr(com_call(device, ID3D11Device_CreateQuery, HRESULT, ctypes.byref(query_desc), ctypes.byref(query)),
             "CreateQuery")
    done = wintypes.BOOL()

    print(f"[producer] D3D11 texture {width}x{height} BGRA NT handle=0x{nt_handle:x}", flush=True)

    conn = LineSocket(port)
    try:
        conn.send({
            "type": "texture",
            "pid": os.getpid(),
            "width": width,
            "height": height,
            "pixelFormat": "bgra",
            "ntHandle": nt_handle,
        })
        seq = 0
        frame_interval = 1 / 60
        while True:
            start = time.monotonic()
            interop.lock(gl_device, 1, objs)
            GL.glClearColor(0.5 + 0.5 * math.sin(time.time()), 0.0, 0.0, 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            interop.unlock(gl_device, 1, objs)
            com_call(d3d_ctx, ID3D11DeviceContext_CopyResource, None, texture, render_texture)
            # The shared texture has no keyed mutex, so wait until the GPU is done before telling Electron.
            com_call(d3d_ctx, ID3D11DeviceContext_End, None, query)
            while com_call(d3d_ctx, ID3D11DeviceContext_GetData, HRESULT, query, ctypes.byref(done),
                           ctypes.c_uint(ctypes.sizeof(done)), ctypes.c_uint(0)) != 0:
                time.sleep(0)
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
        interop.unregister(gl_device, objs[0])
        GL.glDeleteTextures(1, [tex])
        interop.close_device(gl_device)
        release(query)
        kernel32.CloseHandle(nt_handle)
        release(texture)
        release(render_texture)
        release(d3d_ctx)
        release(device)
        opengl32.wglMakeCurrent(None, None)
        opengl32.wglDeleteContext(ctx)
        user32.ReleaseDC(hwnd, hdc)
        user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    main()
