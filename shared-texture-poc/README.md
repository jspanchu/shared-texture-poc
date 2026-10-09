# sharedTexture POC

A Python process renders with OpenGL and Electron shows the result without
copying it through the CPU. Tested on Linux with NVIDIA, on macOS 26 with an
Apple M3 Max, on Windows 11 with an NVIDIA RTX 4060, and Electron 44. There are
four producers, and `main.js` picks one based on the platform Electron runs on
(Windows; macOS; on Linux `--ozone-platform`, otherwise Wayland if
`WAYLAND_DISPLAY` is set):

- **Wayland**, `wayland_producer.py`: renders on a headless EGL context and
  exports the texture as a DMA-BUF. Electron imports it with `sharedTexture` and
  draws it into a canvas.
- **X11**, `x11_producer.py`: `sharedTexture` doesn't work on X11 (see
  Findings), so this one renders with GLX into a child X window placed over the
  page's content area.
- **macOS** (Apple Silicon only), `macos_producer.py`: renders on a windowless
  CGL context into a global IOSurface. Electron opens it by ID, imports it with
  `sharedTexture` and draws it into a canvas.
- **Windows** (NVIDIA, or any driver with `WGL_NV_DX_interop2`),
  `windows_producer.py`: renders with WGL into a D3D11 texture and shares it
  through an NT handle. Electron duplicates the handle, imports it with
  `sharedTexture` and draws it into a canvas.

## Setup

```sh
npm install
npm approve-scripts electron koffi   # npm 11 blocks install scripts by default
uv venv .venv && uv pip install -p .venv -r requirements.txt
```

## Run

```sh
npm start
```

The content area should pulse red at about 60 fps (about 50 fps on macOS, see
Findings). To check that real pixels arrive, look at the once-a-second log line:
on Wayland, macOS and Windows the main process logs `[renderer] ... center rgba=...`; on
X11 the producer logs its own fps.

Overrides:

- `RENDER_NODE=/dev/dri/renderD12x` (Wayland only): the GPU the producer
  renders on. By default it uses the GPU driving the display (see "Same GPU"
  below). On X11, GLX always renders on the GPU driving the X screen.
- `D3D_ADAPTER=<n>` (Windows only): the DXGI adapter index the producer
  allocates on. The default, 0, is the adapter Chromium uses too.
- `PYTHON=/path/to/python`: the interpreter for the producer. It is only used
  when `.venv/` doesn't exist.

## How it works: Wayland

1. `wayland_producer.py` opens the EGL device for the render node, creates a GL 4.5
   core context without a surface, allocates an RGBA8 texture with
   `glTexStorage2D` and exports it with `eglExportDMABUFImageMESA`. That gives
   fourcc `AB24`, a modifier, and one fd/stride/offset per plane.
2. The producer connects to the main process over TCP on 127.0.0.1 and sends a
   `texture` message with its pid and the plane fds as numbers in *its own* fd
   table.
3. `main.js` copies those fds into its own process with `pidfd_open` +
   `pidfd_getfd` (`pidfd.js`, through koffi). This works because the producer
   is our child process (`ptrace_scope=1`).
4. For each `frame` message, `main.js` calls `sharedTexture.importSharedTexture`
   with a `nativePixmap` handle and passes the result to the renderer with
   `sendSharedTexture`.
5. `preload.js` gets a `VideoFrame` from the imported texture, draws it with
   `ctx.drawImage`, then closes and releases it.

Protocol (newline-delimited JSON):

| Direction | Message | Meaning |
|---|---|---|
| producer → main | `texture` | Once: pid, size, fourcc, pixelFormat, modifier, planes |
| producer → main | `frame` | A render finished (`glFinish`'d): `seq`, `timestamp_us` |
| main → producer | `frame_done` | Electron released every reference to frame `seq` |

Backpressure: the producer doesn't render into the texture again until it gets
`frame_done` for the previous frame, or until 1 s has passed. Electron sends it
from the import's `allReferencesReleased` callback.

## How it works: X11

1. `main.js` reads the window's XID with `win.getNativeWindowHandle()` and the
   content area's offset inside it: the X window includes Electron's menu bar,
   so the offset is `getContentBounds() - getBounds()`, scaled to device pixels.
   It starts `x11_producer.py <port> <xid> <x> <y> <width> <height>`.
2. The producer creates a child window of that XID with the visual of its GLX
   framebuffer config. It gives the window its own colormap and border pixel,
   since Chromium's visual may differ and `XCreateWindow` would fail with
   `BadMatch`. Then it renders with `glXSwapBuffers`, synced to vsync.
3. Chromium draws the page into its own child window, which covers the whole
   Electron window and starts out above ours. The producer listens for
   `SubstructureNotify` on the parent and re-raises its window whenever a
   sibling is mapped or reconfigured.
4. The child window's input shape is empty (XFixes), so mouse events go through
   to the page underneath.
5. The TCP connection carries no messages. It only ties the two lifetimes
   together: the producer exits when the connection closes or its window is
   destroyed.

Limitations: HTML can't be drawn over the render area, and the geometry is set
once at startup (the window isn't resizable). A resizable version would need
`main.js` to send geometry updates and the producer to call `XMoveResizeWindow`.

## How it works: macOS

1. `macos_producer.py` creates a GL 4.1 core context with CGL (no window; on
   Apple Silicon this is Apple's GL-on-Metal driver), and a BGRA IOSurface with
   `kIOSurfaceIsGlobal` set. It binds the IOSurface to a `GL_TEXTURE_RECTANGLE`
   with `CGLTexImageIOSurface2D` (CGL only binds IOSurfaces to rectangle
   textures) and attaches that to an FBO.
2. The producer sends a `texture` message with `ioSurfaceId` (`IOSurfaceGetID`)
   instead of `fourcc`/`modifier`/`planes`.
3. `main.js` opens the surface in its own process with `IOSurfaceLookup(id)`
   (`iosurface.js`, through koffi) and keeps that reference for the session.
4. For each `frame` message, `main.js` calls `sharedTexture.importSharedTexture`
   with handle `{ ioSurface }`: a Buffer holding the `IOSurfaceRef` pointer.
   The rest (`sendSharedTexture`, `frame_done`, `preload.js`) is shared with
   Wayland.

IOSurfaces have no implicit fences, so the producer `glFinish`es before each
`frame` message, and the `frame_done` backpressure keeps it from drawing into
the surface while Electron reads it.

Unlike X11, HTML draws over the render area (the stats overlay is visible), and
`ioclasscount IOSurface` stays flat while the app runs, so no surface leaks per
frame.

## How it works: Windows

1. `windows_producer.py` creates a legacy WGL context on a hidden window, and a
   D3D11 device on DXGI adapter 0 (`D3D_ADAPTER`). Both must be on the same
   GPU, or `wglDXOpenDeviceNV` fails.
2. It creates two BGRA D3D11 textures: a private one that GL renders into
   through `WGL_NV_DX_interop2` (attached to an FBO), and a shared one with
   `D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE`, with an
   NT handle from `IDXGIResource1::CreateSharedHandle`. The COM calls go
   through ctypes with vtable indices taken from the Windows SDK headers.
3. The producer sends a `texture` message with its pid and `ntHandle`, the
   handle's value in *its own* handle table.
4. `main.js` copies the handle into its own process with `OpenProcess` +
   `DuplicateHandle` (`winhandle.js`, through koffi), keeps it for the session
   and passes it as handle `{ ntHandle }`: a Buffer holding the HANDLE value.
   The rest (`sendSharedTexture`, `frame_done`, `preload.js`) is shared with
   Wayland and macOS.
5. Each frame: lock the interop object, `glClear`, unlock, `CopyResource` into
   the shared texture, then wait on a D3D11 event query before sending `frame`.
   Electron's `bgra` textures have no keyed mutex, so this wait plus the
   `frame_done` backpressure is all the synchronization there is.

As on macOS, HTML draws over the render area, and the handle counts of the
Electron and Python processes stay flat while the app runs.

## Findings

- **`sharedTexture` does not work on X11**, and the producer can't fix it. NVIDIA's EGL on X11
  supports dma-buf import, but Electron only offers ANGLE, and none of ANGLE's
  backends work here:
  - default (ANGLE on OpenGL through GLX): GLX can't import dma-bufs, so
    `Could not find SharedImageBackingFactory ... gmb_type: platform`.
  - `--use-angle=gl-egl`: the GPU process fails `eglCreateWindowSurface`
    (`EGL_BAD_MATCH`) and the factory error remains.
  - `--use-angle=vulkan --enable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE`:
    the GPU process does take the dma-buf, but the canvas stays
    `0,0,0,0`, and the GPU process leaks one dma-buf fd per frame. This is the
    same with the producer on either GPU and with compressed, uncompressed
    (`0x…606014`) or LINEAR buffers, so it's a Chromium-side problem.
  - `--use-gl=egl` (native EGL): not allowed in this build, so the GPU process
    exits.
- **Wayland works**, including NVIDIA's default compressed block-linear
  modifier (`0x0300000000e08014`). The pixels in the `VideoFrame` match what
  Python rendered. You don't need to allocate an uncompressed buffer through
  GBM.
- **Same GPU.** The producer must allocate on the GPU Chromium composites on.
  On a machine with two GPUs, a buffer from the other GPU fails with
  `CreateSharedImage: could not create backing`. The producer picks the card
  whose `/sys/class/drm/card*/device/boot_vga` is `1` (the GPU the firmware
  used for the boot display) and uses that card's `renderD*` node. If no card
  matches, it falls back to `renderD128`. This is a heuristic: if your
  compositor runs on a different GPU than the boot GPU, set `RENDER_NODE`.
- **fd ownership.** Electron `dup()`s the plane fds on every import, so
  `main.js` keeps one copy for the whole session, imports from it each frame,
  and closes it on shutdown.
- **macOS works with a global IOSurface.** Electron's `ioSurface` handle is a
  pointer that is only valid in the main process, so the surface must be
  opened there. `IOSurfaceLookup(id)` still works on macOS 26 for surfaces
  created with `kIOSurfaceIsGlobal`, but that flag is deprecated and blocked
  under the App Sandbox. A shipped app would send a Mach port from
  `IOSurfaceCreateMachPort` instead, which needs a native helper since neither
  Node nor Python can send Mach ports.
- **macOS runs at about 50 fps, not 60.** Not investigated yet. The likely
  cause is the single surface: each frame waits for the `frame_done` round
  trip before the next render starts. Rendering into two IOSurfaces in turn
  would remove that wait.
- **Windows works, with one GPU copy per frame.** NVIDIA's
  `wglDXRegisterObjectNV` fails with `ERROR_OPEN_FAILED` (`0xC007006E`) on a
  texture created with `SHARED_NTHANDLE`. It works on plain and legacy-shared
  (`MISC_SHARED` only) textures. Electron only accepts NT handles, so GL
  renders into a private texture that is copied into the shared one with
  `CopyResource`. This costs one GPU copy, which a
  `D3D11_RESOURCE_MISC_SHARED_NTHANDLE` texture created and rendered by D3D
  itself (or by ANGLE, or Vulkan with external memory) wouldn't need. Electron
  duplicates the NT handle on every import, so as with dma-buf fds `main.js`
  keeps one copy for the session.
- **Windows: same GPU.** As on Linux, the shared texture must be on the GPU
  Chromium uses (adapter 0 unless Windows' per-app GPU preference says
  otherwise), and the WGL context must be on that GPU too. On hybrid laptops
  where `python.exe` and `electron.exe` get different GPUs, set the per-app
  preference in Windows Settings > Display > Graphics, or `D3D_ADAPTER`.
