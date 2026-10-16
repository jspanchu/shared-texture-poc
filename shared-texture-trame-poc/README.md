# Install

```sh
uv sync
# Activate venv
# Windows: .venv\Scripts\activate
# Linux: source .venv/bin/activate
# macOS: source .venv/bin/activate
cd electron
npm i
```

If you're on linux and using wayland, please do `sudo sysctl -w kernel.yama.ptrace_scope=1`.

On Windows, the GL driver needs `WGL_NV_DX_interop2`, and VTK must render on the GPU Chromium uses.
Set `D3D_ADAPTER=<index>` if that isn't DXGI adapter 0.

# Start

```sh
npm start
```
