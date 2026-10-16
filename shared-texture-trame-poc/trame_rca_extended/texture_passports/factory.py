def texture_passport_class(name):
    if not isinstance(name, str):
        raise TypeError("Passport name must be string type")
    elif name == "iosurface":
        from .iosurface import IOSurfaceTexturePassport
        return IOSurfaceTexturePassport
    elif name == "nthandle":
        from .nthandle import NTHandleTexturePassport
        return NTHandleTexturePassport
    elif name == "wayland-dmabuf":
        from .wayland_dmabuf import WaylandDMABUFTexturePassport
        return WaylandDMABUFTexturePassport
    elif name == "x11-dmabuf":
        raise NotImplementedError("x11-dmabuf passport not supported yet")
    else:
        raise ValueError(f"Passport name '{name}' is not recognized".format(name))
