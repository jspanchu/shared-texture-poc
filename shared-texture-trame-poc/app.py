import json
import platform
from importlib.metadata import version

# Required for vtk factory
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

from trame.app import TrameApp, asynchronous
from trame.decorators import change
from trame.ui.vuetify3 import SinglePageWithDrawerLayout
from trame.widgets import html, vuetify3 as v3
from trame_rca.utils import RcaViewAdapter
from trame_rca.widgets import rca

from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
from vtkmodules.vtkRenderingCore import vtkCompositePolyDataMapper, vtkVolume, vtkActor, vtkRenderer, vtkRenderWindow, vtkColorTransferFunction, vtkVolumeProperty
from vtkmodules.vtkRenderingUI import vtkGenericRenderWindowInteractor
from vtkmodules.vtkRenderingVolumeOpenGL2 import vtkSmartVolumeMapper
from vtkmodules.vtkFiltersSources import vtkPartitionedDataSetCollectionSource
from vtkmodules.vtkImagingCore import vtkRTAnalyticSource

# Extended trame rca package
from trame_rca_extended.widgets import ElectronSharedTextureView
from trame_rca_extended.schedulers import ElectronSharedTextureRenderScheduler


def create_vtk_pipeline():
    wavelet = vtkRTAnalyticSource()
    volume_mapper = vtkSmartVolumeMapper()
    volume_mapper.SetInputConnection(wavelet.output_port)
    color_tf = vtkColorTransferFunction()
    color_tf.AddRGBPoint(0., 0., 0., 1.)
    color_tf.AddRGBPoint(255. / 2., 0., 1., 1.)
    color_tf.AddRGBPoint(255., 1., 0., 1.)
    opacity_tf = vtkPiecewiseFunction()
    opacity_tf.AddPoint(0., 0.)
    opacity_tf.AddPoint(255., 0.1)
    volume_property = vtkVolumeProperty()
    volume_property.SetColor(color_tf)
    volume_property.SetScalarOpacity(opacity_tf)
    volume_property.ShadeOn()
    volume_property.SetInterpolationTypeToLinear()
    volume_actor = vtkVolume(mapper=volume_mapper, property=volume_property)

    shapes = vtkPartitionedDataSetCollectionSource()
    cpdm = vtkCompositePolyDataMapper()
    cpdm.SetInputConnection(shapes.output_port)
    shapes_actor = vtkActor(mapper=cpdm)

    renderer = vtkRenderer()
    renderer.AddVolume(volume_actor)
    renderer.AddActor(shapes_actor)
    # The IOSurface's alpha reaches Electron, so keep the background opaque.
    renderer.SetBackgroundAlpha(1.0)
    renderer.ResetCamera()

    render_window = vtkRenderWindow()
    render_window.AddRenderer(renderer)
    # trame-rca replays the canvas's input on the interactor.
    vtkGenericRenderWindowInteractor(render_window=render_window,
                                     interactor_style=vtkInteractorStyleTrackballCamera())
    return render_window, renderer, opacity_tf, shapes


PYTHON_PACKAGES = ["vtk", "trame", "trame-client", "trame-server", "trame-vuetify", "trame-rca", "wslink"]
ELECTRON_VERSIONS = [("Electron", "electron"), ("Chromium", "chrome"), ("Node", "node"), ("V8", "v8")]


class ElectronApp(TrameApp):
    def __init__(self, server=None):
        super().__init__(server)
        self.server.cli.add_argument("--emp-port", type=int, required=True,
                                     help="Port of the Electron main process's texture channel")
        self.server.cli.add_argument("--electron-versions", default="{}",
                                     help="The Electron main process's process.versions, as JSON")
        args, _ = self.server.cli.parse_known_args()

        electron_versions = json.loads(args.electron_versions)
        self.state.electron_versions = [{"name": name, "version": electron_versions[key]}
                                        for name, key in ELECTRON_VERSIONS if key in electron_versions]
        self.state.python_versions = [{"name": "Python", "version": platform.python_version()}] + [
            {"name": name, "version": version(name)} for name in PYTHON_PACKAGES]
        self.state.spinning = False
        # Frame stats from the canvas, once a second: only the footer needs them, not the server.
        self.state.client_only("stats")
        self.state.stats = {"fps": 0, "frames": 0, "info": ""}

        self.render_window, self.renderer, self.volume_opacity_tf, self.shapes_source = create_vtk_pipeline()
        self.scheduler = ElectronSharedTextureRenderScheduler(self.render_window, args.emp_port,
                                                      on_disconnect=self.on_emp_disconnect,
                                                      before_render=self.before_render)
        self.ctrl.on_server_ready.add(self.scheduler.start)
        self._build_ui()

    def on_emp_disconnect(self):
        asynchronous.create_task(self.server.stop())

    def reset_camera(self):
        self.renderer.ResetCamera()
        self.view_handler.update()

    def before_render(self):
        # Spin by a degree per frame: each frame asks for the next one.
        if self.state.spinning:
            self.renderer.GetActiveCamera().Azimuth(1.0)
            self.renderer.ResetCameraClippingRange()
            self.scheduler.schedule_render()

    @change("spinning")
    def on_spinning_change(self, spinning, **_):
        if spinning:
            self.view_handler.update()

    @change("number_of_shapes")
    def on_number_of_shapes_change(self, number_of_shapes, **_):
        self.shapes_source.number_of_shapes = min(12, max(1, int(number_of_shapes)))
        self.view_handler.update()

    @change("opacity")
    def on_opacity_change(self, opacity, **_):
        # Node 1 is the top of the scalar range: (x, y, midpoint, sharpness).
        self.volume_opacity_tf.SetNodeValue(1, [255.0, float(opacity), .5, 0.])
        self.view_handler.update()

    def _build_ui(self):
        with SinglePageWithDrawerLayout(self.server) as self.ui:
            self.ui.title.set_text("Shared texture trame POC")

            with self.ui.toolbar:
                v3.VSpacer()
                v3.VSlider(
                    v_model=("opacity", 0.1),
                    label="Opacity",
                    min=0.,
                    max=1.,
                    step=0.01,
                    thumb_label=True,
                    hide_details=True,
                    density="compact",
                    style="max-width: 250px",
                    classes="mx-2",
                )
                v3.VSlider(
                    v_model=("number_of_shapes", 1),
                    label="Shapes",
                    min=1,
                    max=12,
                    step=1,
                    thumb_label=True,
                    hide_details=True,
                    density="compact",
                    style="max-width: 250px",
                    classes="mx-2",
                )
                v3.VBtn(icon="mdi-crop-free", click=self.reset_camera)
                v3.VBtn(
                    icon="mdi-rotate-3d-variant",
                    click="spinning = !spinning",
                    color=("spinning ? 'primary' : undefined",),
                )

            with self.ui.drawer:
                with v3.VList(density="compact"):
                    v3.VListSubheader("Electron", v_if="electron_versions.length")
                    v3.VListItem(
                        v_for="item in electron_versions",
                        key=("item.name",),
                        title=("item.name",),
                        subtitle=("item.version",),
                    )
                    v3.VListSubheader("Python")
                    v3.VListItem(
                        v_for="item in python_versions",
                        key=("item.name",),
                        title=("item.name",),
                        subtitle=("item.version",),
                    )

            with self.ui.footer:
                self.ui.footer.clear()
                v3.VProgressCircular(
                    indeterminate=("!!trame__busy",),
                    color="#04a94d",
                    size=16,
                    width=3,
                    classes="ml-n3 mr-1",
                )
                html.Span("{{ stats.fps }} fps", classes="text-caption")
                v3.VSpacer()
                html.Span("{{ stats.info }}", classes="text-caption text-grey-lighten-1")

            with self.ui.content:
                with v3.VContainer(fluid=True, classes="pa-0 fill-height position-relative"):
                    with rca.RemoteControlledArea(display="shared-texture", event_throttle_ms=10) as view:
                        ElectronSharedTextureView(stats="stats = $event")
                    self.view_handler = RcaViewAdapter(self.scheduler.rca, view.name, scheduler=self.scheduler)
                    view.add_view_handler(self.view_handler)


if __name__ == "__main__":
    ElectronApp().server.start()
