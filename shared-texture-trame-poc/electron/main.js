const { app, BrowserWindow, ipcMain, sharedTexture } = require("electron/main");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { platform } = require("node:process");

const iosurface = () => require("./dma/iosurface");

// The trame app lives one level up, with its venv.
const APP_DIR = path.join(__dirname, "..");

let producer = null;
let server = null;
let socket = null;
let surfaceRef = null;
let localHandle = null;


const pythonPath = () => {
  const venv = path.join(APP_DIR, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : process.env.PYTHON || "python3";
};

// On Linux, Electron uses Wayland when it's available, unless --ozone-platform=x11.
const texturePassportName = () => {
  if (process.platform === "darwin") return "iosurface";
  if (process.platform === "win32") return "nthandle";
  // --ozone-platform wins over --ozone-platform-hint, whose default is auto.
  const ozone =
    app.commandLine.getSwitchValue("ozone-platform") ||
    app.commandLine.getSwitchValue("ozone-platform-hint") ||
    "auto";
  const wayland =
    ozone === "auto" ? !!process.env.WAYLAND_DISPLAY : ozone === "wayland";
  return wayland ? "wayland-dmabuf" : "x11-dmabuf";
};

const send = (msg) => {
  if (socket && !socket.destroyed) socket.write(JSON.stringify(msg) + "\n");
};

// Open the producer's global IOSurface in this process.
const ioSurfaceHandle = (msg) => {
  const { lookupSurface, describeSurface, surfaceHandle } = iosurface();
  surfaceRef = lookupSurface(msg.ioSurfaceId);
  console.log(
    `[main] texture ${msg.width}x${msg.height} (${msg.pixelFormat}) IOSurface id=${msg.ioSurfaceId} ${describeSurface(surfaceRef)}`,
  );
  return { ioSurface: surfaceHandle(surfaceRef) };
};

const textureHandle = (msg) => {
  if (msg.ioSurfaceId !== undefined) return ioSurfaceHandle(msg);
  throw new Error("unsupported texture handle");
};

// Open the producer's global IOSurface in this process, and describe it for
// sharedTexture.importSharedTexture.
const importTextureInfo = (msg) => {
  return {
    pixelFormat: msg.pixelFormat,
    codedSize: { width: msg.width, height: msg.height },
    visibleRect: { x: 0, y: 0, width: msg.width, height: msg.height },
    handle: textureHandle(msg),
  };
};

const forwardFrame = async (win, textureInfo, msg) => {
  const frameDone = () => send({ type: "frame_done", seq: msg.seq });
  let imported = null;
  try {
    // Electron retains the IOSurface, so it can be re-imported every frame.
    imported = sharedTexture.importSharedTexture({
      textureInfo: { ...textureInfo, timestamp: msg.timestamp_us },
      allReferencesReleased: frameDone,
    });
    await sharedTexture.sendSharedTexture({
      frame: win.webContents.mainFrame,
      importedSharedTexture: imported,
    });
  } catch (err) {
    console.error(`[main] frame ${msg.seq}:`, err);
    if (!imported) frameDone();
  } finally {
    imported?.release();
  }
};

const handleConnection = (win, conn) => {
  socket = conn;
  // Without this, Nagle holds frame_done back until the producer's delayed ACK.
  conn.setNoDelay(true);
  let textureInfo = null;
  let buf = "";
  send({ type: "texture_passport_meta", name: texturePassportName() });
  conn.on("data", (chunk) => {
    buf += chunk.toString();
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const msg = JSON.parse(buf.slice(0, nl));
      buf = buf.slice(nl + 1);
      if (msg.type === "texture") {
        try {
          // A new texture after a resize. Frames still in flight hold their own imports.
          releaseTexture();
          textureInfo = importTextureInfo(msg);
        } catch (err) {
          console.error("[main] failed to get producer texture:", err);
          conn.destroy();
        }
      } else if (msg.type === "frame" && textureInfo) {
        forwardFrame(win, textureInfo, msg);
      }
    }
  });
  conn.on("close", () => console.log("[main] producer disconnected"));
};

// Spawn the trame server, and load its page once it says where it runs. The canvas in that
// page reports its size to trame, so the producer renders at the canvas's size.
const startProducer = (win) => {
  server = net.createServer((conn) => handleConnection(win, conn));
  server.listen(0, "127.0.0.1", () => {
    const { port } = server.address();
    const { electron, chrome, node, v8 } = process.versions;
    const args = [
      path.join(APP_DIR, "app.py"),
      "--server",
      "--port",
      "0",
      "--emp-port",
      String(port),
      "--electron-versions",
      JSON.stringify({ electron, chrome, node, v8 }),
    ];
    console.log(`[main] spawning ${pythonPath()} ${args.join(" ")}`);
    producer = spawn(pythonPath(), args, {
      cwd: APP_DIR,
      stdio: ["ignore", "pipe", "inherit"],
    });
    producer.on("exit", (code, signal) =>
      console.log(`[main] producer exited code=${code} signal=${signal}`),
    );

    let out = "";
    let loaded = false;
    producer.stdout.on("data", (chunk) => {
      process.stdout.write(chunk);
      if (loaded) return;
      out += chunk.toString();
      const match = out.match(/Local:\s+(http:\/\/\S+)/);
      if (match) {
        loaded = true;
        console.log(`[main] loading ${match[1]}`);
        win.loadURL(match[1]);
      }
    });
  });
};

// Close this process's reference to the producer's texture.
const releaseTexture = () => {
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
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      // The preload needs require('electron').sharedTexture.
      sandbox: false,
    },
  });

  ipcMain.on("stats", (_event, s) =>
    console.log(
      `[renderer] frames=${s.frames} fps=${s.fps} ${s.info} center rgba=${s.center}`,
    ),
  );
  startProducer(win);
};

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  cleanup();
  app.quit();
});

app.on("before-quit", cleanup);
