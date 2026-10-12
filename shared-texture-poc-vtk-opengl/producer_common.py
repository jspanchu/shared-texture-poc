"""What the sharedTexture producers have in common: the connection to Electron, and
driving VTK from the input events of the canvas the frames are drawn on.

Talks newline-delimited JSON to the Electron main process on 127.0.0.1:<port>:
  -> {"type":"texture", ...}    at start and after every resize, describing the shared texture
  -> {"type":"frame", ...}      after every render (finished on the GPU)
  <- {"type":"frame_done", ...} when Electron released the texture
  <- {"type":"event", ...}      a mouse, wheel or keyboard event on the canvas, in device pixels
  <- {"type":"resize", ...}     the canvas's new size in device pixels

Frames are rendered on demand: when the interactor style asks for one, and after a resize.
"""

import json
import socket
import time

from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkRenderingUI import vtkGenericRenderWindowInteractor


class LineSocket:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.buf = b""

    def send(self, msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def recv(self, timeout):
        """Return the next message, None on timeout. None blocks, 0 polls. Raises EOFError on close."""
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, BlockingIOError):
                return None
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)


# The canvas names its events after the vtkCommand events, minus the "Event" suffix.
MOUSE_EVENTS = {
    "MouseMove", "LeftButtonPress", "LeftButtonRelease", "MiddleButtonPress", "MiddleButtonRelease",
    "RightButtonPress", "RightButtonRelease", "MouseWheelForward", "MouseWheelBackward", "Enter", "Leave",
}

# DOM KeyboardEvent.key -> the X11 keysym names VTK's interactors report.
NAMED_KEYS = {
    "Enter": "Return", "Escape": "Escape", "Backspace": "BackSpace", "Tab": "Tab", "Delete": "Delete",
    "Insert": "Insert", "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "ArrowLeft": "Left", "ArrowUp": "Up", "ArrowRight": "Right", "ArrowDown": "Down",
    "CapsLock": "Caps_Lock", "NumLock": "Num_Lock",
}
# The side of a modifier only shows in KeyboardEvent.code.
MODIFIER_KEYS = {
    "ShiftLeft": "Shift_L", "ShiftRight": "Shift_R", "ControlLeft": "Control_L", "ControlRight": "Control_R",
    "AltLeft": "Alt_L", "AltRight": "Alt_R", "MetaLeft": "Super_L", "MetaRight": "Super_R",
}
PUNCTUATION = {
    " ": "space", "!": "exclam", '"': "quotedbl", "#": "numbersign", "$": "dollar", "%": "percent",
    "&": "ampersand", "'": "apostrophe", "(": "parenleft", ")": "parenright", "*": "asterisk", "+": "plus",
    ",": "comma", "-": "minus", ".": "period", "/": "slash", ":": "colon", ";": "semicolon", "<": "less",
    "=": "equal", ">": "greater", "?": "question", "@": "at", "[": "bracketleft", "\\": "backslash",
    "]": "bracketright", "^": "asciicircum", "_": "underscore", "`": "grave", "{": "braceleft", "|": "bar",
    "}": "braceright", "~": "asciitilde",
}
CONTROL_CHARS = {"Return": "\r", "Escape": "\x1b", "BackSpace": "\b", "Tab": "\t", "Delete": "\x7f"}


def translate_key(key, code):
    """DOM key and code -> VTK's (keycode, keysym)."""
    if code in MODIFIER_KEYS:
        return "\0", MODIFIER_KEYS[code]
    if len(key) == 1:
        return (key if key.isascii() else "\0"), PUNCTUATION.get(key, key)
    keysym = NAMED_KEYS.get(key, key)
    return CONTROL_CHARS.get(keysym, "\0"), keysym


def dispatch(interactor, event):
    """Replay a canvas event on the interactor, the way VTK's native interactors report them."""
    name = event["event"]
    ctrl, shift = int(event["ctrl"]), int(event["shift"])
    if name in MOUSE_EVENTS:
        interactor.SetEventInformationFlipY(round(event["x"]), round(event["y"]), ctrl, shift, "\0",
                                            event.get("repeat", 0))
    elif name in ("KeyPress", "KeyRelease"):
        keycode, keysym = translate_key(event["key"], event["code"])
        interactor.SetKeyEventInformation(ctrl, shift, keycode, event.get("repeat", 0), keysym)
    else:
        print(f"[producer] ignoring unknown event {name}", flush=True)
        return
    interactor.SetAltKey(int(event["alt"]))
    interactor.InvokeEvent(f"{name}Event")
    if name == "KeyPress":
        interactor.InvokeEvent("CharEvent")


def serve(conn, render_window, render, resize):
    """Render on demand and replay the canvas's input on the scene until Electron disconnects.

    render() renders a frame into the shared texture and returns once the GPU finished it.
    resize(width, height) reallocates the shared texture after the window was resized,
    and returns its "texture" message.
    """
    interactor = vtkGenericRenderWindowInteractor(render_window=render_window,
                                                  interactor_style=vtkInteractorStyleTrackballCamera())
    # The style asks for renders through the interactor. render() stays the only place that
    # renders: the shared texture may need to be locked around it.
    interactor.EnableRenderOff()
    interactor.Initialize()
    dirty = True
    pending_size = None

    def request_render(*_):
        nonlocal dirty
        dirty = True

    def handle(msg):
        nonlocal pending_size
        if msg["type"] == "event":
            dispatch(interactor, msg)
        elif msg["type"] == "resize":
            pending_size = (msg["width"], msg["height"])

    interactor.AddObserver("RenderEvent", request_render)
    seq = 0
    while True:
        # Block until there's something to do, then take everything queued so a burst of input renders once.
        msg = conn.recv(0 if dirty or pending_size else None)
        while msg is not None:
            handle(msg)
            msg = conn.recv(0)
        if pending_size:
            if pending_size != tuple(render_window.GetSize()):
                interactor.UpdateSize(*pending_size)
                conn.send(resize(*pending_size))
                dirty = True
            pending_size = None
        if not dirty:
            continue
        dirty = False
        render()
        conn.send({"type": "frame", "seq": seq, "timestamp_us": int(time.time() * 1e6)})
        # Backpressure: don't touch the texture until Electron is done reading it. Input keeps
        # moving the camera meanwhile; a resize waits, since it frees the texture.
        deadline = time.monotonic() + 1.0
        while True:
            msg = conn.recv(max(0.0, deadline - time.monotonic()))
            if msg is None:
                print(f"[producer] frame {seq}: no frame_done within 1s, continuing", flush=True)
                break
            if msg["type"] == "frame_done":
                if msg["seq"] == seq:
                    break
            else:
                handle(msg)
        seq += 1
