const { ipcRenderer, sharedTexture } = require('electron');

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

  ipcRenderer.on('size', (_event, { width, height, cssWidth, cssHeight }) => {
    canvas.width = width;
    canvas.height = height;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;
  });

  ipcRenderer.send('receiver-ready');
});
