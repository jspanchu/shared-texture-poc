sharedTexture.importSharedTexture(options) imports a shared texture from the given options into the electron app main process.

sharedTexture.setSharedTextureReceiver(callback) sets a callback on the electron renderer process (ERP) to receive the imported shared textures from the EMP (EMP).

sharedTexture.sendSharedTexture(options, ...args) sends the imported shared texture to an ERP. Requires that receiver is registered at ERP before calling this method.

Proof-of-concept app to understand how it all works, on Linux only.

1. Create a python script which draws into an OpenGL framebuffer (through EGL context). Fill the fbo with color=(sin(time.time()), 0.0, 0.0)
   - Create using glTexStorage2D rather than glTexImage2D (maybe needed to successfully map back to system DMA-BUF)
   - Render to texture by attaching a standard RGBA texture to the FBO and clear color
   - Create an EGLImage from the FBO's texture using `eglCreateImageKHR`
   - Export the EGLImage to a DMA-BUF file descriptors `eglExportDMABUFImageQueryMESA` and `eglExportDMABUFImageMESA`
   - Clean up EGLImage.
   - Call glFinish()
   - Send the fds over a socket connection (use port from CLI args) to the EMP.
2. The EMP will spawn the python script with a port argument and wait for the file descriptors on the socket. Ensure that the ERP is ready to receive shared textures.
3. Upon receiving the file descriptors, you will call `sharedTexture.importSharedTexture(options)` to import it into the EMP. After import, call `sharedTexture.sendSharedTexture(options, ...args)` to send it to the ERP.
4. The ERP should get the `VideoFrame` out of the `SharedTextureImported` object passed in the receiver callback and display it in the `index.html`.
~
Communication between python script and EMP happens over socket. Send and receive JSON. The EMP should spwan the python script with three arguments: port, width, and height. The port should be random unused port. The width and height should be the size of the renderable area in the window. For starters, do not allow window to resize, as this will require EMP to send the size again to the python script over socket and the python script will have to realloc it's texture and re-export and send new FDs.