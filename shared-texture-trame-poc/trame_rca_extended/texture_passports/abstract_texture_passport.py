class AbstractTexturePassport:
    def __init__(self, render_window, width, height, ):
        self.render_window = render_window
        self.width = width
        self.height = height

    def to_json(self):
        return {}

    def release(self):
        pass

    def flush_graphics_commands(self):
        pass
