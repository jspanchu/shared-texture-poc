# Install

```sh
uv venv -p 3.13
# Activate venv
# Windows: .venv\Scripts\activate
# Linux: source .venv/bin/activate
# macOS: source .venv/bin/activate
uv pip install -r ./requirements.txt

npm i
```

If you're on linux and using wayland, please do `sudo sysctl -w kernel.yama.ptrace_scope=1`.

# Start

```sh
npm start
```
