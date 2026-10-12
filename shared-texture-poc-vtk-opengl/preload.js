const { ipcRenderer, sharedTexture } = require('electron');

// Mouse buttons by MouseEvent.button, named like VTK's events.
const BUTTONS = ['Left', 'Middle', 'Right'];

window.addEventListener('DOMContentLoaded', () => {
  const canvas = document.getElementById('view');
  const ctx = canvas.getContext('2d');
  const stats = document.getElementById('stats');

  let frames = 0;
  let windowStart = performance.now();
  let windowFrames = 0;
  let fps = 0;

  sharedTexture.setSharedTextureReceiver(async ({ importedSharedTexture }) => {
    const vf = importedSharedTexture.getVideoFrame();
    // The canvas fills the window; its pixels follow the producer's texture, which follows resizes.
    if (canvas.width !== vf.displayWidth || canvas.height !== vf.displayHeight) {
      canvas.width = vf.displayWidth;
      canvas.height = vf.displayHeight;
    }
    ctx.drawImage(vf, 0, 0, canvas.width, canvas.height);
    const info = `${vf.codedWidth}x${vf.codedHeight} ${vf.format}`;
    vf.close();
    importedSharedTexture.release();

    frames++;
    windowFrames++;
    const now = performance.now();
    if (now - windowStart >= 1000) {
      fps = (windowFrames * 1000) / (now - windowStart);
      windowStart = now;
      windowFrames = 0;
      const center = [...ctx.getImageData(canvas.width >> 1, canvas.height >> 1, 1, 1).data];
      ipcRenderer.send('stats', { frames, fps: fps.toFixed(1), info, center });
    }
    stats.textContent = `frame ${frames} | ${fps.toFixed(1)} fps | ${info}`;
  });

  // Forward input to the producer's interactor, in the texture's pixels.
  const sendInput = (event, e, extra) => ipcRenderer.send('input', {
    event, ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey, ...extra,
  });
  const position = (e) => ({
    x: (e.offsetX * canvas.width) / canvas.clientWidth,
    y: (e.offsetY * canvas.height) / canvas.clientHeight,
  });

  // Keep getting mouse events while dragging outside the canvas.
  canvas.addEventListener('pointerdown', (e) => canvas.setPointerCapture(e.pointerId));
  canvas.addEventListener('mousedown', (e) => {
    if (BUTTONS[e.button]) sendInput(`${BUTTONS[e.button]}ButtonPress`, e, { ...position(e), repeat: e.detail > 1 ? 1 : 0 });
  });
  canvas.addEventListener('mouseup', (e) => {
    if (BUTTONS[e.button]) sendInput(`${BUTTONS[e.button]}ButtonRelease`, e, position(e));
  });
  canvas.addEventListener('mousemove', (e) => sendInput('MouseMove', e, position(e)));
  canvas.addEventListener('mouseenter', (e) => sendInput('Enter', e, position(e)));
  canvas.addEventListener('mouseleave', (e) => sendInput('Leave', e, position(e)));
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.deltaY !== 0) sendInput(e.deltaY < 0 ? 'MouseWheelForward' : 'MouseWheelBackward', e, position(e));
  }, { passive: false });
  // Right-drag zooms.
  canvas.addEventListener('contextmenu', (e) => e.preventDefault());
  canvas.addEventListener('keydown', (e) => sendInput('KeyPress', e, { key: e.key, code: e.code, repeat: e.repeat ? 1 : 0 }));
  canvas.addEventListener('keyup', (e) => sendInput('KeyRelease', e, { key: e.key, code: e.code }));
  canvas.focus();

  ipcRenderer.send('receiver-ready');
});
