"""
Talks newline-delimited JSON to the Electron main process on 127.0.0.1:<port>.

-> means python to EMP
<- means EMP to Python

    <- {"type":"texture_passport_meta", ...}    at start, name of the texture passport class.
    -> {"type":"texture", ...}                  at start and after every resize, describing the shared texture
    -> {"type":"frame", ...}                    after every render (finished on the GPU)
    <- {"type":"frame_done", ...}               when Electron released the texture

Everything else (size, input, app state) goes from the renderer process to trame over its websocket.
"""

import asyncio
import json
import socket


class LineConnection:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Small request/response messages: don't let Nagle wait for delayed ACKs.
        writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(reader, writer)

    async def send(self, msg):
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

    async def recv(self):
        """Return the next message. Raises EOFError on close."""
        line = await self.reader.readline()
        if not line:
            raise EOFError
        return json.loads(line)

    def close(self):
        self.writer.close()
