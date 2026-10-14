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

# Start

```sh
npm start
```
