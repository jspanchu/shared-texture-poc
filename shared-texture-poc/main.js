const { app, BrowserWindow, ipcMain, screen, sharedTexture } = require("electron/main");
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { dupFdFromProcess, closeFd } = require('./pidfd');

let producer = null;
let server = null;
let socket = null;
let localFds = [];

const pythonPath = () => {
  const venv = path.join(__dirname, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : (process.env.PYTHON || 'python3');
};

// Electron uses Wayland when it's available, unless --ozone-platform says otherwise.
const usesX11 = () => {
  const platform = app.commandLine.getSwitchValue('ozone-platform');
  return platform ? platform === 'x11' : !process.env.WAYLAND_DISPLAY;
};

const send = (msg) => {
  if (socket && !socket.destroyed) socket.write(JSON.stringify(msg) + '\n');
};

// Duplicate the producer's dma-buf fds into this process and describe the texture.
const importTextureInfo = (msg) => {
  console.log(`[main] texture ${msg.width}x${msg.height} ${msg.fourcc} (${msg.pixelFormat}) modifier=${msg.modifier}`);
  localFds = msg.planes.map((p) => dupFdFromProcess(msg.pid, p.fd));
  console.log(`[main] producer fds ${msg.planes.map((p) => p.fd)} -> local fds ${localFds}`);
  return {
    pixelFormat: msg.pixelFormat,
    codedSize: { width: msg.width, height: msg.height },
    visibleRect: { x: 0, y: 0, width: msg.width, height: msg.height },
    handle: {
      nativePixmap: {
        planes: msg.planes.map((p, i) => ({ fd: localFds[i], stride: p.stride, offset: p.offset, size: p.size })),
        modifier: msg.modifier,
        supportsZeroCopyWebGpuImport: false,
      },
    },
  };
};

const forwardFrame = async (win, textureInfo, msg) => {
  const frameDone = () => send({ type: 'frame_done', seq: msg.seq });
  let imported = null;
  try {
    // Electron dup()s the plane fds, so localFds can be re-imported every frame.
    imported = sharedTexture.importSharedTexture({
      textureInfo: { ...textureInfo, timestamp: msg.timestamp_us },
      allReferencesReleased: frameDone,
    });
    await sharedTexture.sendSharedTexture({ frame: win.webContents.mainFrame, importedSharedTexture: imported });
  } catch (err) {
    console.error(`[main] frame ${msg.seq}:`, err);
    if (!imported) frameDone();
  } finally {
    imported?.release();
  }
};

const handleConnection = (win, conn) => {
  socket = conn;
  let textureInfo = null;
  let buf = '';
  conn.on('data', (chunk) => {
    buf += chunk.toString();
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const msg = JSON.parse(buf.slice(0, nl));
      buf = buf.slice(nl + 1);
      if (msg.type === 'texture') {
        try {
          textureInfo = importTextureInfo(msg);
        } catch (err) {
          console.error('[main] failed to get producer fds:', err);
          conn.destroy();
        }
      } else if (msg.type === 'frame' && textureInfo) {
        forwardFrame(win, textureInfo, msg);
      }
    }
  });
  conn.on('close', () => console.log('[main] producer disconnected'));
};

const startProducer = (win) => {
  server = net.createServer((conn) => handleConnection(win, conn));
  server.listen(0, '127.0.0.1', () => {
    const { port } = server.address();
    const { scaleFactor } = screen.getDisplayMatching(win.getBounds());
    const [cw, ch] = win.getContentSize();
    const width = Math.round(cw * scaleFactor);
    const height = Math.round(ch * scaleFactor);
    win.webContents.send('size', { width, height, cssWidth: cw, cssHeight: ch });

    let args;
    if (usesX11()) {
      // The X window includes the menu bar, so place the child over the content area only.
      const xid = win.getNativeWindowHandle().readUInt32LE();
      const bounds = win.getBounds();
      const content = win.getContentBounds();
      const x = Math.round((content.x - bounds.x) * scaleFactor);
      const y = Math.round((content.y - bounds.y) * scaleFactor);
      args = [path.join(__dirname, 'x11_producer.py'), String(port), `0x${xid.toString(16)}`,
        String(x), String(y), String(width), String(height)];
    } else {
      args = [path.join(__dirname, 'wayland_producer.py'), String(port), String(width), String(height)];
    }
    console.log(`[main] spawning ${pythonPath()} ${args.join(' ')}`);
    producer = spawn(pythonPath(), args, { stdio: 'inherit' });
    producer.on('exit', (code, signal) => console.log(`[main] producer exited code=${code} signal=${signal}`));
  });
};

const cleanup = () => {
  producer?.kill();
  producer = null;
  socket?.destroy();
  socket = null;
  server?.close();
  server = null;
  localFds.forEach(closeFd);
  localFds = [];
};

const createWindow = () => {
  const win = new BrowserWindow({
    width: 800,
    height: 600,
    useContentSize: true,
    resizable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // The preload needs require('electron').sharedTexture.
      sandbox: false,
    }
  });

  ipcMain.once('receiver-ready', () => startProducer(win));
  ipcMain.on('stats', (_event, s) => console.log(`[renderer] frames=${s.frames} fps=${s.fps} ${s.info} center rgba=${s.center}`));
  win.loadFile("index.html");
};

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  cleanup();
  app.quit();
});

app.on("before-quit", cleanup);
