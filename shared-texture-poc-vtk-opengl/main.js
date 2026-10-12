const { app, BrowserWindow, ipcMain, screen, sharedTexture } = require("electron/main");
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

// Loaded on demand: pidfd.js needs Linux's libc, iosurface.js needs macOS frameworks,
// winhandle.js needs kernel32.
const pidfd = () => require('./dma/pidfd');
const iosurface = () => require('./dma/iosurface');
const winhandle = () => require('./dma/winhandle');

let producer = null;
let server = null;
let socket = null;
let localFds = [];
let surfaceRef = null;
let localHandle = null;

const RESIZE_DEBOUNCE_MS = 150;

const pythonPath = () => {
  const venv = process.platform === 'win32'
    ? path.join(__dirname, '.venv', 'Scripts', 'python.exe')
    : path.join(__dirname, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : (process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'));
};

// On Linux, Electron uses Wayland when it's available, unless --ozone-platform says otherwise.
const platform = () => {
  if (process.platform === 'darwin') return 'mac';
  if (process.platform === 'win32') return 'win';
  const ozone = app.commandLine.getSwitchValue('ozone-platform');
  return (ozone ? ozone === 'x11' : !process.env.WAYLAND_DISPLAY) ? 'x11' : 'wayland';
};

const send = (msg) => {
  if (socket && !socket.destroyed) socket.write(JSON.stringify(msg) + '\n');
};

// Duplicate the producer's dma-buf fds into this process.
const nativePixmapHandle = (msg) => {
  console.log(`[main] texture ${msg.width}x${msg.height} ${msg.fourcc} (${msg.pixelFormat}) modifier=${msg.modifier}`);
  localFds = msg.planes.map((p) => pidfd().dupFdFromProcess(msg.pid, p.fd));
  console.log(`[main] producer fds ${msg.planes.map((p) => p.fd)} -> local fds ${localFds}`);
  return {
    nativePixmap: {
      planes: msg.planes.map((p, i) => ({ fd: localFds[i], stride: p.stride, offset: p.offset, size: p.size })),
      modifier: msg.modifier,
      supportsZeroCopyWebGpuImport: false,
    },
  };
};

// Open the producer's global IOSurface in this process.
const ioSurfaceHandle = (msg) => {
  const { lookupSurface, describeSurface, surfaceHandle } = iosurface();
  surfaceRef = lookupSurface(msg.ioSurfaceId);
  console.log(`[main] texture ${msg.width}x${msg.height} (${msg.pixelFormat}) IOSurface id=${msg.ioSurfaceId} ${describeSurface(surfaceRef)}`);
  return { ioSurface: surfaceHandle(surfaceRef) };
};

// Duplicate the producer's D3D11 shared texture handle into this process.
const ntHandle = (msg) => {
  const { dupHandleFromProcess, handleBuffer } = winhandle();
  localHandle = dupHandleFromProcess(msg.pid, msg.ntHandle);
  console.log(`[main] texture ${msg.width}x${msg.height} (${msg.pixelFormat}) producer handle 0x${msg.ntHandle.toString(16)} -> local handle 0x${localHandle.toString(16)}`);
  return { ntHandle: handleBuffer(localHandle) };
};

const textureHandle = (msg) => {
  if (msg.ioSurfaceId !== undefined) return ioSurfaceHandle(msg);
  if (msg.ntHandle !== undefined) return ntHandle(msg);
  return nativePixmapHandle(msg);
};

// Describe the producer's texture for sharedTexture.importSharedTexture.
const importTextureInfo = (msg) => ({
  pixelFormat: msg.pixelFormat,
  codedSize: { width: msg.width, height: msg.height },
  visibleRect: { x: 0, y: 0, width: msg.width, height: msg.height },
  handle: textureHandle(msg),
});

const forwardFrame = async (win, textureInfo, msg) => {
  const frameDone = () => send({ type: 'frame_done', seq: msg.seq });
  let imported = null;
  try {
    // Electron dup()s the plane fds / NT handle and retains the IOSurface, so they can be re-imported every frame.
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
  // Without this, Nagle holds frame_done back behind unacknowledged input events until
  // the producer's delayed ACK (40ms on Linux), capping interaction at ~20fps.
  conn.setNoDelay(true);
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
          // A new texture after a resize. Frames still in flight hold their own imports.
          releaseTexture();
          textureInfo = importTextureInfo(msg);
        } catch (err) {
          console.error('[main] failed to get producer texture:', err);
          conn.destroy();
        }
      } else if (msg.type === 'frame' && textureInfo) {
        forwardFrame(win, textureInfo, msg);
      }
    }
  });
  conn.on('close', () => console.log('[main] producer disconnected'));
};

// The content area in device pixels, the size the producer renders at.
const deviceSize = (win) => {
  const { scaleFactor } = screen.getDisplayMatching(win.getBounds());
  const [cw, ch] = win.getContentSize();
  return { width: Math.round(cw * scaleFactor), height: Math.round(ch * scaleFactor), scaleFactor };
};

// Tell the producer about new sizes, including scale factor changes, once the window settles:
// each one reallocates the shared texture.
const watchSize = (win, initial) => {
  let last = initial;
  let timer = null;
  const update = () => {
    timer = null;
    if (win.isDestroyed()) return;
    const { width, height } = deviceSize(win);
    if (width < 1 || height < 1 || (width === last.width && height === last.height)) return;
    last = { width, height };
    console.log(`[main] resize ${width}x${height}`);
    send({ type: 'resize', width, height });
  };
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(update, RESIZE_DEBOUNCE_MS);
  };
  win.on('resize', schedule);
  win.on('move', schedule);
  screen.on('display-metrics-changed', schedule);
  win.on('closed', () => {
    clearTimeout(timer);
    screen.off('display-metrics-changed', schedule);
  });
};

const startProducer = (win) => {
  server = net.createServer((conn) => handleConnection(win, conn));
  server.listen(0, '127.0.0.1', () => {
    const { port } = server.address();
    const { width, height, scaleFactor } = deviceSize(win);

    let args;
    if (platform() === 'win') {
      args = [path.join(__dirname, 'windows_vtk_frame_producer.py'), String(port), String(width), String(height)];
    } else if (platform() === 'mac') {
      args = [path.join(__dirname, 'macos_vtk_frame_producer.py'), String(port), String(width), String(height)];
    } else if (platform() === 'x11') {
      // The X window includes the menu bar, so place the child over the content area only.
      const xid = win.getNativeWindowHandle().readUInt32LE();
      const bounds = win.getBounds();
      const content = win.getContentBounds();
      const x = Math.round((content.x - bounds.x) * scaleFactor);
      const y = Math.round((content.y - bounds.y) * scaleFactor);
      args = [path.join(__dirname, 'x11_vtk_frame_producer.py'), String(port), `0x${xid.toString(16)}`,
        String(x), String(y), String(width), String(height)];
    } else {
      args = [path.join(__dirname, 'wayland_vtk_frame_producer.py'), String(port), String(width), String(height)];
    }
    console.log(`[main] spawning ${pythonPath()} ${args.join(' ')}`);
    producer = spawn(pythonPath(), args, { stdio: 'inherit' });
    producer.on('exit', (code, signal) => console.log(`[main] producer exited code=${code} signal=${signal}`));
    if (platform() !== 'x11') watchSize(win, { width, height });
  });
};

// Close this process's references to the producer's texture.
const releaseTexture = () => {
  localFds.forEach((fd) => pidfd().closeFd(fd));
  localFds = [];
  if (surfaceRef) iosurface().releaseSurface(surfaceRef);
  surfaceRef = null;
  if (localHandle) winhandle().closeHandle(localHandle);
  localHandle = null;
};

const cleanup = () => {
  producer?.kill();
  producer = null;
  socket?.destroy();
  socket = null;
  server?.close();
  server = null;
  releaseTexture();
};

const createWindow = () => {
  const win = new BrowserWindow({
    width: 800,
    height: 600,
    useContentSize: true,
    // The X11 producer's child window doesn't follow resizes yet.
    resizable: platform() !== 'x11',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // The preload needs require('electron').sharedTexture.
      sandbox: false,
    }
  });

  ipcMain.once('receiver-ready', () => startProducer(win));
  ipcMain.on('input', (_event, input) => send({ type: 'event', ...input }));
  ipcMain.on('stats', (_event, s) => console.log(`[renderer] frames=${s.frames} fps=${s.fps} ${s.info} center rgba=${s.center}`));
  win.loadFile("index.html");
};

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  cleanup();
  app.quit();
});

app.on("before-quit", cleanup);
