"""Qt3D geometry and orthographic navigation for the demo viewer."""

from array import array
import math

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QQuaternion, QVector3D
from PySide6.Qt3DCore import Qt3DCore as Core
from PySide6.Qt3DRender import Qt3DRender as Render
from PySide6.Qt3DExtras import Qt3DExtras as Extras


BACKGROUND = (0.055, 0.075, 0.11)


def color_material(parent, color, shaded=True):
    """Use Qt3D's built-in tone shading without changing player hues."""
    return Extras.QGoochMaterial(
        parent, warm=QColor.fromRgbF(*color),
        cool=QColor.fromRgbF(*(c * 0.65 if shaded else c for c in color)),
        alpha=0, beta=0, specular=QColor("black"))


class Lines(Core.QEntity):
    """Independent line pairs; buffers are reused when endpoints move."""

    def __init__(self, parent, points, color):
        super().__init__(parent)
        self.geometry = geometry = Core.QGeometry(self)
        self.buffer = Core.QBuffer(geometry)
        self.attributes = []
        for name, offset in (
            (Core.QAttribute.defaultPositionAttributeName(), 0),
            (Core.QAttribute.defaultNormalAttributeName(), 12),
        ):
            # The convenience constructor loses attributes added after rendering
            # starts with this Qt3D build; explicit setters also work on reload.
            attribute = Core.QAttribute(geometry)
            attribute.setBuffer(self.buffer)
            attribute.setName(name)
            attribute.setVertexBaseType(Core.QAttribute.VertexBaseType.Float)
            attribute.setVertexSize(3)
            attribute.setByteOffset(offset)
            attribute.setByteStride(24)
            geometry.addAttribute(attribute)
            self.attributes.append(attribute)
        self.renderer = Render.QGeometryRenderer(self)
        self.renderer.setGeometry(geometry)
        self.renderer.setPrimitiveType(Render.QGeometryRenderer.PrimitiveType.Lines)
        self.addComponent(self.renderer)
        self.addComponent(color_material(self, color, shaded=False))
        self.set_points(points)

    def set_points(self, points):
        vertices = array("f")
        for point in points:
            # The RHI material pipeline requires normals for colored lines too.
            vertices.extend((*point, 0.0, 0.0, 1.0))
        self.buffer.setData(QByteArray(vertices.tobytes()))
        count = len(vertices) // 6
        for attribute in self.attributes:
            attribute.setCount(count)
        self.renderer.setVertexCount(count)


class Solid(Core.QEntity):
    def __init__(self, parent, mesh, color):
        super().__init__(parent)
        self.transform = Core.QTransform(self)
        material = color_material(self, color)
        self.addComponent(mesh)
        self.addComponent(material)
        self.addComponent(self.transform)


class Marker(Solid):
    def __init__(self, parent, color):
        mesh = Extras.QSphereMesh(parent, radius=42, slices=16, rings=12)
        super().__init__(parent, mesh, color)

    def set_position(self, position):
        self.transform.setTranslation(QVector3D(*position))


class Arrow(Core.QEntity):
    """A cylinder and cone along local +Y, rotated onto the heading."""

    def __init__(self, parent, color, length=170.0, thickness=240.0):
        super().__init__(parent)
        self.transform = Core.QTransform(self)
        self.addComponent(self.transform)
        shaft = Extras.QCylinderMesh(self, radius=thickness * 0.055, length=length * 0.7, slices=16)
        self.shaft = Solid(self, shaft, color)
        self.shaft.transform.setTranslation(QVector3D(0, length * 0.35, 0))
        tip = Extras.QConeMesh(self, bottomRadius=thickness * 0.12,
                              topRadius=0, length=length * 0.3, slices=16)
        self.tip = Solid(self, tip, color)
        self.tip.transform.setTranslation(QVector3D(0, length * 0.85, 0))

    def set_pose(self, position, direction):
        self.transform.setTranslation(QVector3D(*position))
        self.transform.setRotation(QQuaternion.rotationTo(
            QVector3D(0, 1, 0), QVector3D(*direction).normalized()))


class ScreenText(Extras.QText2DEntity):
    """Screen-aligned text rendered by Qt3D in the existing window."""

    def __init__(self, scene, alignment=Qt.AlignmentFlag.AlignRight):
        super().__init__(scene.root)
        self.scene = scene
        font = QFont()
        font.setPixelSize(16)
        self.setFont(font)
        self.setColor(QColor("#eef3fa"))
        self.setAlignment(alignment | Qt.AlignmentFlag.AlignTop)
        self.setEnabled(False)
        self.transform = Core.QTransform(self)
        self.addComponent(self.transform)
        layer = Render.QLayer(self)
        layer.setRecursive(True)
        self.addComponent(layer)
        self.camera = Render.QCamera(scene.root)
        self.camera.setPosition(QVector3D(0, 0, 10))
        self.camera.setViewCenter(QVector3D())
        viewport = Render.QViewport(scene.frame_graph)
        overlay = Render.QRenderStateSet(viewport)
        depth = Render.QDepthTest(overlay)
        depth.setDepthFunction(Render.QDepthTest.DepthFunction.Always)
        overlay.addRenderState(depth)
        overlay.addRenderState(Render.QNoDepthMask(overlay))
        selector = Render.QCameraSelector(overlay)
        selector.setCamera(self.camera)
        self.layer_filter = Render.QLayerFilter(selector)
        self.layer_filter.addLayer(layer)
        scene.widthChanged.connect(self.reposition)
        scene.heightChanged.connect(self.reposition)
        self.reposition()

    def reposition(self, *_):
        width, height = max(1, self.scene.width()), max(1, self.scene.height())
        self.camera.lens().setOrthographicProjection(0, width, 0, height, 0.1, 100)
        self.setWidth(min(420, max(1, (width - 36) / 2)))
        self.setHeight(max(1, height - 24))
        x = width - self.width() - 12 if self.alignment() & Qt.AlignmentFlag.AlignRight else 12
        self.transform.setTranslation(QVector3D(x, 12, 0))

    def display(self, lines):
        # QText2DEntity lays out paragraphs at the same origin. Soft line
        # breaks keep all messages in one layout with distinct baselines.
        text = "\u2028".join(lines)
        text = text.replace("\r\n", "\n").replace("\n", "\u2028").replace("\r", "\u2028")
        if text != self.text():
            self.setText(text)
        self.setEnabled(bool(text))


class ChatOverlay(ScreenText):
    def display(self, messages):
        super().display(
            f"{'[Team] ' if msg.get('channel') == 'team' else ''}"
            f"{msg.get('player') or 'Unknown'}: {msg['text']}" for msg in messages)


class SceneWindow(Extras.Qt3DWindow):
    def __init__(self):
        super().__init__()
        self.root = Core.QEntity()
        self.content = Core.QEntity(self.root)
        self.content_layer = Render.QLayer(self.root)
        self.content_layer.setRecursive(True)
        self.content.addComponent(self.content_layer)
        light_entity = Core.QEntity(self.root)
        light_entity.addComponent(Render.QPointLight(light_entity))
        light_transform = Core.QTransform(light_entity)
        light_entity.addComponent(light_transform)
        self.camera().positionChanged.connect(light_transform.setTranslation)
        self.setRootEntity(self.root)
        self.half_height = 720.0
        self.far_plane = 100000.0
        self.last_mouse = None
        self.camera().setUpVector(QVector3D(0, 1, 0))
        self.camera().setPosition(QVector3D(1350, 1250, 1350))
        self.camera().setViewCenter(QVector3D())
        self._create_axes()
        self.chat_overlay = ChatOverlay(self)
        self.score_overlay = ScreenText(self, Qt.AlignmentFlag.AlignLeft)
        self.widthChanged.connect(self._projection)
        self.heightChanged.connect(self._projection)
        self._projection()

    def clear(self):
        self.content.setEnabled(False)
        self.content.setParent(None)
        self.content.deleteLater()
        self.content = Core.QEntity(self.root)
        self.content.addComponent(self.content_layer)

    def _create_axes(self):
        axes = Core.QEntity(self.root)
        layer = Render.QLayer(axes)
        layer.setRecursive(True)
        axes.addComponent(layer)
        for name, direction, color in (
            ("X", (1, 0, 0), (1.0, 0.25, 0.25)),
            ("Y", (0, 1, 0), (0.3, 1.0, 0.3)),
            ("Z", (0, 0, 1), (0.3, 0.55, 1.0)),
        ):
            arrow = Arrow(axes, color, length=1.0, thickness=1.4)
            arrow.set_pose((0, 0, 0), direction)
            mesh = Extras.QExtrudedTextMesh(axes, text=name, font=QFont("Helvetica", 10), depth=0.01)
            label = Solid(axes, mesh, color)
            label.transform.setScale(0.3)
            label.transform.setTranslation(QVector3D(*direction) * 1.2)
        self.axes_camera = Render.QCamera(self.root)
        self.camera().positionChanged.connect(self._orient_axes)
        self.camera().viewCenterChanged.connect(self._orient_axes)
        self.camera().upVectorChanged.connect(self._orient_axes)
        self._orient_axes()

        # Draw the map first, then an independently framed orientation inset.
        self.frame_graph = Render.QRenderSurfaceSelector()
        self.frame_graph.setSurface(self)
        viewport = Render.QViewport(self.frame_graph)
        viewport.setNormalizedRect(QRectF(0, 0, 1, 1))
        clear = Render.QClearBuffers(viewport)
        clear.setBuffers(Render.QClearBuffers.BufferType.ColorDepthBuffer)
        clear.setClearColor(QColor.fromRgbF(*BACKGROUND))
        camera = Render.QCameraSelector(clear)
        camera.setCamera(self.camera())
        main_filter = Render.QLayerFilter(camera)
        main_filter.addLayer(self.content_layer)
        inset = Render.QViewport(self.frame_graph)
        inset.setNormalizedRect(QRectF(0.02, 0.78, 0.15, 0.20))
        overlay = Render.QRenderStateSet(inset)
        depth = Render.QDepthTest(overlay)
        depth.setDepthFunction(Render.QDepthTest.DepthFunction.Always)
        overlay.addRenderState(depth)
        overlay.addRenderState(Render.QNoDepthMask(overlay))
        inset_camera = Render.QCameraSelector(overlay)
        inset_camera.setCamera(self.axes_camera)
        axes_filter = Render.QLayerFilter(inset_camera)
        axes_filter.addLayer(layer)
        self.setActiveFrameGraph(self.frame_graph)

    def _orient_axes(self, *_):
        camera = self.camera()
        self.axes_camera.setPosition((camera.position() - camera.viewCenter()).normalized() * 5)
        self.axes_camera.setViewCenter(QVector3D())
        self.axes_camera.setUpVector(camera.upVector())

    def fit(self, points):
        low = [min(p[a] for p in points) for a in range(3)]
        high = [max(p[a] for p in points) for a in range(3)]
        center = QVector3D(*[(a + b) / 2 for a, b in zip(low, high)])
        extent = max(max(b - a for a, b in zip(low, high)), 1000.0)
        camera = self.camera()
        camera.setViewCenter(center)
        camera.setPosition(center + QVector3D(1.35, 1.25, 1.35) * extent)
        camera.setUpVector(QVector3D(0, 1, 0))
        # Fit the actual projected paths, rather than a bounding sphere whose
        # depth also needlessly increases the orthographic zoom distance.
        view = camera.viewMatrix()
        projected = [view.map(QVector3D(*point)) for point in points]
        left, right = min(p.x() for p in projected), max(p.x() for p in projected)
        bottom, top = min(p.y() for p in projected), max(p.y() for p in projected)
        shift = view.inverted()[0].mapVector(QVector3D((left + right) / 2, (bottom + top) / 2, 0))
        camera.setViewCenter(center + shift)
        camera.setPosition(camera.position() + shift)
        aspect = max(self.width(), 1) / max(self.height(), 1)
        # Leave room for the 170-unit heading arrows / 180-unit velocity lines,
        # plus a small visual margin. Camera distance only controls clipping.
        self.half_height = max((top - bottom) / 2 + 180,
                               ((right - left) / 2 + 180) / aspect) * 1.05
        self.far_plane = max(extent * 20, 100000.0)
        self._projection()

    def _projection(self, *_):
        half_width = self.half_height * max(self.width(), 1) / max(self.height(), 1)
        self.camera().lens().setOrthographicProjection(
            -half_width, half_width, -self.half_height, self.half_height,
            0.1, self.far_plane)
        aspect = max(self.width(), 1) * 0.15 / (max(self.height(), 1) * 0.20)
        self.axes_camera.lens().setOrthographicProjection(
            -1.7 * aspect, 1.7 * aspect, -1.7, 1.7, 0.1, 20)

    def mousePressEvent(self, event):
        self.last_mouse = event.position()
        event.accept()

    def mouseReleaseEvent(self, event):
        self.last_mouse = None
        event.accept()

    def mouseMoveEvent(self, event):
        if self.last_mouse is None:
            return
        delta = event.position() - self.last_mouse
        self.last_mouse = event.position()
        camera = self.camera()
        if event.buttons() & Qt.MouseButton.LeftButton:
            # Orbit with a fixed horizon. Crossing a pole used to turn the
            # camera upside down and reverse horizontal dragging on screen.
            offset = camera.position() - camera.viewCenter()
            radius = offset.length()
            if radius > 1e-6:
                yaw = math.atan2(offset.x(), offset.z()) - math.radians(delta.x() * 0.35)
                pitch = math.atan2(offset.y(), math.hypot(offset.x(), offset.z()))
                limit = math.radians(89)
                pitch = max(-limit, min(limit, pitch + math.radians(delta.y() * 0.35)))
                camera.setPosition(camera.viewCenter() + QVector3D(
                    math.sin(yaw) * math.cos(pitch), math.sin(pitch),
                    math.cos(yaw) * math.cos(pitch)) * radius)
                camera.setUpVector(QVector3D(0, 1, 0))
        elif event.buttons() & Qt.MouseButton.MiddleButton:
            scale = 2 * self.half_height / max(self.height(), 1)
            camera.translate(QVector3D(-delta.x() * scale, delta.y() * scale, 0))
        event.accept()

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        if not steps:
            steps = event.pixelDelta().y() / 40
        self.half_height = min(1e7, max(1.0, self.half_height * 0.85 ** steps))
        self._projection()
        event.accept()
