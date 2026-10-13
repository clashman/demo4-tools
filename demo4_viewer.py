"""Interactive 3D viewer for inferred UT2004 player movement in demo4 files."""

from __future__ import annotations

import argparse
from bisect import bisect_left
import math
from pathlib import Path
import signal
import sys

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# VTK's modular Python build requires backend/style imports to register their
# concrete implementations before constructing the Qt render window.
import vtkmodules.vtkInteractionStyle
import vtkmodules.vtkRenderingOpenGL2

from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkFiltersSources import vtkArrowSource, vtkSphereSource
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkRenderer

from lib.demo import Actor, read_header
from lib.netmodel import Model
from lib.upkg import PackageIndex
from lib.utpath import find_ut2004, find_ut2004_userdir
from pkgdb import DEFAULT_DB, PackageDB
from ut2k4demo import Analyzer, build_parser


PLAYER_COLORS = (
    (0.25, 0.75, 1.0),
    (1.0, 0.45, 0.28),
    (0.45, 0.9, 0.55),
    (0.9, 0.75, 0.25),
)
FACING_LENGTH = 170.0
FACING_THICKNESS = 240.0


class LoadDemo(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, filename: Path):
        super().__init__()
        self.filename = filename

    def run(self):
        try:
            data = self.filename.read_bytes()
            header = read_header(data)
            package_paths = {
                path for path in (find_ut2004(), find_ut2004_userdir())
                if path is not None and path.is_dir()
            }
            package_index = (PackageIndex(package_paths) if package_paths
                             else PackageDB(DEFAULT_DB))
            model = Model(package_index)
            parser, analyzer, _ = build_parser(
                data, header, package_index, model)
            parser.run()
            tracks = _movement_tracks(analyzer)
            self.loaded.emit({
                "filename": self.filename,
                "map_name": header["map"],
                "tracks": tracks,
            })
        except Exception as exc:  # surface parser/IO errors in the window
            self.failed.emit(str(exc))


def _vector(value):
    if isinstance(value, dict):
        values = (value.get("x"), value.get("y"), value.get("z"))
    elif isinstance(value, (tuple, list)) and len(value) == 3:
        values = value
    else:
        return None
    try:
        return {axis: float(component)
                for axis, component in zip(("x", "y", "z"), values)}
    except (TypeError, ValueError):
        return None


def _movement_tracks(analyzer: Analyzer) -> dict:
    tracks = {}
    for pawn_id, samples in analyzer.pawn_samples.items():
        pawn = analyzer.pawns[pawn_id]
        pri = analyzer.pawn_pris.get(pawn_id)
        if not isinstance(pri, Actor):
            continue
        player = analyzer.pri_name(pri)
        if not player:
            continue

        merged = {}
        for sample in samples:
            location = _vector(sample.get("location"))
            if location is None:
                continue
            timestamp = sample["time"]
            row = merged.setdefault(timestamp, {
                "elapsed_seconds": timestamp,
                "channel": pawn.chan,
                "channel_lifetime": pawn.id,
                "location": location,
                "rotation": None,
                "raw_velocity": None,
            })
            row["location"] = location
            rotation = sample.get("rotation")
            if isinstance(rotation, (tuple, list)) and len(rotation) == 3:
                row["rotation"] = tuple(float(value) for value in rotation)
            velocity = _vector(sample.get("velocity"))
            if velocity is not None:
                row["raw_velocity"] = velocity

        rows = sorted(merged.values(), key=lambda row: row["elapsed_seconds"])
        movement = []
        for index, row in enumerate(rows):
            velocity = row["raw_velocity"]
            if velocity is None:
                before = rows[index - 1] if index else None
                after = rows[index + 1] if index + 1 < len(rows) else None
                if (before is not None and after is not None
                        and 0 < after["elapsed_seconds"] - before["elapsed_seconds"] <= 0.5):
                    start, end = before, after
                elif before is not None and 0 < row["elapsed_seconds"] - before["elapsed_seconds"] <= 0.5:
                    start, end = before, row
                elif after is not None and 0 < after["elapsed_seconds"] - row["elapsed_seconds"] <= 0.5:
                    start, end = row, after
                else:
                    start = end = None
                if start is None:
                    velocity = {axis: 0.0 for axis in ("x", "y", "z")}
                else:
                    elapsed = end["elapsed_seconds"] - start["elapsed_seconds"]
                    velocity = {
                        axis: (end["location"][axis] - start["location"][axis]) / elapsed
                        for axis in ("x", "y", "z")
                    }

            speed = math.sqrt(sum(value * value for value in velocity.values()))
            movement_direction = _unit(tuple(velocity[a] for a in ("x", "y", "z")))
            rotation = row["rotation"]
            if rotation is not None:
                pitch, yaw, roll = rotation
                pitch_rad = pitch * (2 * math.pi / 65536.0)
                yaw_rad = yaw * (2 * math.pi / 65536.0)
                facing = (math.cos(pitch_rad) * math.cos(yaw_rad),
                          math.cos(pitch_rad) * math.sin(yaw_rad),
                          math.sin(pitch_rad))
                rotation_degrees = {
                    "pitch": pitch * 360.0 / 65536.0,
                    "yaw": yaw * 360.0 / 65536.0,
                    "roll": roll * 360.0 / 65536.0,
                }
            else:
                facing = movement_direction or (1.0, 0.0, 0.0)
                rotation_degrees = {
                    "pitch": 0.0,
                    "yaw": math.degrees(math.atan2(facing[1], facing[0])),
                    "roll": 0.0,
                }
            movement.append({
                "elapsed_seconds": row["elapsed_seconds"],
                "channel": row["channel"],
                "channel_lifetime": row["channel_lifetime"],
                "location": row["location"],
                "velocity": velocity,
                "facing_direction": dict(zip(("x", "y", "z"), facing)),
                "movement_direction": (
                    dict(zip(("x", "y", "z"), movement_direction))
                    if movement_direction else None
                ),
                "rotation_degrees": rotation_degrees,
                "speed": speed,
            })

        if not movement:
            continue
        track = tracks.setdefault(player, {"movement": [], "pawn_lifetimes": []})
        track["movement"].extend(movement)
        track["pawn_lifetimes"].append({
            "channel": pawn.chan,
            "channel_lifetime": pawn.id,
        })

    for track in tracks.values():
        track["movement"].sort(key=lambda row: row["elapsed_seconds"])
    return tracks


def _world(vector: dict) -> tuple[float, float, float]:
    """Convert Unreal XYZ coordinates to a Y-up viewer's XYZ coordinates."""
    return vector["x"], vector["z"], vector["y"]


def _lerp(a: float, b: float, alpha: float) -> float:
    return a + (b - a) * alpha


def _unit(vector):
    length = math.sqrt(sum(value * value for value in vector))
    return tuple(value / length for value in vector) if length > 1e-8 else None


def _index_track(track: dict):
    by_lifetime = {}
    for row in track["movement"]:
        by_lifetime.setdefault((row["channel"], row["channel_lifetime"]), []).append(row)
    indexed = []
    for rows in by_lifetime.values():
        rows.sort(key=lambda row: row["elapsed_seconds"])
        indexed.append(([row["elapsed_seconds"] for row in rows], rows))
    return indexed


def _sample_track(indexed_track: list, timestamp: float):
    """Interpolate within one Pawn lifetime, never across a respawn/gap."""
    nearest = None
    for times, rows in indexed_track:
        index = bisect_left(times, timestamp)
        if index < len(rows) and times[index] == timestamp:
            return rows[index]
        if 0 < index < len(rows):
            before, after = rows[index - 1], rows[index]
            gap = after["elapsed_seconds"] - before["elapsed_seconds"]
            if 0 < gap <= 0.5:
                alpha = (timestamp - before["elapsed_seconds"]) / gap
                return _interpolate(before, after, alpha, timestamp)
        candidates = rows[max(0, index - 1):min(len(rows), index + 1)]
        if candidates:
            candidate = min(candidates, key=lambda row: abs(row["elapsed_seconds"] - timestamp))
            distance = abs(candidate["elapsed_seconds"] - timestamp)
            if distance <= 0.12 and (nearest is None or distance < nearest[0]):
                nearest = distance, candidate
    return nearest[1] if nearest else None


def _interpolate(a: dict, b: dict, alpha: float, timestamp: float):
    result = dict(a)
    result["elapsed_seconds"] = timestamp
    for field in ("location", "velocity", "facing_direction"):
        result[field] = {
            axis: _lerp(a[field][axis], b[field][axis], alpha)
            for axis in ("x", "y", "z")
        }
    facing = _unit(tuple(result["facing_direction"][axis] for axis in ("x", "y", "z")))
    result["facing_direction"] = dict(zip(("x", "y", "z"), facing or (1, 0, 0)))
    result["speed"] = math.sqrt(sum(v * v for v in result["velocity"].values()))
    direction = _unit(tuple(result["velocity"][axis] for axis in ("x", "y", "z")))
    result["movement_direction"] = (
        dict(zip(("x", "y", "z"), direction)) if direction else None
    )
    return result


class Viewer(QMainWindow):
    def __init__(self, initial_file: Path | None = None):
        super().__init__()
        self.setWindowTitle("UT2004 Demo Viewer")
        self.resize(1280, 820)
        self.tracks = {}
        self.track_indices = {}
        self.map_name = ""
        self.current_file = None
        self.checkboxes = {}
        self.player_actors = {}
        self.worker = None
        self.duration = 0.0

        self.renderer = vtkRenderer()
        self.renderer.SetBackground(0.055, 0.075, 0.11)
        self.view = QVTKRenderWindowInteractor(self)
        self.view.GetRenderWindow().AddRenderer(self.renderer)
        self.camera_style = vtkInteractorStyleTrackballCamera()
        self.camera_style.SetDefaultRenderer(self.renderer)
        self.view.SetInteractorStyle(self.camera_style)
        self.view.setToolTip("Left-drag: rotate · Middle-drag: pan · Scroll: zoom")

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        self.setCentralWidget(central)

        toolbar = QHBoxLayout()
        self.open_button = QPushButton("Open demo…")
        self.open_button.clicked.connect(self.open_dialog)
        toolbar.addWidget(self.open_button)
        self.loading_indicator = QProgressBar()
        self.loading_indicator.setRange(0, 0)
        self.loading_indicator.setTextVisible(False)
        self.loading_indicator.setFixedWidth(90)
        self.loading_indicator.setMaximumHeight(16)
        self.loading_indicator.setToolTip("Loading demo")
        self.loading_indicator.hide()
        toolbar.addWidget(self.loading_indicator)
        self.title = QLabel("Select a demo4 file")
        self.title.setStyleSheet("color: #cbd5e1; font-weight: 600")
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.title, 1)
        self.path_checkbox = QCheckBox("Movement trails")
        self.path_checkbox.setChecked(True)
        self.path_checkbox.toggled.connect(self._toggle_paths)
        toolbar.addWidget(self.path_checkbox)
        self.facing_checkbox = QCheckBox("Facing direction")
        self.facing_checkbox.setChecked(True)
        self.facing_checkbox.toggled.connect(self._update_time)
        toolbar.addWidget(self.facing_checkbox)
        self.velocity_checkbox = QCheckBox("Movement direction")
        self.velocity_checkbox.setChecked(True)
        self.velocity_checkbox.toggled.connect(self._update_time)
        toolbar.addWidget(self.velocity_checkbox)
        self.legend = QLabel("Colored arrow: pawn facing  ·  Light line: movement direction")
        self.legend.setStyleSheet("color: #aeb9c8; font-size: 11px")
        self.legend.setMinimumWidth(0)
        self.legend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.legend)
        layout.addLayout(toolbar)
        self.player_row = QHBoxLayout()
        layout.addLayout(self.player_row)
        self.view.setMinimumSize(1, 1)
        self.view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.view, 1)

        controls = QHBoxLayout()
        self.play_button = QPushButton("▶")
        self.play_button.setFixedWidth(42)
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setRange(0, 0)
        self.time_slider.setEnabled(False)
        self.time_slider.setSingleStep(33)
        self.time_slider.setPageStep(1000)
        self.time_slider.valueChanged.connect(self._update_time)
        controls.addWidget(self.time_slider, 1)
        self.time_label = QLabel("00:00.000")
        self.time_label.setMinimumWidth(82)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        self.statusBar().showMessage(
            "Left-drag: rotate · Middle-drag: pan · Scroll: zoom · "
            "Positions and orientations are inferred from demo updates."
        )
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._advance)

        self.view.Initialize()
        self.view.Start()
        self.axes_widget = vtkOrientationMarkerWidget()
        self.axes_widget.SetOrientationMarker(vtkAxesActor())
        self.axes_widget.SetInteractor(self.view.GetRenderWindow().GetInteractor())
        self.axes_widget.SetViewport(0.02, 0.02, 0.17, 0.23)
        self.axes_widget.SetOutlineColor(0.75, 0.78, 0.84)
        self.axes_widget.SetEnabled(True)
        self.axes_widget.InteractiveOff()
        if initial_file:
            QTimer.singleShot(0, lambda: self.load_file(initial_file))

    def open_dialog(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open UT2004 demo", str(Path.cwd()), "UT2004 demos (*.demo4);;All files (*)"
        )
        if filename:
            self.load_file(Path(filename))

    def load_file(self, filename: Path):
        if not filename.exists():
            QMessageBox.warning(self, "File not found", str(filename))
            return
        if self.worker and self.worker.isRunning():
            self.statusBar().showMessage("A demo is already loading.")
            return
        self.open_button.setEnabled(False)
        self.loading_indicator.show()
        self.play_button.setEnabled(False)
        self.timer.stop()
        self.title.setText(f"Loading {filename.name} …")
        self.worker = LoadDemo(filename)
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(self._load_failed)
        self.worker.start()

    def _load_failed(self, message: str):
        self.open_button.setEnabled(True)
        self.loading_indicator.hide()
        self.title.setText("Loading failed")
        QMessageBox.critical(self, "Could not load demo", message)

    def _loaded(self, data: dict):
        self.open_button.setEnabled(True)
        self.tracks = data["tracks"]
        self.track_indices = {
            player: _index_track(track) for player, track in self.tracks.items()
        }
        self.map_name = data["map_name"]
        self.current_file = data["filename"]
        self.title.setText(f"{self.map_name}  ·  {self.current_file.name}")
        if not self.tracks:
            self.loading_indicator.hide()
            self.renderer.RemoveAllViewProps()
            self.player_actors.clear()
            self.time_slider.setEnabled(False)
            self.play_button.setEnabled(False)
            self.view.Render()
            QMessageBox.information(self, "No named movement found",
                                    "No player movement could be identified in this demo.")
            return
        self._rebuild_player_controls()
        self._build_scene()
        times = [row["elapsed_seconds"] for track in self.tracks.values()
                 for row in track["movement"]]
        self.duration = max(times)
        self.time_slider.setRange(0, round(self.duration * 1000))
        self.time_slider.setValue(0)
        self.time_slider.setEnabled(True)
        self.play_button.setEnabled(True)
        self.loading_indicator.hide()
        self.statusBar().showMessage(
            f"{len(self.tracks)} players · {sum(len(t['movement']) for t in self.tracks.values()):,} samples"
        )
        self._update_time()
        self.view.Render()

    def _rebuild_player_controls(self):
        while self.player_row.count():
            item = self.player_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.checkboxes.clear()
        for index, player in enumerate(self.tracks):
            checkbox = QCheckBox(player)
            checkbox.setChecked(True)
            color = PLAYER_COLORS[index % len(PLAYER_COLORS)]
            color_css = "#%02x%02x%02x" % tuple(round(value * 255) for value in color)
            checkbox.setStyleSheet(f"color: {color_css}; font-weight: 600")
            checkbox.toggled.connect(lambda checked, name=player: self._toggle_player(name, checked))
            self.player_row.addWidget(checkbox)
            self.checkboxes[player] = checkbox
        self.player_row.addStretch(1)

    def _build_scene(self):
        self.renderer.RemoveAllViewProps()
        self.player_actors.clear()
        all_points = []

        for index, (player, track) in enumerate(self.tracks.items()):
            color = PLAYER_COLORS[index % len(PLAYER_COLORS)]
            points = vtkPoints()
            lines = vtkCellArray()
            for lifetime in track["pawn_lifetimes"]:
                key = (lifetime["channel"], lifetime["channel_lifetime"])
                rows = [row for row in track["movement"]
                        if (row["channel"], row["channel_lifetime"]) == key]
                rows.sort(key=lambda row: row["elapsed_seconds"])
                segment = []

                def flush_segment():
                    if len(segment) >= 2:
                        lines.InsertNextCell(len(segment))
                        for point_id in segment:
                            lines.InsertCellPoint(point_id)
                    segment.clear()

                previous = None
                for row in rows:
                    world = _world(row["location"])
                    all_points.append(world)
                    if previous is not None:
                        dt = row["elapsed_seconds"] - previous["elapsed_seconds"]
                        distance = math.dist(world, _world(previous["location"]))
                        if not (0 < dt <= 0.5 and distance <= 200):
                            flush_segment()
                    point_id = points.InsertNextPoint(*world)
                    segment.append(point_id)
                    previous = row
                flush_segment()

            poly = vtkPolyData()
            poly.SetPoints(points)
            poly.SetLines(lines)
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(poly)
            path_actor = vtkActor()
            path_actor.SetMapper(mapper)
            path_actor.GetProperty().SetColor(*color)
            path_actor.GetProperty().SetOpacity(0.28)
            path_actor.GetProperty().SetLineWidth(2.0)
            self.renderer.AddActor(path_actor)

            sphere = vtkSphereSource()
            sphere.SetRadius(42.0)
            sphere.SetThetaResolution(16)
            sphere.SetPhiResolution(12)
            sphere_mapper = vtkPolyDataMapper()
            sphere_mapper.SetInputConnection(sphere.GetOutputPort())
            marker = vtkActor()
            marker.SetMapper(sphere_mapper)
            marker.GetProperty().SetColor(*color)
            self.renderer.AddActor(marker)

            arrow = vtkArrowSource()
            arrow.SetTipLength(0.30)
            arrow.SetTipRadius(0.12)
            arrow.SetShaftRadius(0.055)
            arrow_mapper = vtkPolyDataMapper()
            arrow_mapper.SetInputConnection(arrow.GetOutputPort())
            facing_actor = vtkActor()
            facing_actor.SetMapper(arrow_mapper)
            facing_actor.GetProperty().SetColor(*color)
            self.renderer.AddActor(facing_actor)

            velocity_actor = vtkActor()
            velocity_actor.GetProperty().SetColor(0.85, 0.9, 1.0)
            velocity_actor.GetProperty().SetLineWidth(3.0)
            self.renderer.AddActor(velocity_actor)
            self.player_actors[player] = {
                "path": path_actor,
                "marker": marker,
                "facing": facing_actor,
                "velocity": velocity_actor,
            }

        if all_points:
            low = [min(point[axis] for point in all_points) for axis in range(3)]
            high = [max(point[axis] for point in all_points) for axis in range(3)]
            center = [(a + b) / 2 for a, b in zip(low, high)]
            extent = max(max(b - a for a, b in zip(low, high)), 1000.0)
            camera = self.renderer.GetActiveCamera()
            camera.SetFocalPoint(*center)
            camera.SetPosition(center[0] + extent * 1.35,
                               center[1] + extent * 1.25,
                               center[2] + extent * 1.35)
            camera.SetViewUp(0, 1, 0)
            camera.ParallelProjectionOn()
            camera.SetParallelScale(extent * 0.72)
            self.renderer.ResetCameraClippingRange()

    def _toggle_paths(self, visible: bool):
        for player, actors in self.player_actors.items():
            actors["path"].SetVisibility(visible and self.checkboxes[player].isChecked())
        self.view.Render()

    def _toggle_player(self, player: str, visible: bool):
        actors = self.player_actors.get(player, {})
        actors["path"].SetVisibility(visible and self.path_checkbox.isChecked())
        self._update_time()

    def _toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("▶")
        else:
            if self.time_slider.value() >= self.time_slider.maximum():
                self.time_slider.setValue(0)
            self.timer.start()
            self.play_button.setText("❚❚")

    def _advance(self):
        next_value = self.time_slider.value() + 33
        if next_value >= self.time_slider.maximum():
            self.time_slider.setValue(self.time_slider.maximum())
            self.timer.stop()
            self.play_button.setText("▶")
        else:
            self.time_slider.setValue(next_value)

    def _update_time(self, *_):
        if not self.tracks:
            return
        timestamp = self.time_slider.value() / 1000.0
        details = []
        for player, track in self.tracks.items():
            actors = self.player_actors[player]
            visible = self.checkboxes[player].isChecked()
            sample = _sample_track(self.track_indices[player], timestamp)
            active = visible and sample is not None
            actors["marker"].SetVisibility(active)
            actors["facing"].SetVisibility(active and self.facing_checkbox.isChecked())
            actors["velocity"].SetVisibility(active and self.velocity_checkbox.isChecked()
                                                and sample["movement_direction"] is not None)
            if not active:
                continue

            position = _world(sample["location"])
            actors["marker"].SetPosition(*position)
            direction = _unit(_world(sample["facing_direction"])) or (1, 0, 0)
            angle = math.degrees(math.acos(max(-1.0, min(1.0, direction[0]))))
            if direction[1] < 0:
                angle = -angle
            axis = _unit((0.0, -direction[2], direction[1]))
            if axis is None:
                axis = (0.0, 1.0, 0.0)
            transform = actors["facing"].GetUserTransform()
            if transform is None:
                from vtkmodules.vtkCommonTransforms import vtkTransform
                transform = vtkTransform()
                actors["facing"].SetUserTransform(transform)
            transform.Identity()
            transform.Translate(*position)
            transform.RotateWXYZ(angle, *axis)
            # vtkArrowSource has a unit-length X axis but a tiny ~0.05 radius.
            # Scale its cross-section too, otherwise the heading is invisible
            # at the map's several-thousand-unit camera scale.
            transform.Scale(FACING_LENGTH, FACING_THICKNESS, FACING_THICKNESS)

            if sample["movement_direction"] is not None:
                move_direction = _unit(_world(sample["movement_direction"]))
                length = min(180.0, max(45.0, sample["speed"] * 0.15))
                end = tuple(position[i] + move_direction[i] * length for i in range(3))
                line = actors["velocity"].GetMapper()
                if line is None:
                    from vtkmodules.vtkFiltersSources import vtkLineSource
                    source = vtkLineSource()
                    source.SetPoint1(*position)
                    source.SetPoint2(*end)
                    mapper = vtkPolyDataMapper()
                    mapper.SetInputConnection(source.GetOutputPort())
                    actors["velocity"].SetMapper(mapper)
                    actors["velocity_source"] = source
                else:
                    source = actors["velocity_source"]
                    source.SetPoint1(*position)
                    source.SetPoint2(*end)

            yaw = sample["rotation_degrees"]["yaw"]
            speed = sample["speed"]
            details.append(f"{player}: X {sample['location']['x']:.0f}, "
                           f"Y {sample['location']['y']:.0f}, Z {sample['location']['z']:.0f} · "
                           f"Yaw {yaw:.1f}° · {speed:.0f} uu/s")

        self.time_label.setText(f"{self._format_time(timestamp)} / {self._format_time(self.duration)}")
        self.statusBar().showMessage("   |   ".join(details) if details else "No player position at this time")
        self.view.Render()

    @staticmethod
    def _format_time(seconds: float) -> str:
        minutes = int(seconds // 60)
        return f"{minutes:02d}:{seconds - minutes * 60:06.3f}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path, help="optional demo4 file to open at startup")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    interrupt_timer = QTimer()
    interrupt_timer.timeout.connect(lambda: None)
    interrupt_timer.start(200)
    window = Viewer(args.file)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
