// see electron/preload.js, that's the one which defines window.ElectronSharedTextureView
(function () {
  const ElectronSharedTextureView = {
    emits: ['stats'],
    mounted() {
      if (!window.ElectronSharedTextureView) {
        console.warn('shared-texture-view: no ElectronSharedTextureView bridge, not running in Electron?');
        return;
      }
      // { fps, frames, info } once a second.
      window.ElectronSharedTextureView.attach(this.$refs.canvas, (stats) => this.$emit('stats', stats));
    },
    beforeUnmount() {
      window.ElectronSharedTextureView?.detach(this.$refs.canvas);
    },
    template: `
      <canvas ref="canvas" tabindex="0" @contextmenu.prevent
        style="position:absolute;top:0;left:0;width:100%;height:100%;display:block;outline:none"></canvas>
    `,
  };

  window.trame_shared_texture = {
    install(app) {
      app.component('electron-shared-texture-view', ElectronSharedTextureView);
    },
  };
})();
