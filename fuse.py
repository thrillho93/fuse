#!/usr/bin/env python3
"""Fuse — stitch, rotate, and reencode video files."""

from __future__ import annotations

import atexit
import hashlib
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

def _init_preview_dir() -> Path:
    from gi.repository import GLib as _GLib
    d = Path(_GLib.get_user_cache_dir()) / "fuse" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d

PREVIEW_DIR = _init_preview_dir()

APP_ID = "io.github.frazier.Fuse"
APP_NAME = "Fuse"
APP_VERSION = "0.1.0"

ROTATIONS = [
    ("0°", 0),
    ("90° CW", 90),
    ("180°", 180),
    ("270° CW", 270),
]

VIDEO_EXTS = (
    "mp4", "mkv", "mov", "avi", "webm", "m4v",
    "wmv", "flv", "mpg", "mpeg", "ts", "3gp",
)

# (label, extension, plain-English description shown in the Output dialog)
OUTPUT_FORMATS = [
    ("MP4",  "mp4",  "Works on every phone, TV, browser, and media player"),
    ("MKV",  "mkv",  "Open format, great for archiving — plays on most desktop media players"),
    ("MOV",  "mov",  "Apple's format — best for macOS, iOS, and Final Cut Pro"),
]

# Short labels shown in each dropdown, plus the description shown as the row subtitle.
CODEC_CHOICES = [
    ("H.264", "libx264"),
    ("H.265", "libx265"),
]
CODEC_DESCS = [
    "Plays on any device — phones, smart TVs, and web browsers",
    "About 30% smaller files than H.264 — requires a device or player from 2015 or later",
]

QUALITY_CHOICES = [
    ("Highest", 17),
    ("High",    18),
    ("Good",    20),
    ("Balanced", 23),
    ("Compact", 26),
]
QUALITY_DESCS = [
    "Near-lossless — virtually identical to the original, very large files",
    "Great for archiving and sharing — high quality with manageable file size",
    "A solid balance of quality and file size",
    "Smaller files with a minor quality trade-off",
    "Smallest files — some quality loss is visible on close inspection",
]

PRESET_CHOICES = [
    ("Very Fast", "veryfast"),
    ("Fast",      "fast"),
    ("Medium",    "medium"),
    ("Slow",      "slow"),
    ("Very Slow", "veryslow"),
]
PRESET_DESCS = [
    "Encodes in seconds — larger output files",
    "Quick encoding with only slightly larger files",
    "A sensible middle ground",
    "Smaller files at the cost of longer encoding time",
    "Maximum compression — can take many minutes for long videos",
]

AUDIO_CHOICES = [
    ("128 kbps", "128k"),
    ("192 kbps", "192k"),
    ("256 kbps", "256k"),
    ("320 kbps", "320k"),
]
AUDIO_DESCS = [
    "Good for speech and casual listening",
    "Standard quality — suitable for most music and video",
    "High quality audio with noticeably better fidelity",
    "Maximum quality — best for music or critical listening",
]

DEFAULT_SETTINGS = {
    "video_crf":    18,
    "video_preset": "slow",
    "video_codec":  "libx264",
    "audio_bitrate": "192k",
    "enhance":      False,
}


class Settings:
    """Tiny JSON-backed settings store under $XDG_CONFIG_HOME/bitsplice."""

    def __init__(self):
        self._dir = Path(GLib.get_user_config_dir()) / "fuse"
        self._path = self._dir / "settings.json"
        self._data = dict(DEFAULT_SETTINGS)
        try:
            raw = json.loads(self._path.read_text())
            for k, v in raw.items():
                if k in DEFAULT_SETTINGS:
                    self._data[k] = v
        except (OSError, json.JSONDecodeError):
            pass

    def __getitem__(self, key):
        return self._data[key]

    def set(self, key, value):
        if self._data.get(key) != value:
            self._data[key] = value
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(self._data, indent=2))
            except OSError:
                pass


settings = Settings()


@dataclass
class Clip:
    path: str
    rotation: int = 0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    has_audio: bool = False
    trim_start: float = 0.0
    trim_end: float | None = None
    filmed_at: datetime | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def effective_duration(self) -> float:
        end = self.trim_end if self.trim_end is not None else self.duration
        return max(0.0, end - self.trim_start)

    @property
    def is_trimmed(self) -> bool:
        return self.trim_start > 0 or self.trim_end is not None


def parse_time(text: str) -> float | None:
    """Parse 'H:MM:SS.dd', 'M:SS.dd', or raw seconds. None if invalid."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    if any(v < 0 for v in vals):
        return None
    if len(vals) == 1:
        return vals[0]
    if len(vals) == 2:
        return vals[0] * 60 + vals[1]
    if len(vals) == 3:
        return vals[0] * 3600 + vals[1] * 60 + vals[2]
    return None


def format_time(secs: float) -> str:
    """Render seconds as M:SS or H:MM:SS with millisecond precision."""
    if secs < 0:
        secs = 0
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs - (h * 3600 + m * 60)
    if h:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


def build_preview_command(clip: Clip, out_path: Path) -> list[str]:
    chain = []
    if clip.rotation == 90:
        chain.append("transpose=1")
    elif clip.rotation == 180:
        chain.append("transpose=2,transpose=2")
    elif clip.rotation == 270:
        chain.append("transpose=2")
    if settings["enhance"]:
        chain.extend(["eq=contrast=1.2:brightness=0.04:saturation=1.15:gamma=0.88", "unsharp=3:3:0.8:3:3:0.0"])
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if clip.trim_start > 0:
        cmd.extend(["-ss", f"{clip.trim_start}"])
    if clip.trim_end is not None:
        cmd.extend(["-to", f"{clip.trim_end}"])
    cmd.extend(["-i", clip.path])
    if chain:
        cmd.extend(["-vf", ",".join(chain)])
    cmd.extend([
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-t", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ])
    return cmd


def preview_path_for(clip: Clip) -> Path:
    try:
        mtime = int(Path(clip.path).stat().st_mtime)
    except OSError:
        mtime = 0
    digest = hashlib.md5(
        f"{clip.path}:{mtime}:{clip.rotation}:{clip.trim_start}:{clip.trim_end}".encode()
    ).hexdigest()[:16]
    return PREVIEW_DIR / f"{digest}.mp4"


def thumbnail_path_for(clip: Clip) -> Path:
    try:
        mtime = int(Path(clip.path).stat().st_mtime)
    except OSError:
        mtime = 0
    digest = hashlib.md5(
        f"thumb:{clip.path}:{mtime}:{clip.rotation}".encode()
    ).hexdigest()[:16]
    return PREVIEW_DIR / f"thumb_{digest}.jpg"


def build_thumbnail_command(clip: Clip, out_path: Path) -> list[str]:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", clip.path]
    chain = []
    if clip.rotation == 90:
        chain.append("transpose=1")
    elif clip.rotation == 180:
        chain.append("transpose=2,transpose=2")
    elif clip.rotation == 270:
        chain.append("transpose=2")
    chain.append("scale=112:112:force_original_aspect_ratio=decrease")
    chain.append("pad=112:112:(ow-iw)/2:(oh-ih)/2")
    chain.append("setsar=1")
    cmd.extend(["-vf", ",".join(chain), "-vframes", "1", "-q:v", "3", str(out_path)])
    return cmd


def probe_clip(path: str) -> tuple[int, int, float, bool, datetime | None]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    width = height = 0
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and width == 0:
            width = int(s.get("width", 0))
            height = int(s.get("height", 0))
        elif s.get("codec_type") == "audio":
            has_audio = True
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0))
    filmed_at = _parse_creation_time(
        fmt.get("tags", {}).get("creation_time")
        or next(
            (s.get("tags", {}).get("creation_time")
             for s in data.get("streams", [])
             if s.get("tags", {}).get("creation_time")),
            None,
        )
    )
    return width, height, duration, has_audio, filmed_at


def _parse_creation_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Python 3.11+ handles trailing Z; older versions need the swap.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_ffmpeg_command(clips: list[Clip], output_path: str) -> list[str]:
    """Single-pass ffmpeg command: per-clip rotation, scale/pad to a common
    canvas, then concat. Silent audio is synthesized for clips without it so
    the concat filter's audio count stays consistent."""
    cmd = ["ffmpeg", "-y", "-hide_banner"]
    for c in clips:
        if c.trim_start > 0:
            cmd.extend(["-ss", f"{c.trim_start}"])
        if c.trim_end is not None:
            cmd.extend(["-to", f"{c.trim_end}"])
        cmd.extend(["-i", c.path])

    first = clips[0]
    if first.rotation in (90, 270):
        tw, th = first.height, first.width
    else:
        tw, th = first.width, first.height
    tw -= tw % 2
    th -= th % 2

    parts: list[str] = []
    concat_pairs: list[str] = []

    for i, c in enumerate(clips):
        chain: list[str] = []
        if c.rotation == 90:
            chain.append("transpose=1")
        elif c.rotation == 180:
            chain.append("transpose=2,transpose=2")
        elif c.rotation == 270:
            chain.append("transpose=2")
        if settings["enhance"]:
            chain.extend(["eq=contrast=1.2:brightness=0.04:saturation=1.15:gamma=0.88", "unsharp=3:3:0.8:3:3:0.0"])
        chain.append(f"scale={tw}:{th}:force_original_aspect_ratio=decrease")
        chain.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2")
        chain.append("setsar=1")
        chain.append("format=yuv420p")
        parts.append(f"[{i}:v:0]{','.join(chain)}[v{i}]")

        if c.has_audio:
            parts.append(
                f"[{i}:a:0]aformat=sample_rates=48000:channel_layouts=stereo[a{i}]"
            )
        else:
            parts.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={c.effective_duration}[a{i}]"
            )
        concat_pairs.append(f"[v{i}][a{i}]")

    parts.append(
        f"{''.join(concat_pairs)}concat=n={len(clips)}:v=1:a=1[outv][outa]"
    )

    cmd.extend(["-filter_complex", ";".join(parts)])
    cmd.extend(["-map", "[outv]", "-map", "[outa]"])
    cmd.extend(["-c:v", settings["video_codec"],
                "-preset", settings["video_preset"],
                "-crf", str(settings["video_crf"])])
    cmd.extend(["-c:a", "aac", "-b:a", settings["audio_bitrate"]])
    cmd.extend(["-progress", "pipe:1", "-nostats"])
    cmd.append(output_path)
    return cmd


class OutputDialog(Adw.Dialog):
    """Output settings dialog — format, filename, location, codec, quality."""

    def __init__(self, on_save):
        super().__init__(title="Output")
        self.set_content_width(500)
        self.set_content_height(580)
        self._on_save = on_save
        self._format_idx = 0

        videos = Path.home() / "Videos"
        self._folder = str(videos if videos.is_dir() else Path.home())

        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar.add_top_bar(header)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(self.save_btn)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        scroller.set_child(body)
        toolbar.set_content(scroller)

        # ---- Where to save ----
        dest_group = Adw.PreferencesGroup()
        body.append(dest_group)

        self.format_row = Adw.ComboRow(title="Format",
                                       subtitle=OUTPUT_FORMATS[0][2])
        self.format_row.set_subtitle_lines(0)
        self.format_row.set_model(Gtk.StringList.new([f[0] for f in OUTPUT_FORMATS]))
        self.format_row.connect("notify::selected", self._on_format_changed)
        dest_group.add(self.format_row)

        self.name_row = Adw.EntryRow(title="Filename")
        self.name_row.set_text(f"output.{OUTPUT_FORMATS[0][1]}")
        self.name_row.connect("notify::text", lambda *_: self._update_sensitivity())
        dest_group.add(self.name_row)

        self.folder_row = Adw.ActionRow(title="Save To", activatable=True)
        self.folder_row.set_subtitle(self._folder)
        self.folder_row.set_subtitle_lines(0)
        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron.set_valign(Gtk.Align.CENTER)
        self.folder_row.add_suffix(chevron)
        self.folder_row.connect("activated", self._on_folder_activated)
        dest_group.add(self.folder_row)

        # ---- Video settings ----
        video_group = Adw.PreferencesGroup(title="Video")
        body.append(video_group)

        self._combo(video_group, "Codec",           CODEC_CHOICES,   CODEC_DESCS,   "video_codec")
        self._combo(video_group, "Quality",         QUALITY_CHOICES, QUALITY_DESCS, "video_crf")
        self._combo(video_group, "Encoding Speed",  PRESET_CHOICES,  PRESET_DESCS,  "video_preset")

        enhance_row = Adw.SwitchRow(
            title="Enhance Video",
            subtitle="Auto-adjust brightness and contrast, sharpen details",
        )
        enhance_row.set_active(settings["enhance"])
        enhance_row.connect(
            "notify::active",
            lambda r, _p: settings.set("enhance", r.get_active()),
        )
        video_group.add(enhance_row)

        # ---- Audio settings ----
        audio_group = Adw.PreferencesGroup(title="Audio")
        body.append(audio_group)

        self._combo(audio_group, "Bitrate", AUDIO_CHOICES, AUDIO_DESCS, "audio_bitrate")

        self._update_sensitivity()

    # ---- helpers ----

    @staticmethod
    def _combo(group, title, choices, descs, setting_key):
        current = settings[setting_key]
        values = [c[1] for c in choices]
        try:
            idx = values.index(current)
        except ValueError:
            idx = 0
        row = Adw.ComboRow(title=title, subtitle=descs[idx])
        row.set_subtitle_lines(0)
        row.set_model(Gtk.StringList.new([c[0] for c in choices]))
        row.set_selected(idx)

        def on_change(r, _p):
            i = r.get_selected()
            settings.set(setting_key, values[i])
            r.set_subtitle(descs[i])

        row.connect("notify::selected", on_change)
        group.add(row)
        return row

    def _on_format_changed(self, row, _param):
        self._format_idx = row.get_selected()
        row.set_subtitle(OUTPUT_FORMATS[self._format_idx][2])
        ext = OUTPUT_FORMATS[self._format_idx][1]
        current = self.name_row.get_text().strip()
        if current:
            self.name_row.set_text(f"{Path(current).stem}.{ext}")

    def _on_folder_activated(self, _row):
        dialog = Gtk.FileDialog(title="Choose Save Location")
        dialog.set_initial_folder(Gio.File.new_for_path(self._folder))
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.select_folder(parent, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            f = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        self._folder = f.get_path()
        self.folder_row.set_subtitle(self._folder)

    def _update_sensitivity(self):
        self.save_btn.set_sensitive(bool(self.name_row.get_text().strip()))

    def _on_save_clicked(self, _btn):
        filename = self.name_row.get_text().strip()
        if not filename:
            return
        ext = OUTPUT_FORMATS[self._format_idx][1]
        output_path = str(Path(self._folder) / f"{Path(filename).stem}.{ext}")
        self.close()
        self._on_save(output_path)


class TrimDialog(Adw.Dialog):
    def __init__(self, clip: Clip, on_apply):
        super().__init__()
        self.set_title("Trim Clip")
        self.set_content_width(620)
        self.set_content_height(580)
        self._clip = clip
        self._on_apply = on_apply

        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar.add_top_bar(header)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("pill")
        apply_btn.connect("clicked", self._on_apply_clicked)
        header.pack_end(apply_btn)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        toolbar.set_content(body)

        self.video = Gtk.Video()
        self.video.set_size_request(-1, 220)
        self.video.set_hexpand(True)
        self.video.set_vexpand(True)
        self.video.add_css_class("card")
        body.append(self.video)

        self.media = Gtk.MediaFile.new_for_file(Gio.File.new_for_path(clip.path))
        self.media.connect("notify::error", self._on_media_error)
        self.video.set_media_stream(self.media)

        self.warning = Gtk.Label()
        self.warning.set_wrap(True)
        self.warning.set_xalign(0)
        self.warning.add_css_class("warning")
        self.warning.set_visible(False)
        body.append(self.warning)

        capture_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.CENTER,
        )
        body.append(capture_box)

        self.set_start_btn = Gtk.Button(label="Set Start at Playhead")
        self.set_start_btn.add_css_class("pill")
        self.set_start_btn.connect("clicked", lambda _b: self._capture_into(self.start_row))
        capture_box.append(self.set_start_btn)

        self.set_end_btn = Gtk.Button(label="Set End at Playhead")
        self.set_end_btn.add_css_class("pill")
        self.set_end_btn.connect("clicked", lambda _b: self._capture_into(self.end_row))
        capture_box.append(self.set_end_btn)

        group = Adw.PreferencesGroup()
        group.set_description(
            f"Full clip is {format_time(clip.duration)}. "
            "Accepts M:SS, H:MM:SS, or raw seconds. "
            "Leave End blank to use the end of the clip."
        )
        body.append(group)

        self.start_row = Adw.EntryRow()
        self.start_row.set_title("Start")
        if clip.trim_start > 0:
            self.start_row.set_text(format_time(clip.trim_start))
        group.add(self.start_row)

        self.end_row = Adw.EntryRow()
        self.end_row.set_title("End")
        if clip.trim_end is not None:
            self.end_row.set_text(format_time(clip.trim_end))
        group.add(self.end_row)

        reset_btn = Gtk.Button(label="Clear Trim")
        reset_btn.add_css_class("flat")
        reset_btn.add_css_class("pill")
        reset_btn.set_halign(Gtk.Align.CENTER)
        reset_btn.connect("clicked", self._on_reset)
        body.append(reset_btn)

        self.connect("closed", self._on_closed)

    def _capture_into(self, row: Adw.EntryRow):
        # Read the current playhead position. Accepting 0 is intentional —
        # the user may want to trim starting from the very beginning.
        ts_us = self.media.get_timestamp()
        if ts_us < 0:
            return
        row.set_text(format_time(ts_us / 1_000_000))

    def _on_media_error(self, *_):
        err = self.media.props.error
        if err is None:
            return
        self.video.set_visible(False)
        self.set_start_btn.set_sensitive(False)
        self.set_end_btn.set_sensitive(False)
        self.warning.set_visible(True)
        self.warning.set_label(
            "Can't play this file inside BitSplice — likely missing GStreamer codec "
            "plugins. You can still enter trim times manually below, or use the "
            "Preview button on the clip row to open it in your default video player."
        )

    def _on_closed(self, *_):
        try:
            self.media.pause()
            self.video.set_media_stream(None)
        except Exception:
            pass

    def _on_reset(self, *_):
        self._on_apply(0.0, None)
        self.close()

    def _on_apply_clicked(self, *_):
        start_text = self.start_row.get_text().strip()
        end_text = self.end_row.get_text().strip()

        start = parse_time(start_text) if start_text else 0.0
        end = parse_time(end_text) if end_text else None

        if start is None:
            self._error("Start time is not a valid time.")
            return
        if end_text and end is None:
            self._error("End time is not a valid time.")
            return
        if end is not None:
            if end > self._clip.duration + 0.05:
                self._error(f"End must be at most {format_time(self._clip.duration)}.")
                return
            if end <= start:
                self._error("End must be after Start.")
                return

        self._on_apply(start, end)
        self.close()

    def _error(self, msg: str):
        alert = Adw.AlertDialog(heading="Invalid Input", body=msg)
        alert.add_response("ok", "_OK")
        alert.present(self)


class ClipRow(Adw.ActionRow):
    def __init__(self, clip: Clip, window: "FuseWindow"):
        super().__init__()
        self.clip = clip
        self.window = window

        self.set_title(GLib.markup_escape_text(clip.name))
        self.set_subtitle(self._format_subtitle())

        # Thumbnail frame
        self.thumb_picture = Gtk.Picture()
        self.thumb_picture.set_size_request(112, 112)
        self.thumb_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.thumb_picture.set_valign(Gtk.Align.CENTER)
        self.thumb_picture.set_visible(False)
        self.add_prefix(self.thumb_picture)

        up_btn = Gtk.Button.new_from_icon_name("go-up-symbolic")
        up_btn.set_valign(Gtk.Align.CENTER)
        up_btn.add_css_class("flat")
        up_btn.set_tooltip_text("Move Up")
        up_btn.connect("clicked", lambda _b: self.window.move_clip(self, -1))
        self.add_prefix(up_btn)

        down_btn = Gtk.Button.new_from_icon_name("go-down-symbolic")
        down_btn.set_valign(Gtk.Align.CENTER)
        down_btn.add_css_class("flat")
        down_btn.set_tooltip_text("Move Down")
        down_btn.connect("clicked", lambda _b: self.window.move_clip(self, 1))
        self.add_prefix(down_btn)

        rot_model = Gtk.StringList.new([r[0] for r in ROTATIONS])
        self.rot_dropdown = Gtk.DropDown(model=rot_model)
        self.rot_dropdown.set_valign(Gtk.Align.CENTER)
        self.rot_dropdown.set_tooltip_text("Rotation")
        self.rot_dropdown.set_selected(0)
        self.rot_dropdown.connect("notify::selected", self._on_rotation_changed)
        self.add_suffix(self.rot_dropdown)

        self.trim_btn = Gtk.Button.new_from_icon_name("edit-cut-symbolic")
        self.trim_btn.set_valign(Gtk.Align.CENTER)
        self.trim_btn.add_css_class("flat")
        self.trim_btn.set_tooltip_text("Trim Clip")
        self.trim_btn.connect("clicked", lambda _b: self._open_trim_dialog())
        self.add_suffix(self.trim_btn)

        self.preview_btn = Gtk.Button()
        self.preview_btn.set_child(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        self.preview_btn.set_valign(Gtk.Align.CENTER)
        self.preview_btn.add_css_class("flat")
        self.preview_btn.set_tooltip_text("Preview")
        self.preview_btn.connect("clicked", lambda _b: self.window.preview_clip(self))
        self.add_suffix(self.preview_btn)

        del_btn = Gtk.Button.new_from_icon_name("edit-delete-symbolic")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        del_btn.add_css_class("destructive-action")
        del_btn.set_tooltip_text("Remove")
        del_btn.connect("clicked", lambda _b: self.window.remove_clip(self))
        self.add_suffix(del_btn)

        # Drag-and-drop reordering
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag_source)

        drop_target = Gtk.DropTarget.new(GObject.TYPE_BOOLEAN, Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("motion", self._on_drag_motion)
        drop_target.connect("leave", self._on_drag_leave)
        self.add_controller(drop_target)

    def _format_subtitle(self) -> str:
        audio = "audio" if self.clip.has_audio else "no audio"
        if self.clip.is_trimmed:
            end = self.clip.trim_end if self.clip.trim_end is not None else self.clip.duration
            time_part = f"{format_time(self.clip.trim_start)}–{format_time(end)}"
        else:
            mins = int(self.clip.duration) // 60
            secs = int(self.clip.duration) % 60
            time_part = f"{mins}:{secs:02d}"
        parts = []
        if self.clip.filmed_at:
            parts.append(self.clip.filmed_at.strftime("%-d %b %Y, %-I:%M %p"))
        parts += [f"{self.clip.width}×{self.clip.height}", time_part, audio]
        return " · ".join(parts)

    def _open_trim_dialog(self):
        TrimDialog(self.clip, on_apply=self._apply_trim).present(self.window)

    def _apply_trim(self, start: float, end: float | None):
        self.clip.trim_start = start
        self.clip.trim_end = end
        self.set_subtitle(self._format_subtitle())

    def _on_drag_prepare(self, source, x, y):
        self.window._dragged_row = self
        val = GObject.Value(GObject.TYPE_BOOLEAN, True)
        return Gdk.ContentProvider.new_for_value(val)

    def _on_drag_begin(self, source, drag):
        icon = Gtk.DragIcon.get_for_drag(drag)
        lbl = Gtk.Label(label=self.clip.name)
        lbl.add_css_class("card")
        lbl.set_margin_top(8)
        lbl.set_margin_bottom(8)
        lbl.set_margin_start(12)
        lbl.set_margin_end(12)
        icon.set_child(lbl)
        source.set_hotspot(0, 0)

    def _on_drop(self, target, value, x, y):
        dragged = self.window._dragged_row
        if dragged is None or dragged is self:
            return False
        self.window.reorder_clip(dragged, self)
        self.window._dragged_row = None
        self.remove_css_class("drop-target-highlight")
        return True

    def _on_drag_motion(self, target, x, y):
        self.add_css_class("drop-target-highlight")
        return Gdk.DragAction.MOVE

    def _on_drag_leave(self, target):
        self.remove_css_class("drop-target-highlight")

    def _on_rotation_changed(self, *_args):
        idx = self.rot_dropdown.get_selected()
        self.clip.rotation = ROTATIONS[idx][1]
        # Invalidate cached thumbnail so it regenerates with the new rotation
        old_thumb = thumbnail_path_for(self.clip)
        if old_thumb.exists():
            try:
                old_thumb.unlink()
            except OSError:
                pass
        self.window.load_thumbnail(self)


class Encoder(GObject.Object):
    __gsignals__ = {
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "done": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
    }

    def __init__(self):
        super().__init__()
        self.process: subprocess.Popen | None = None
        self.cancelled = False

    def start(self, clips: list[Clip], output_path: str):
        self.cancelled = False
        total_duration = sum(c.effective_duration for c in clips)
        cmd = build_ffmpeg_command(clips, output_path)
        threading.Thread(
            target=self._run, args=(cmd, total_duration), daemon=True,
        ).start()

    def cancel(self):
        self.cancelled = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def _run(self, cmd: list[str], total_duration: float):
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            GLib.idle_add(self.emit, "done", False,
                          "ffmpeg not found — install it and retry.")
            return

        # Drain stderr in a side thread to prevent pipe-buffer deadlock.
        # ffmpeg can write verbose output that fills the OS pipe buffer (~64 KB)
        # before stdout is exhausted, blocking both ends of the pipe.
        stderr_buf: list[str] = []
        def _drain_stderr():
            assert self.process.stderr is not None
            stderr_buf.extend(self.process.stderr)
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") and total_duration > 0:
                try:
                    out_ms = int(line.split("=", 1)[1])
                    frac = min(1.0, max(0.0, (out_ms / 1_000_000) / total_duration))
                    GLib.idle_add(self.emit, "progress", frac)
                except ValueError:
                    pass

        rc = self.process.wait()
        stderr_thread.join()

        if self.cancelled:
            GLib.idle_add(self.emit, "done", False, "Cancelled.")
        elif rc != 0:
            tail = "".join(stderr_buf).strip().splitlines()
            msg = "\n".join(tail[-4:]) if tail else "No details available."
            GLib.idle_add(self.emit, "done", False, f"Encoding failed.\n\n{msg}")
        else:
            GLib.idle_add(self.emit, "done", True, "")


class FuseWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(760, 620)

        self._rows: list[ClipRow] = []
        self._dragged_row: "ClipRow | None" = None
        self._output_path: str | None = None
        self.encoder = Encoder()
        self.encoder.connect("progress", self._on_progress)
        self.encoder.connect("done", self._on_done)

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.set_tooltip_text("Add Videos")
        add_btn.connect("clicked", self._on_add_clicked)
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append("About BitSplice", "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        empty = Adw.StatusPage(
            icon_name="video-x-generic-symbolic",
            title="No Videos",
            description="Add videos to begin stitching.",
        )
        empty_btn = Gtk.Button(label="Add Videos")
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.set_halign(Gtk.Align.CENTER)
        empty_btn.connect("clicked", self._on_add_clicked)
        empty.set_child(empty_btn)
        self.stack.add_named(empty, "empty")

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_propagate_natural_height(True)
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12, margin_bottom=12,
            margin_start=12, margin_end=12,
        )
        self.list_group = Adw.PreferencesGroup()
        self.list_group.set_title("Clips")
        self.list_group.set_description(
            "Clips are stitched top to bottom. Rotation and trim apply per clip."
        )
        outer.append(self.list_group)
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        clamp.set_child(outer)
        scroller.set_child(clamp)
        self.stack.add_named(scroller, "list")

        toolbar.set_content(self.stack)
        self.stack.set_visible_child_name("empty")

        bottom_clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        bottom = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=8, margin_bottom=8,
            margin_start=12, margin_end=12,
        )
        bottom_clamp.set_child(bottom)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.save_btn = Gtk.Button(label="Save…")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.add_css_class("pill")
        self.save_btn.set_hexpand(True)
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save_clicked)
        action_row.append(self.save_btn)

        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.add_css_class("destructive-action")
        self.cancel_btn.add_css_class("pill")
        self.cancel_btn.set_visible(False)
        self.cancel_btn.connect("clicked", lambda _b: self.encoder.cancel())
        action_row.append(self.cancel_btn)

        bottom.append(action_row)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_visible(False)
        bottom.append(self.progress)

        toolbar.add_bottom_bar(bottom_clamp)

    # ---- clip list management ----

    @property
    def clips(self) -> list[Clip]:
        return [r.clip for r in self._rows]

    def add_clip(self, clip: Clip):
        row = ClipRow(clip, self)
        self.list_group.add(row)
        self._rows.append(row)
        self._refresh()
        self.load_thumbnail(row)

    def _add_clip_at(self, clip: Clip, index: int):
        row = ClipRow(clip, self)
        for r in self._rows:
            self.list_group.remove(r)
        self._rows.insert(index, row)
        for r in self._rows:
            self.list_group.add(r)
        self._refresh()
        self.load_thumbnail(row)

    def _insert_clip_sorted(self, clip: Clip):
        if clip.filmed_at is None:
            self.add_clip(clip)
            return
        insert_at = len(self._rows)
        for i, row in enumerate(self._rows):
            existing = row.clip.filmed_at
            if existing is None or existing > clip.filmed_at:
                insert_at = i
                break
        self._add_clip_at(clip, insert_at)

    # ---- thumbnails ----

    def load_thumbnail(self, row: ClipRow):
        target = thumbnail_path_for(row.clip)
        if target.exists() and target.stat().st_size > 0:
            GLib.idle_add(self._set_thumbnail, row, str(target))
            return
        threading.Thread(
            target=self._thumbnail_worker, args=(row, target), daemon=True,
        ).start()

    def _thumbnail_worker(self, row: ClipRow, target: Path):
        cmd = build_thumbnail_command(row.clip, target)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
            GLib.idle_add(self._set_thumbnail, row, str(target))
        except Exception:
            pass

    def _set_thumbnail(self, row: ClipRow, path: str):
        try:
            row.thumb_picture.set_file(Gio.File.new_for_path(path))
            row.thumb_picture.set_visible(True)
        except Exception:
            pass

    # ---- reorder ----

    def reorder_clip(self, source_row: ClipRow, target_row: ClipRow):
        if source_row is target_row:
            return
        source_idx = self._rows.index(source_row)
        target_idx = self._rows.index(target_row)
        self._rows.pop(source_idx)
        self._rows.insert(target_idx, source_row)
        for r in self._rows:
            self.list_group.remove(r)
        for r in self._rows:
            self.list_group.add(r)

    # ---- preview ----

    def preview_clip(self, row: ClipRow):
        clip = row.clip
        if clip.rotation == 0 and not clip.is_trimmed and not settings["enhance"]:
            self._launch_in_player(clip.path)
            return
        target = preview_path_for(clip)
        if target.exists() and target.stat().st_size > 0:
            self._launch_in_player(str(target))
            return
        row.preview_btn.set_sensitive(False)
        row.preview_btn.set_tooltip_text("Preparing Preview…")
        spinner = Gtk.Spinner()
        spinner.start()
        row.preview_btn.set_child(spinner)
        threading.Thread(
            target=self._preview_worker, args=(row, clip, target), daemon=True,
        ).start()

    def _preview_worker(self, row: ClipRow, clip: Clip, target: Path):
        cmd = build_preview_command(clip, target)
        try:
            subprocess.run(cmd, check=True, capture_output=True,
                           text=True, timeout=600)
            GLib.idle_add(self._preview_done, row, str(target), None)
        except subprocess.CalledProcessError as e:
            tail = "\n".join(e.stderr.strip().splitlines()[-3:]) if e.stderr else str(e)
            GLib.idle_add(self._preview_done, row, None, tail)
        except subprocess.TimeoutExpired:
            GLib.idle_add(self._preview_done, row, None,
                          "Preview render timed out after 10 minutes.")

    def _preview_done(self, row: ClipRow, path: str | None, error: str | None):
        row.preview_btn.set_sensitive(True)
        row.preview_btn.set_tooltip_text("Preview")
        row.preview_btn.set_child(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        if error:
            self._show_error(f"Preview Failed\n\n{error}")
        elif path:
            self._launch_in_player(path)

    def _launch_in_player(self, path: str):
        gfile = Gio.File.new_for_path(path)
        launcher = Gtk.FileLauncher.new(gfile)
        launcher.launch(self, None, None)

    def remove_clip(self, row: ClipRow):
        self.list_group.remove(row)
        self._rows.remove(row)
        self._refresh()

    def move_clip(self, row: ClipRow, delta: int):
        idx = self._rows.index(row)
        new_idx = idx + delta
        if not 0 <= new_idx < len(self._rows):
            return
        self._rows[idx], self._rows[new_idx] = self._rows[new_idx], self._rows[idx]
        for r in self._rows:
            self.list_group.remove(r)
        for r in self._rows:
            self.list_group.add(r)

    def _refresh(self):
        self.stack.set_visible_child_name("list" if self._rows else "empty")
        self.save_btn.set_sensitive(bool(self._rows))

    # ---- actions ----

    def _on_add_clicked(self, _btn):
        dialog = Gtk.FileDialog(title="Add Videos")
        flt = Gtk.FileFilter()
        flt.set_name("Video Files")
        for ext in VIDEO_EXTS:
            flt.add_suffix(ext)
        all_flt = Gtk.FileFilter()
        all_flt.set_name("All Files")
        all_flt.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        filters.append(all_flt)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        new_clips: list[Clip] = []
        for i in range(files.get_n_items()):
            f: Gio.File = files.get_item(i)
            path = f.get_path()
            if not path:
                continue
            try:
                w, h, dur, has_audio, filmed_at = probe_clip(path)
            except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
                self._show_error(f"Could not read {Path(path).name}:\n{e}")
                continue
            new_clips.append(Clip(
                path=path, width=w, height=h,
                duration=dur, has_audio=has_audio, filmed_at=filmed_at,
            ))
        for clip in sorted(new_clips, key=lambda c: c.filmed_at or datetime.max.replace(tzinfo=timezone.utc)):
            self._insert_clip_sorted(clip)

    def _on_save_clicked(self, _btn):
        OutputDialog(on_save=self._start_encode).present(self)

    def _start_encode(self, output_path: str):
        self._output_path = output_path
        self.save_btn.set_sensitive(False)
        self.cancel_btn.set_visible(True)
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text("Starting…")
        self.encoder.start(self.clips, output_path)

    def _on_progress(self, _enc, frac: float):
        self.progress.set_fraction(frac)
        self.progress.set_text(f"Encoding… {int(frac * 100)}%")

    def _on_done(self, _enc, success: bool, message: str):
        self.cancel_btn.set_visible(False)
        self.progress.set_visible(False)
        self.save_btn.set_sensitive(bool(self._rows))
        if success:
            self._show_info(f"Saved to {Path(self._output_path).name}")
        else:
            self._show_error(message or "Encoding failed.")

    def _show_error(self, msg: str):
        dialog = Adw.AlertDialog(heading="Error", body=msg)
        dialog.add_response("ok", "_OK")
        dialog.present(self)

    def _show_info(self, msg: str):
        dialog = Adw.AlertDialog(heading="Done", body=msg)
        dialog.add_response("ok", "_OK")
        dialog.present(self)


class FuseApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = FuseWindow(self)
        win.present()

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=APP_VERSION,
            developer_name="thrillho93",
            comments="Stitch, rotate, and reencode video files.",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self.props.active_window)


def main() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("Error: ffmpeg and ffprobe must be installed.", file=sys.stderr)
        return 1
    app = FuseApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
