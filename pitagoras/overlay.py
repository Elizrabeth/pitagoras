"""The floating character: a yellow circle with a mouth, always on top.

Left click   -> chat box (type to it)          Drag  -> move it
Right click  -> menu (brain, ears, eyes, voice, …)

It's a separate process that talks to Pitagoras over the same WebSocket
protocol your own character app can use (see README), so it can be replaced.
Run alone:  python -m pitagoras.overlay
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from collections import OrderedDict

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QPropertyAnimation, QRectF, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import (QApplication, QColorDialog, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QVBoxLayout, QWidget)

POPUP_FLAGS = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
WANDER_SECONDS = (90, 300)  # idle time before it teleports somewhere else (random in this range)


# ── colors (appearance.json) ────────────────────────────────
DEFAULT_COLORS = {"body": [255, 210, 63], "mouth": [21, 21, 21], "outline": [120, 85, 0],
                  "listening_ring": [90, 209, 154]}
COLOR_HELP = ("Pitagoras' colors. Each one is [r, g, b] (0-255), \"#rrggbb\" or \"rgb(r, g, b)\". "
              "Saved changes show up right away. Or use right-click > Settings > Colors.")


def appearance_path():
    from .config import ROOT

    return ROOT / "appearance.json"


def parse_color(v) -> QColor | None:
    try:
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return QColor(*[max(0, min(255, int(c))) for c in v[:3]])
        if isinstance(v, dict):
            return QColor(int(v.get("r", 0)), int(v.get("g", 0)), int(v.get("b", 0)))
        if isinstance(v, str):
            m = re.match(r"\s*rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", v, re.I)
            if m:
                return QColor(*[min(255, int(x)) for x in m.groups()])
            c = QColor(v.strip())
            return c if c.isValid() else None
    except (TypeError, ValueError):
        pass
    return None


def read_appearance() -> dict:
    path = appearance_path()
    if not path.exists():
        write_appearance(DEFAULT_COLORS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write_appearance(values: dict) -> None:
    data = {"_help": COLOR_HELP}
    data.update({k: v for k, v in values.items() if not k.startswith("_")})
    lines = [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in data.items()]  # one color per line
    appearance_path().write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")


def load_colors() -> dict[str, QColor]:
    data = read_appearance()
    cols = {k: parse_color(data.get(k)) or parse_color(d) for k, d in DEFAULT_COLORS.items()}
    body = cols["body"]
    # shading for the 3D look, derived from the body color
    cols["body_light"] = QColor(*(int(c + (255 - c) * 0.6) for c in (body.red(), body.green(), body.blue())))
    cols["body_dark"] = body.darker(118)
    return cols


def ease_out_back(u: float) -> float:
    c1 = 1.9
    return 1 + (c1 + 1) * (u - 1) ** 3 + c1 * (u - 1) ** 2

MENU_CSS = """
QMenu { background: #1f2329; color: #e9edf2; border: 1px solid #3a414b; border-radius: 8px; padding: 4px; font-size: 10pt; }
QMenu::item { padding: 6px 22px 6px 24px; border-radius: 5px; }
QMenu::item:selected { background: #39424e; }
QMenu::item:disabled { color: #8a94a3; }
QMenu::separator { height: 1px; background: #3a414b; margin: 4px 8px; }
QMenu::indicator { width: 14px; height: 14px; left: 6px; }
"""


# ── connection to the backend ───────────────────────────────
class Link(QObject):
    message = Signal(dict)
    status = Signal(bool)

    def __init__(self, url: str):
        super().__init__()
        self.url, self.ok = url, False
        self.ws = QWebSocket()
        self.ws.connected.connect(self._up)
        self.ws.disconnected.connect(self._down)
        self.ws.textMessageReceived.connect(self._text)
        self.retry = QTimer(self, singleShot=True, interval=1500, timeout=self.open)
        self.open()

    def open(self):
        self.ws.open(QUrl(self.url))

    def _up(self):
        self.ok = True
        self.status.emit(True)
        self.send("get_settings")

    def _down(self):
        if self.ok:
            self.status.emit(False)
        self.ok = False
        self.retry.start()

    def _text(self, raw: str):
        try:
            self.message.emit(json.loads(raw))
        except ValueError:
            pass

    def send(self, type_: str, **data):
        if self.ok:
            self.ws.sendTextMessage(json.dumps({"type": type_, **data}))


# ── popups ──────────────────────────────────────────────────
class Bubble(QWidget):
    """Speech bubble above the character."""

    def __init__(self):
        super().__init__(None, POPUP_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setMaximumWidth(320)
        self.label.setStyleSheet(
            "QLabel { background: rgba(255,255,255,240); color: #1d1f23; border-radius: 12px;"
            " padding: 9px 12px; font: 10.5pt 'Segoe UI'; border: 1px solid rgba(0,0,0,40); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.label)
        self.hide_timer = QTimer(self, singleShot=True, timeout=self.hide)

    def show_text(self, text: str, italic: bool = False):
        text = text.strip()
        if len(text) > 600:
            text = "…" + text[-600:]
        self.label.setText(text)
        f = self.label.font()
        f.setItalic(italic)
        self.label.setFont(f)
        self.hide_timer.stop()
        self.adjustSize()
        self.show()

    def hide_later(self, ms: int):
        self.hide_timer.start(ms)


class TimerPanel(QWidget):
    """Running timers (cooldowns, reminders) under the character."""

    def __init__(self):
        super().__init__(None, POPUP_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setStyleSheet(
            "QLabel { background: rgba(31,35,41,225); color: #ffe27a; border-radius: 9px; padding: 5px 10px;"
            " font: bold 10pt 'Segoe UI'; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.label)
        self.timers: list[tuple[str, float]] = []  # (label, ends at monotonic)

    def set_timers(self, items: list[dict]):
        now = time.monotonic()
        self.timers = [(t["label"], now + float(t["left"])) for t in items]
        self.refresh()

    def refresh(self) -> bool:
        """Update the countdowns; returns True if visibility/size changed."""
        now = time.monotonic()
        self.timers = [(lbl, end) for lbl, end in self.timers if end > now - 1]
        was = self.isVisible()
        if not self.timers:
            self.hide()
            return was
        lines = []
        for lbl, end in self.timers[:5]:
            s = max(0, int(end - now + 0.999))
            lines.append(f"⏱ {lbl}  {s // 60}:{s % 60:02d}")
        if len(self.timers) > 5:
            lines.append(f"+{len(self.timers) - 5} more")
        text = "\n".join(lines)
        if text != self.label.text():
            old = self.size()
            self.label.setText(text)
            self.adjustSize()
            if not was:
                self.show()
            return not was or self.size() != old
        return False


class ChatBox(QWidget):
    def __init__(self, on_send):
        super().__init__(None, POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("Say something to Pitagoras…  (Enter to send, Esc to close)")
        self.edit.setMinimumWidth(330)
        self.edit.setStyleSheet(
            "QLineEdit { background: #1f2329; color: #e9edf2; border: 2px solid #f2c230; border-radius: 14px;"
            " padding: 7px 12px; font: 10.5pt 'Segoe UI'; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.edit)
        self.edit.returnPressed.connect(self._send)
        self.on_send = on_send

    def _send(self):
        t = self.edit.text().strip()
        if t:
            self.on_send(t)
            self.edit.clear()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def popup(self):
        self.adjustSize()
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()


# ── the character ───────────────────────────────────────────
class Character(QWidget):
    def __init__(self, port: int, size: int = 150):
        super().__init__(None, POPUP_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Pitagoras")
        self.S = size
        self.setFixedSize(int(size * 1.5), int(size * 1.75))  # room to rock and jump

        self.link = Link(f"ws://127.0.0.1:{port}")
        self.link.message.connect(self.on_message)
        self.link.status.connect(lambda ok: self.update())
        self.settings: dict = {}

        # animation state
        self.state = "loading"
        self.mouth_t = self.mouth = 0.0
        self.mic = 0.0
        self.user_speaking = False
        self.emotion, self.emotion_until = "neutral", 0.0
        self.music_level = 0.0
        self.last_music = 0.0
        self.beat = 0.0
        self.bpm = 0
        self.phase = 0.0
        self.playing = False
        self.t0 = time.monotonic()
        self.last_tick = self.t0
        self.following_until = 0.0
        self.reply = ""
        # current hop: hop_u goes 0 -> 1 (takeoff -> landing) at hop_rate per second; 1 = on the ground
        self.hop_u, self.hop_rate, self.jump_amp = 1.0, 3.0, 0.0
        self.pending_hop: tuple[float, float] | None = None  # (height, duration) waiting for the landing
        self.hops = 0
        self.bass_level = 0.0
        self.energy = 0.0      # smoothed loudness: how big the moves are
        self.groove = 0.0      # 0..1 how steady the beat is right now (rises on beats, fades without)
        self.sway_amp = 0.0    # current rocking angle (degrees)
        self.last_beat_t = -10.0

        # teleporting around when nothing's going on
        self.last_interaction = self.t0
        self.next_wander = random.uniform(*WANDER_SECONDS)
        self.tp: dict | None = None   # {"stage": "out"/"in", "t0": ..., "target": QPoint}
        self.pop = 1.0                # body scale while teleporting
        self.poof_t0 = -10.0

        self.bubble = Bubble()
        self.timer_panel = TimerPanel()
        self.chat = ChatBox(lambda t: self.link.send("say", text=t))
        self.anim: QPropertyAnimation | None = None
        self._press: QPoint | None = None
        self._start = QPoint()
        self._dragged = False

        self.store = QSettings("Pitagoras", "overlay")
        self.wander = self.store.value("wander", True, type=bool)
        self.colors = load_colors()
        self.colors_mtime = self._appearance_mtime()
        QTimer(self, interval=1000, timeout=self.check_appearance).start()  # pick up edits to the file
        pos = self.store.value("pos")
        if isinstance(pos, QPoint) and QApplication.screenAt(pos + QPoint(10, 10)):
            self.move(pos)
        else:
            self.reset_position()

        self.timer = QTimer(self, interval=16, timeout=self.tick)
        self.timer.start()
        QTimer(self, interval=5000, timeout=self.raise_).start()  # stay above newly opened windows

    # ── messages ────────────────────────────────────────────
    def on_message(self, m: dict):
        t = m.get("type")
        if t == "mouth":
            self.mouth_t = float(m.get("level", 0))
        elif t == "mic":
            self.mic = float(m.get("level", 0))
        elif t == "music":
            self.music_level = float(m.get("level", 0))
            self.bass_level = float(m.get("bass", 0))
            if self.music_level > 0.05:
                self.last_music = time.monotonic()
            if m.get("beat") and self.playing:  # only dance to music, not to game/video noise
                self.on_beat(float(m.get("strength", 0.6)), m.get("kind") or "kick")
            if m.get("bpm"):
                self.bpm = int(m["bpm"])
        elif t == "state":
            self.state = m.get("state", "idle")
            if self.state == "idle" and self.reply:
                self.bubble.hide_later(6000 + 40 * len(self.reply))
        elif t == "user_speaking":
            self.user_speaking = bool(m.get("active"))
            self.touch()
        elif t == "emotion":
            self.emotion = m.get("emotion", "neutral")
            self.emotion_until = time.monotonic() + (20 if self.emotion == "dance" else 8)
        elif t == "move":
            self.move_to(m.get("position", "center"))
        elif t == "now_playing":
            self.playing = m.get("status") == "playing"
        elif t == "assistant_start":
            self.reply = ""
        elif t == "user_text":
            self.touch()
        elif t == "assistant_text":
            self.touch()
            self.reply += m.get("delta", "")
            if self.reply.strip():
                self.bubble.show_text(self.reply)
                self.place_popups()
        elif t == "assistant_end":
            if self.reply.strip():
                self.bubble.hide_later(8000 + 40 * len(self.reply))
        elif t == "toast":
            if not self.bubble.isVisible() or not self.reply:
                self.bubble.show_text(m.get("text", ""), italic=True)
                self.place_popups()
                self.bubble.hide_later(4000)
                self.reply = ""
        elif t == "error":
            self.bubble.show_text("⚠ " + m.get("message", "error")[:200], italic=True)
            self.place_popups()
            self.bubble.hide_later(6000)
        elif t == "settings":
            self.settings = m
        elif t == "timers":
            self.timer_panel.set_timers(m.get("timers") or [])
            self.place_popups()
        self.update()

    # ── animation ───────────────────────────────────────────
    def music_on(self) -> bool:
        return self.playing and time.monotonic() - self.last_music < 1.5

    def on_beat(self, strength: float = 0.6, kind: str = "kick"):
        """A hit in the music: kick (bass), hat (hi-hats/cymbals) or drop (sudden loud part).
        Drops -> huge hop, kicks -> big hops, hats -> smaller quick hops."""
        now = time.monotonic()
        beat_len = 60 / (self.bpm or 110)
        if kind == "drop":
            strength = 1.0
            self.energy = max(self.energy, 0.9)
            self.groove = 1.0
        self.beat = (0.3 + 0.7 * strength) * (0.6 if kind == "hat" else 1.0)  # squash on the hit
        self.groove = min(1.0, self.groove + (0.12 if kind == "hat" else 0.3) * min(1.0, strength * 1.6))
        self.last_beat_t = now
        loud = max(self.energy, self.bass_level)
        if kind == "drop":
            hop, dur = 1.35, 0.5
        elif kind == "hat":
            hop, dur = (0.25 + 0.45 * strength) * (0.6 + 0.4 * loud), max(0.18, min(0.3, beat_len * 0.5))
        else:
            hop, dur = (0.4 + 0.6 * strength) * (0.6 + 0.4 * loud), max(0.22, min(0.42, beat_len * 0.7))
        if strength > 0.1:
            if self.hop_u >= 1:  # on the ground: jump now
                self.start_hop(hop, dur)
            else:
                # still in the air: finish this hop fast (land quickly), then do the new one
                fast = 0.08 if kind == "drop" else 0.1  # ~6 frames: quick, but still visibly comes down
                left = 1 - self.hop_u
                if left / self.hop_rate > fast:
                    self.hop_rate = left / fast
                if self.pending_hop is None or hop > self.pending_hop[0]:
                    self.pending_hop = (hop, dur)
        if kind == "hat":
            return  # hats are too fast to steer the rocking; kicks keep it on the beat
        # lock the rocking to the beat: every beat should hit a left/right extreme
        k = round(self.phase / math.pi - 0.5)
        target = (k + 0.5) * math.pi
        self.phase += (target - self.phase) * 0.6

    def start_hop(self, height: float, dur: float):
        self.hop_u, self.hop_rate, self.jump_amp = 0.0, 1 / dur, height
        self.hops += 1

    def tick_hop(self, dt: float):
        if self.hop_u >= 1:
            return
        self.hop_u += dt * self.hop_rate
        if self.hop_u >= 1:  # landed
            self.hop_u = 1.0
            self.beat = max(self.beat, 0.35 * self.jump_amp)  # little squash on landing
            if self.pending_hop:
                self.start_hop(*self.pending_hop)
                self.pending_hop = None

    def touch(self):
        """Something happened (talk, click…): reset the idle-wander timer."""
        self.last_interaction = time.monotonic()

    def dancing(self) -> bool:
        return self.music_on() or self.emotion == "dance"

    def tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last_tick)
        self.last_tick = now
        self.mouth += (self.mouth_t - self.mouth) * min(1.0, dt * 25)
        self.beat *= math.exp(-dt * 7)
        self.mic *= math.exp(-dt * 4)
        self.tick_hop(dt)
        bpm = self.bpm or 110
        music = self.music_on()
        fake = self.emotion == "dance" and not music  # dancing without music: keep our own beat
        if fake and now - self.last_beat_t > 60 / bpm:
            self.on_beat(0.55)
        # how big the moves are follows the loudness (fast up, slower down)
        target = self.music_level if music else (0.6 if fake else 0.0)
        self.energy += (target - self.energy) * min(1.0, dt * (8 if target > self.energy else 2.5))
        if now - self.last_beat_t > 60 / bpm * 1.5:  # no kicks lately: the groove fades
            self.groove *= math.exp(-dt / 1.2)
        # always rock to music: bigger when loud, on the beat when there is one,
        # slow and gentle for soft songs/vocals
        want = (8 + 16 * self.energy) * (0.6 + 0.4 * self.groove) if music or fake else 0.0
        self.sway_amp += (want - self.sway_amp) * min(1.0, dt * 3)
        if self.groove > 0.3:
            self.phase += dt * bpm / 60 * math.pi  # half a rock (left->right) per beat
        else:
            self.phase += dt * 1.6                  # slow drift
        if now > self.emotion_until and self.emotion != "neutral":
            self.emotion = "neutral"
        if now < self.following_until:
            target = QCursor.pos() + QPoint(30, 30)
            cur = self.pos()
            self.move(cur + (target - cur) * 0.12)
        self.tick_teleport(now)
        if self.timer_panel.timers and int(now * 4) != int((now - dt) * 4):  # countdown: 4x a second
            if self.timer_panel.refresh():
                self.place_popups()
        self.update()

    # ── teleporting ─────────────────────────────────────────
    def idle_for_wander(self, now: float) -> bool:
        return (self.wander and self.link.ok and self.tp is None and self.state == "idle"
                and now - self.last_interaction > self.next_wander and now > self.following_until
                and not (self.dancing() or self.user_speaking or self.bubble.isVisible() or self.chat.isVisible()
                         or self._press is not None or (self.anim and self.anim.state() == QPropertyAnimation.Running)))

    def random_spot(self) -> QPoint:
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        w, h, m = self.width(), self.height(), 10
        x0, x1, y0, y1 = scr.left() + m, scr.right() - w - m, scr.top() + m, scr.bottom() - h - m
        best = QPoint(x0, y1)
        for _ in range(12):  # somewhere new, and preferably near an edge (out of the way)
            x, y = random.randint(x0, max(x0, x1)), random.randint(y0, max(y0, y1))
            if random.random() < 0.75:
                if random.random() < 0.5:
                    x = random.choice([random.randint(x0, x0 + w), random.randint(max(x0, x1 - w), x1)])
                else:
                    y = random.choice([random.randint(y0, y0 + h // 2), random.randint(max(y0, y1 - h), y1)])
            best = QPoint(x, y)
            if (best - self.pos()).manhattanLength() > 400:
                break
        return best

    def teleport(self, target: QPoint | None = None):
        if self.tp is None:
            self.following_until = 0
            if self.anim:
                self.anim.stop()
            self.tp = {"stage": "out", "t0": time.monotonic(), "target": target or self.random_spot()}

    def tick_teleport(self, now: float):
        if self.tp is None:
            if self.idle_for_wander(now):
                self.teleport()
            return
        t = now - self.tp["t0"]
        if self.tp["stage"] == "out":  # shrink into a poof
            u = min(1.0, t / 0.22)
            self.pop = 1 - u * u
            if u >= 1:
                self.move(self.tp["target"])
                self.tp = {"stage": "in", "t0": now}
                self.poof_t0 = now
        else:  # pop out at the new spot
            u = min(1.0, t / 0.38)
            self.pop = max(0.0, ease_out_back(u))
            if u >= 1:
                self.pop, self.tp = 1.0, None
                self.report_position()
                self.touch()
                self.next_wander = random.uniform(*WANDER_SECONDS)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        mono = time.monotonic()
        now = mono - self.t0
        W, H = self.width(), self.height()
        r = self.S * 0.42
        cx, cy = W / 2, H * 0.62

        sway = math.sin(self.phase) * self.sway_amp  # degrees, rocking left/right
        jump = math.sin(math.pi * self.hop_u) * self.jump_amp if self.hop_u < 1 else 0.0  # hop arc
        bounce = -jump * self.S * 0.3
        breathe = math.sin(now * 2.2) * 0.015
        pump = 0.05 * self.bass_level * self.energy if self.music_on() else 0  # swells with the bass
        # squash when the kick hits, stretch tall in the air
        squash = self.beat * 0.13 * max(0.3, self.energy) - jump * 0.08
        rx, ry = r * (1 + breathe + pump + squash), r * (1 + breathe + pump - squash)

        # poof where it just appeared
        pt = mono - self.poof_t0
        if pt < 0.5:
            a = int(160 * (1 - pt / 0.5))
            p.setBrush(Qt.NoBrush)
            ring, spark = QColor(self.colors["body"]), QColor(self.colors["body_light"])
            ring.setAlpha(a)
            spark.setAlpha(a)
            for i in range(3):
                p.setPen(QPen(ring, 3 - i))
                p.drawEllipse(QPointF(cx, cy), r * (0.6 + pt * 2.2 + i * 0.25), r * (0.6 + pt * 2.2 + i * 0.25))
            p.setPen(Qt.NoPen)
            p.setBrush(spark)
            for i in range(8):
                ang = i * math.pi / 4 + 0.3
                d = r * (0.8 + pt * 2.6)
                p.drawEllipse(QPointF(cx + math.cos(ang) * d, cy + math.sin(ang) * d), 4, 4)

        if self.pop <= 0.01:
            return

        # shadow (shrinks while in the air)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, int(45 * (1 - jump * 0.5) * min(1.0, self.pop))))
        p.drawEllipse(QPointF(cx, cy + r + 8), r * 0.75 * (1 - jump * 0.35) * self.pop, r * 0.12 * self.pop)

        # rock around the bottom of the body, like it's standing on the ground
        p.translate(cx, cy + bounce + ry)
        p.rotate(sway)
        p.translate(0, -ry)
        p.scale(self.pop, self.pop)

        # listening ring
        if self.user_speaking:
            ring = QColor(self.colors["listening_ring"])
            ring.setAlpha(int(90 + 140 * min(1.0, self.mic * 2)))
            p.setPen(QPen(ring, 4))
            p.setBrush(Qt.NoBrush)
            grow = 6 + 8 * self.mic
            p.drawEllipse(QPointF(0, 0), rx + grow, ry + grow)

        # body
        g = QRadialGradient(QPointF(-r * 0.35, -r * 0.4), r * 1.6)
        if self.link.ok:  # shaded from appearance.json's body color
            g.setColorAt(0, self.colors["body_light"])
            g.setColorAt(0.45, self.colors["body"])
            g.setColorAt(1, self.colors["body_dark"])
        else:  # grey = not connected
            g.setColorAt(0, QColor("#eeeeee"))
            g.setColorAt(1, QColor("#9a9a9a"))
        p.setBrush(g)
        outline = QColor(self.colors["outline"])
        outline.setAlpha(120)
        p.setPen(QPen(outline, 1.5))
        p.drawEllipse(QPointF(0, 0), rx, ry)

        self.draw_mouth(p, r)
        p.resetTransform()

        # thinking / loading dots
        if self.state in ("thinking", "working", "loading"):
            col = QColor(60, 60, 60) if self.state != "loading" else QColor(150, 150, 150)
            for i in range(3):
                a = 0.35 + 0.65 * max(0.0, math.sin(now * 5 - i * 0.7))
                col.setAlphaF(a)
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(QPointF(cx - 14 + i * 14, cy - r - 16 + bounce), 4.5, 4.5)
        if self.emotion == "sleepy":
            p.setPen(QColor(60, 60, 60))
            p.setFont(QFont("Segoe UI", int(self.S / 10), QFont.Bold))
            p.drawText(QPointF(cx + r * 0.6, cy - r * 0.8), "z z")

    def draw_mouth(self, p: QPainter, r: float):
        mx, my, hw = 0.0, r * 0.3, r * 0.34
        pen = QPen(self.colors["mouth"], max(3.0, r * 0.075), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        m = self.mouth
        if m > 0.04:  # talking: the line opens up
            h = r * 0.05 + m * r * 0.42
            w = hw * (0.8 + 0.2 * m)
            p.setBrush(self.colors["mouth"])
            p.drawRoundedRect(QRectF(mx - w, my - h / 2, 2 * w, h), h / 2, h / 2)
            return
        e = self.emotion
        if self.music_on() and e == "neutral":
            e = "dance"
        if e == "surprised":
            p.drawEllipse(QPointF(mx, my), hw * 0.32, hw * 0.4)
            return
        if e == "thinking":
            p.drawLine(QPointF(mx - hw * 0.5, my + 3), QPointF(mx + hw * 0.6, my - 5))
            return
        curve = {"happy": 0.22, "excited": 0.3, "love": 0.24, "dance": 0.28, "sad": -0.2, "angry": -0.1,
                 "sleepy": 0.0, "neutral": 0.05}.get(e, 0.05) * r
        w = hw * (0.6 if e == "sleepy" else 1.0)
        path = QPainterPath(QPointF(mx - w, my))
        path.quadTo(QPointF(mx, my + curve * 2), QPointF(mx + w, my))
        p.drawPath(path)

    # ── positioning ─────────────────────────────────────────
    def place_popups(self):
        g = self.frameGeometry()
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        if self.bubble.isVisible():
            b = self.bubble
            x = g.center().x() - b.width() // 2
            y = g.top() - b.height() + 10 + int(self.S * 0.28)  # the top of the window is jump room
            if y < scr.top():
                y = g.bottom() - 4
            b.move(max(scr.left() + 4, min(x, scr.right() - b.width() - 4)), y)
        below = g.bottom() - 6
        if self.chat.isVisible():
            c = self.chat
            x = g.center().x() - c.width() // 2
            y = below
            if y + c.height() > scr.bottom():
                y = g.top() - c.height() - (self.bubble.height() if self.bubble.isVisible() else 0)
            else:
                below = y + c.height()
            c.move(max(scr.left() + 4, min(x, scr.right() - c.width() - 4)), y)
        if self.timer_panel.isVisible():
            tp = self.timer_panel
            x = g.center().x() - tp.width() // 2
            y = below
            if y + tp.height() > scr.bottom():  # no room below: to the side
                x = g.left() - tp.width() if g.left() - tp.width() > scr.left() else g.right()
                y = g.bottom() - tp.height() - 10
            tp.move(max(scr.left() + 4, min(x, scr.right() - tp.width() - 4)), y)

    def moveEvent(self, e):
        self.place_popups()
        super().moveEvent(e)

    def report_position(self):
        c = self.frameGeometry().center()
        self.link.send("where", x=c.x(), y=c.y())
        self.store.setValue("pos", self.pos())

    def reset_position(self):
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 30, scr.bottom() - self.height() - 20)
        self.report_position()

    def move_to(self, where: str):
        self.touch()
        if where in ("teleport", "random", "anywhere"):
            self.teleport()
            return
        if where == "follow_mouse":
            self.following_until = time.monotonic() + 15
            return
        self.following_until = 0
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        w, h, m = self.width(), self.height(), 20
        xs = {"left": scr.left() + m, "right": scr.right() - w - m, "center": scr.center().x() - w // 2}
        ys = {"top": scr.top() + m, "bottom": scr.bottom() - h - m, "center": scr.center().y() - h // 2}
        parts = where.replace("_", "-").split("-")
        x, y = xs["center"], ys["center"]
        for part in parts:
            if part in ("left", "right"):
                x = xs[part]
            elif part in ("top", "bottom"):
                y = ys[part]
        self.anim = QPropertyAnimation(self, b"pos", self)
        self.anim.setDuration(900)
        self.anim.setEasingCurve(QEasingCurve.OutBack)
        self.anim.setEndValue(QPoint(int(x), int(y)))
        self.anim.finished.connect(self.report_position)
        self.anim.start()

    # ── mouse ───────────────────────────────────────────────
    def mousePressEvent(self, e):
        self.touch()
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._start = self.pos()
            self._dragged = False
            self.following_until = 0

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            d = e.globalPosition().toPoint() - self._press
            if self._dragged or d.manhattanLength() > 5:
                self._dragged = True
                self.move(self._start + d)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press is not None:
            if self._dragged:
                self.report_position()
            else:
                self.toggle_chat()
            self._press = None

    def toggle_chat(self):
        if self.chat.isVisible():
            self.chat.hide()
        else:
            self.chat.popup()
            self.place_popups()

    def contextMenuEvent(self, e):
        self.link.send("get_settings")  # refresh for next time (models may have appeared)
        menu = self.build_menu()
        menu.exec(e.globalPos())

    # ── menu ────────────────────────────────────────────────
    def _send_set(self, key, value):
        self.link.send("set", key=key, value=value)

    def _radio(self, menu: QMenu, items: list[dict], current, key: str, submenus: bool = False, mark_vision=False):
        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for it in items or []:
            groups.setdefault(it.get("group", ""), []).append(it)
        ag = QActionGroup(menu)
        ag.setExclusive(True)
        first = True
        for gname, its in groups.items():
            target = menu
            if submenus and gname:
                target = menu.addMenu(gname + ("  ●" if any(i["id"] == current for i in its) else ""))
                target.setStyleSheet(MENU_CSS)
            elif not first:
                menu.addSeparator()
            first = False
            for it in its:
                label = it["label"] + ("   👁" if mark_vision and it.get("vision") else "")
                a = QAction(label, target, checkable=True, checked=it["id"] == current)
                a.triggered.connect(lambda _=False, v=it["id"]: self._send_set(key, v))
                ag.addAction(a)
                target.addAction(a)

    def build_menu(self) -> QMenu:
        m = QMenu(self)
        m.setStyleSheet(MENU_CSS)
        cur = self.settings.get("current", {})
        opt = self.settings.get("options", {})
        if not self.link.ok:
            m.addAction("Not connected to Pitagoras (is it running?)").setEnabled(False)
            m.addSeparator()
            m.addAction("Quit character", QApplication.quit)
            return m

        m.addAction("💬  Chat", self.toggle_chat)
        look = m.addAction("👀  Look at my screen", lambda: self.link.send("look"))
        if not cur.get("brain_vision"):
            look.setText("👀  Look at my screen (this brain can't see)")
            look.setEnabled(False)
        m.addAction("✋  Stop talking", lambda: self.link.send("interrupt"))
        watch = QAction("📺  Watch along with me", m, checkable=True, checked=bool(cur.get("watching")))
        watch.triggered.connect(lambda c: self._send_set("watch", c))
        m.addAction(watch)
        m.addSeparator()

        status = cur.get("brain_status", "")
        title = cur.get("brain_label") or "none"
        if status == "loading":
            title += " (loading…)"
        elif status.startswith("error"):
            title += " ⚠"
        bm = m.addMenu(f"🧠  Brain: {title}")
        bm.setStyleSheet(MENU_CSS)
        brains = opt.get("brains", [])
        if not brains:
            bm.addAction("No brains found").setEnabled(False)
        self._radio(bm, brains, cur.get("brain"), "brain", submenus=True, mark_vision=True)
        bm.addSeparator()
        if status.startswith("error"):
            bm.addAction("⚠ " + status[7:90]).setEnabled(False)
        bm.addAction("Custom server…", self.custom_server)
        bm.addAction("Open brains folder", lambda: self.link.send("open_brains_folder"))
        bm.addAction("Rescan", lambda: self.link.send("get_settings"))
        bm.addAction("👁 = can see the screen").setEnabled(False)

        em = m.addMenu("👂  Ears")
        em.setStyleSheet(MENU_CSS)
        self._radio(em, opt.get("ears", []), cur.get("ears"), "ears")
        es = cur.get("ears_status", "")
        if es.startswith("error"):
            em.addAction("⚠ " + es[7:90]).setEnabled(False)
        em.addSeparator()
        mics = [{"id": None, "label": "Default microphone"}] + [{"id": n, "label": n} for n in opt.get("mics", [])]
        mm = em.addMenu("Microphone")
        mm.setStyleSheet(MENU_CSS)
        self._radio(mm, mics, cur.get("mic"), "mic")

        ey = m.addMenu("👁  Eyes")
        ey.setStyleSheet(MENU_CSS)
        self._radio(ey, opt.get("eyes", []), cur.get("eyes"), "eyes")
        if not cur.get("brain_vision"):
            ey.addSeparator()
            ey.addAction("The current brain can't see; pick one marked 👁").setEnabled(False)

        vm = m.addMenu("🔊  Voice")
        vm.setStyleSheet(MENU_CSS)
        mute = QAction("Muted (bubble only)", vm, checkable=True, checked=bool(cur.get("muted")))
        mute.triggered.connect(lambda c: self._send_set("muted", c))
        vm.addAction(mute)
        vm.addSeparator()
        self._radio(vm, opt.get("voices", []), cur.get("voice"), "voice", submenus=True)
        vm.addAction("Fish Audio setup…", self.fish_setup)
        vm.addSeparator()
        # the base voice, before the voice model
        sp = self._sub(vm, f"Speed  ({cur.get('tts_speed', 1.0):g}×)")
        self._radio(sp, [{"id": v, "label": f"{v:g}×" + (" (normal)" if v == 1.0 else "")}
                         for v in (0.75, 0.85, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5)], float(cur.get("tts_speed", 1.0)), "tts_speed")
        pt = self._sub(vm, f"Pitch  ({cur.get('tts_pitch', 0):+g})")
        self._radio(pt, [{"id": float(v), "label": f"{v:+d} semitones" + (" (normal)" if v == 0 else "")}
                         for v in (-6, -4, -2, -1, 0, 1, 2, 4, 6)], float(cur.get("tts_pitch", 0)), "tts_pitch")
        vo = self._sub(vm, f"Volume  ({cur.get('volume', 100):g}%)")
        self._radio(vo, [{"id": float(v), "label": f"{v}%"} for v in (25, 50, 75, 100, 125, 150)],
                    float(cur.get("volume", 100)), "volume")
        vm.addAction("Speed and pitch change the voice before the voice model").setEnabled(False)

        rs = cur.get("rvc_status", "off")
        rv = m.addMenu(f"🎙  Voice model: {cur.get('rvc') or 'off'}" + ("  ⏳" if rs == "loading" else ""))
        rv.setStyleSheet(MENU_CSS)
        rv.addAction("Turns the voice into the voice model's voice (RVC)").setEnabled(False)
        if rs == "loading":
            rv.addAction("⏳ loading…").setEnabled(False)
        elif rs.startswith("error"):
            rv.addAction("⚠ " + rs[7:100]).setEnabled(False)
        rv.addSeparator()
        self._radio(rv, opt.get("rvc", []), cur.get("rvc", ""), "rvc")
        rv.addSeparator()
        pm = rv.addMenu("Pitch")
        pm.setStyleSheet(MENU_CSS)
        pitches = [{"id": p, "label": lbl} for p, lbl in
                   [(-12.0, "−12 (female voice → male model)"), (-6.0, "−6"), (-3.0, "−3"), (0.0, "0 (same gender)"),
                    (3.0, "+3"), (6.0, "+6"), (12.0, "+12 (male voice → female model)")]]
        self._radio(pm, pitches, float(cur.get("rvc_pitch", 0)), "rvc_pitch")
        rv.addAction("Open voices folder", lambda: self.link.send("open_voices_folder"))
        rv.addAction("Rescan", lambda: self.link.send("get_settings"))
        rv.addAction("Models: voices\\<name>\\ with .pth (+ .index), or a loose .pth").setEnabled(False)

        m.addSeparator()
        self.build_settings(m.addMenu("⚙  Settings"), cur, opt)

        pm = m.addMenu("🎭  Personality")
        pm.setStyleSheet(MENU_CSS)
        pm.addAction("Edit personality…", lambda: self.link.send("edit_personality"))
        pm.addAction("Reset to default", self.reset_personality)

        mem = m.addMenu("📒  Memory")
        mem.setStyleSheet(MENU_CSS)
        on = QAction("Remember chats between restarts", mem, checkable=True, checked=bool(cur.get("memory", True)))
        on.triggered.connect(lambda c: self._send_set("memory", c))
        mem.addAction(on)
        mem.addAction(f"{cur.get('remembered', 0)} messages · {cur.get('notes', 0)} saved notes").setEnabled(False)
        mem.addSeparator()
        mem.addAction("Edit memory notes…", lambda: self.link.send("edit_notes"))
        mem.addAction("Open memory folder", lambda: self.link.send("open_memory_folder"))
        mem.addSeparator()
        mem.addAction("Forget this conversation", lambda: self.link.send("clear_memory"))
        mem.addAction("Clear all memory…", self.clear_all_memory)
        m.addSeparator()
        m.addAction("Teleport now", self.teleport)
        m.addAction("Reset position", self.reset_position)
        m.addAction("Quit Pitagoras", self.quit_all)
        return m

    def _sub(self, parent: QMenu, title: str) -> QMenu:
        sm = parent.addMenu(title)
        sm.setStyleSheet(MENU_CSS)
        return sm

    def build_settings(self, s: QMenu, cur: dict, opt: dict):
        s.setStyleSheet(MENU_CSS)
        lang = self._sub(s, "🌍  Language")
        lang.addAction("Replies are in this language; the voice switches to match").setEnabled(False)
        lang.addSeparator()
        self._radio(lang, opt.get("language", []), cur.get("language", "auto"), "language")

        ears = self._sub(s, "👂  I speak (ears)")
        ears.addAction("Which language the ears listen for").setEnabled(False)
        ears.addSeparator()
        self._radio(ears, opt.get("ears_language", []), cur.get("ears_language", "auto"), "ears_language")

        tc = QAction("💬  Comment after doing tasks", s, checkable=True, checked=bool(cur.get("task_comments")))
        tc.triggered.connect(lambda c: self._send_set("task_comments", c))
        s.addAction(tc)
        s.addAction("      (off = just “Robi się!” when opening apps, music…)").setEnabled(False)
        fc = QAction("🌐  Check answers online", s, checkable=True, checked=bool(cur.get("fact_check", True)))
        fc.triggered.connect(lambda c: self._send_set("fact_check", c))
        s.addAction(fc)
        s.addAction("      (questions are looked up first; the internet wins if it disagrees)").setEnabled(False)

        ln = self._sub(s, "📏  Reply length")
        self._radio(ln, opt.get("length", []), cur.get("length"), "length")

        ch = self._sub(s, "💭  Chatter")
        ch.addAction("How often it comments while watching along").setEnabled(False)
        ch.addSeparator()
        self._radio(ch, opt.get("chatter", []), cur.get("chatter"), "chatter")

        col = self._sub(s, "🎨  Colors")
        for key, label in (("body", "Body color…"), ("mouth", "Mouth color…"), ("outline", "Outline color…"),
                           ("listening_ring", "Listening ring color…")):
            a = col.addAction(label, lambda k=key, t=label: self.pick_color(k, t.rstrip("…")))
            a.setIcon(self._swatch(self.colors[key]))
        col.addSeparator()
        col.addAction("Edit appearance.json…", self.open_appearance)
        col.addAction("Reset to yellow", self.reset_colors)

        s.addSeparator()
        pref = self._sub(s, "🙋  Preferences")
        phrase = cur.get("wake_phrase") or "hej pitagoras"
        pref.addAction(f"Wake phrase: “{phrase.title()}”…", self.ask_wake_phrase)
        uname = cur.get("user_name") or ""
        pref.addAction(f"Your name: {uname or '(not set)'}…", self.ask_user_name)
        s.addSeparator()
        ww = QAction(f"Only listen after “{phrase.title()}”", s, checkable=True, checked=bool(cur.get("wake_word")))
        ww.triggered.connect(lambda c: self._send_set("wake_word", c))
        s.addAction(ww)
        fu = QAction("   …and allow follow-ups without it (up to 3)", s, checkable=True,
                     checked=bool(cur.get("follow_ups")))
        fu.setEnabled(bool(cur.get("wake_word")))
        fu.triggered.connect(lambda c: self._send_set("follow_ups", c))
        s.addAction(fu)
        pc = QAction("🖱  Let Pitagoras use mouse & keyboard", s, checkable=True, checked=bool(cur.get("pc_control", True)))
        pc.triggered.connect(lambda c: self._send_set("pc_control", c))
        s.addAction(pc)
        s.addAction("      (move the mouse yourself to take over)").setEnabled(False)
        wa = QAction("🎲  Teleport around when idle", s, checkable=True, checked=self.wander)
        wa.triggered.connect(self.set_wander)
        s.addAction(wa)

    @staticmethod
    def _swatch(c: QColor):
        from PySide6.QtGui import QIcon, QPixmap

        pm = QPixmap(14, 14)
        pm.fill(c)
        return QIcon(pm)

    def custom_server(self):
        url, ok = QInputDialog.getText(self, "Custom brain server", "OpenAI-compatible URL (ending in /v1):",
                                       text="http://localhost:5001/v1")
        if not ok or not url.strip():
            return
        model, ok = QInputDialog.getText(self, "Custom brain server", "Model name (empty = first one):")
        if not ok:
            return
        self.link.send("set", key="brain", value="custom", data={"base_url": url.strip(), "model": model.strip()})

    # ── colors ──────────────────────────────────────────────
    @staticmethod
    def _appearance_mtime() -> float:
        try:
            return appearance_path().stat().st_mtime
        except OSError:
            return 0.0

    def check_appearance(self):
        mt = self._appearance_mtime()
        if mt != self.colors_mtime:
            self.colors_mtime = mt
            self.colors = load_colors()
            self.update()

    def pick_color(self, key: str, title: str):
        dlg = QColorDialog(self.colors[key], self)
        dlg.setWindowTitle(title)
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
        original = dict(self.colors)
        dlg.currentColorChanged.connect(lambda c: self._preview_color(key, c))  # live preview
        if dlg.exec() == QColorDialog.Accepted:
            c = dlg.selectedColor()
            data = read_appearance()
            data[key] = [c.red(), c.green(), c.blue()]
            write_appearance(data)
            self.check_appearance()
        else:
            self.colors = original
            self.update()

    def _preview_color(self, key: str, c: QColor):
        if c.isValid():
            data = {k: [v.red(), v.green(), v.blue()] for k, v in self.colors.items() if k in DEFAULT_COLORS}
            data[key] = [c.red(), c.green(), c.blue()]
            body = parse_color(data["body"])
            self.colors.update({k: parse_color(v) for k, v in data.items()})
            self.colors["body_light"] = QColor(*(int(x + (255 - x) * 0.6) for x in (body.red(), body.green(), body.blue())))
            self.colors["body_dark"] = body.darker(118)
            self.update()

    def open_appearance(self):
        import os

        read_appearance()  # creates it if missing
        try:
            os.startfile(str(appearance_path()), "edit")
        except OSError:
            os.startfile(str(appearance_path()))

    def reset_colors(self):
        write_appearance(DEFAULT_COLORS)
        self.check_appearance()

    def ask_wake_phrase(self):
        cur = (self.settings.get("current") or {}).get("wake_phrase") or "hej pitagoras"
        text, ok = QInputDialog.getText(self, "Wake phrase", "What should I listen for? (e.g. “hej madzia”)",
                                        text=cur)
        if ok and text.strip():
            self._send_set("wake_phrase", text.strip())

    def ask_user_name(self):
        cur = (self.settings.get("current") or {}).get("user_name") or ""
        text, ok = QInputDialog.getText(self, "Your name", "How should I call you? (empty = no name)", text=cur)
        if ok:
            self._send_set("user_name", text.strip())

    def fish_setup(self):
        key, ok = QInputDialog.getText(self, "Fish Audio", "API key (fish.audio > API keys):", QLineEdit.Password)
        if not ok or not key.strip():
            return
        voice, ok = QInputDialog.getText(self, "Fish Audio",
                                         "Voice model ID (from the voice's page URL; empty = default voice):")
        if not ok:
            return
        self.link.send("set", key="fish", value=True, data={"api_key": key.strip(), "voice_id": voice.strip()})

    def set_wander(self, on: bool):
        self.wander = bool(on)
        self.store.setValue("wander", self.wander)
        self.touch()

    def _confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Yes | QMessageBox.Cancel)
        box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
        return box.exec() == QMessageBox.Yes

    def clear_all_memory(self):
        if self._confirm("Clear all memory", "Delete the saved conversation AND all saved notes?\nThis can't be undone."):
            self.link.send("clear_memory", all=True)

    def reset_personality(self):
        if self._confirm("Reset personality", "Replace personality.txt with the default personality?"):
            self.link.send("reset_personality")

    def quit_all(self):
        self.link.send("quit")
        QTimer.singleShot(300, QApplication.quit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--size", type=int, default=0)
    args = ap.parse_args()
    size = args.size
    if not size:
        try:
            from .config import load_config

            size = int((load_config().get("overlay") or {}).get("size", 150))
        except Exception:  # noqa: BLE001
            size = 150
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = Character(args.port, size)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
