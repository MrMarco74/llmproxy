#!/usr/bin/env python3
"""
LLM Monitor — Desktop-App für llmproxy Performance Monitoring.
Verbindet sich mit llmproxy /status/stream (SSE) statt SSH.
Läuft auf beliebigen Linux-Maschinen im Heimnetz.
"""

import sys
import json
import time
import datetime
import configparser
from pathlib import Path

import httpx
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QDialog, QDialogButtonBox,
    QFormLayout, QMessageBox, QSystemTrayIcon, QMenu, QAction,
)
from PyQt5.QtGui import QIcon, QFont, QPixmap
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer

CONFIG_FILE = Path.home() / ".config" / "llmproxy" / "monitor.conf"
DEFAULT_URL = "http://proxy-host:11435"

# Icon: installed by install_monitor.sh into hicolor theme, fallback to bundled copy
_ICON_PATHS = [
    Path.home() / ".local/share/icons/hicolor/256x256/apps/llm-monitor.png",
    Path.home() / ".local/share/icons/hicolor/128x128/apps/llm-monitor.png",
    Path(__file__).parent / "assets" / "llm-monitor.png",
]

def _app_icon():
    from PyQt5.QtGui import QIcon, QPixmap
    # Try hicolor theme first (works for taskbar + startmenu automatically)
    icon = QIcon.fromTheme("llm-monitor")
    if not icon.isNull():
        return icon
    # Fallback: load from file
    for p in _ICON_PATHS:
        if p.exists():
            return QIcon(str(p))
    return QIcon.fromTheme("utilities-system-monitor")


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config() -> str:
    if CONFIG_FILE.exists():
        cfg = configparser.ConfigParser()
        cfg.read(str(CONFIG_FILE))
        return cfg.get("monitor", "proxy_url", fallback=DEFAULT_URL)
    return DEFAULT_URL


def _save_config(url: str):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = configparser.ConfigParser()
    cfg["monitor"] = {"proxy_url": url}
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)


# ── Config dialog (first-run) ─────────────────────────────────────────────────

class ConfigDialog(QDialog):
    def __init__(self, current_url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LLM Monitor — Konfiguration")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form   = QFormLayout()

        self.url_edit = QLineEdit(current_url)
        form.addRow("llmproxy URL:", self.url_edit)
        layout.addLayout(form)

        info = QLabel("Standard: http://proxy-host:11435\nDie URL des llmproxy-Servers im Heimnetz.")
        info.setStyleSheet("color: #9399b2; font-size: 12px;")
        layout.addWidget(info)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self.setStyleSheet("""
            QDialog { background-color: #1e1e2e; color: #cdd6f4; }
            QLabel  { color: #cdd6f4; }
            QLineEdit { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                        border-radius: 4px; padding: 4px; }
            QPushButton { background: #585b70; color: #cdd6f4; border-radius: 4px; padding: 6px 12px; }
            QPushButton:hover { background: #6c7086; }
        """)

    def get_url(self) -> str:
        return self.url_edit.text().strip().rstrip("/")


# ── SSE Monitor Thread ─────────────────────────────────────────────────────────

class SSEMonitorThread(QThread):
    stats_updated = pyqtSignal(dict)
    connection_changed = pyqtSignal(bool, str)   # connected, status_text

    def __init__(self, proxy_url: str):
        super().__init__()
        self.proxy_url = proxy_url
        self._running  = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                self.connection_changed.emit(False, f"Verbinde mit {self.proxy_url} …")
                with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)) as client:
                    with client.stream("GET", f"{self.proxy_url}/status/stream") as resp:
                        self.connection_changed.emit(True, "Verbunden")
                        for line in resp.iter_lines():
                            if not self._running:
                                return
                            if line.startswith("data: "):
                                try:
                                    data = json.loads(line[6:])
                                    self.stats_updated.emit(data)
                                except (json.JSONDecodeError, ValueError):
                                    pass
            except Exception as e:
                if self._running:
                    self.connection_changed.emit(False, f"Verbindung verloren — reconnect in 3s")
                    time.sleep(3)


# ── Token accumulator (since app start) ───────────────────────────────────────

class TokenAccumulator:
    def __init__(self):
        self.prompt_total      = 0
        self.completion_total  = 0
        self.last_tps_in       = 0
        self.last_tps_out      = 0
        self.max_tps_in        = 0
        self.max_tps_out       = 0
        self._last_update      = time.monotonic()
        self._last_pt          = 0
        self._last_ct          = 0

    def update(self, snapshot: dict):
        recent = snapshot.get("recent", [])
        if not recent:
            return
        latest = recent[0]
        pt = latest.get("prompt_tokens", 0) or 0
        ct = latest.get("completion_tokens", 0) or 0
        dur = latest.get("duration_s", 1) or 1

        if pt != self._last_pt or ct != self._last_ct:
            self.prompt_total     += pt
            self.completion_total += ct
            self.last_tps_in  = int(pt / dur) if dur > 0 else 0
            self.last_tps_out = int(ct / dur) if dur > 0 else 0
            self.max_tps_in   = max(self.max_tps_in,  self.last_tps_in)
            self.max_tps_out  = max(self.max_tps_out, self.last_tps_out)
            self._last_pt = pt
            self._last_ct = ct


# ── Main Window ────────────────────────────────────────────────────────────────

class LLMMonitorGUI(QMainWindow):
    def __init__(self, proxy_url: str):
        super().__init__()
        self.proxy_url      = proxy_url
        self.accumulator    = TokenAccumulator()
        self._last_snapshot: dict = {}
        self._loaded_model_names: list = []
        self._last_unread: int = 0
        self._force_quit: bool = False

        self.setWindowTitle("LLM Monitor")
        self.setMinimumWidth(560)
        self.setWindowIcon(_app_icon())

        # System tray
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_app_icon())
        tray_menu = QMenu()
        act_show = QAction("Anzeigen", self)
        act_show.triggered.connect(self._show_window)
        act_quit = QAction("Beenden", self)
        act_quit.triggered.connect(self._quit_app)
        tray_menu.addAction(act_show)
        tray_menu.addSeparator()
        tray_menu.addAction(act_quit)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        # Connection status bar
        status_row = QHBoxLayout()
        self.lbl_conn   = QLabel("● Verbinde …")
        self.lbl_conn.setStyleSheet("color: #f38ba8; font-weight: bold;")
        self.btn_config = QPushButton("⚙")
        self.btn_config.setFixedSize(28, 28)
        self.btn_config.setToolTip("Verbindungs-URL ändern")
        self.btn_config.clicked.connect(self._open_config)
        status_row.addWidget(self.lbl_conn)
        status_row.addStretch()
        status_row.addWidget(self.btn_config)
        root.addLayout(status_row)

        # Gaming mode banner (hidden by default)
        self.gaming_banner = QLabel("🎮  GAMING MODE — LLM-Anfragen werden geblockt")
        self.gaming_banner.setAlignment(Qt.AlignCenter)
        self.gaming_banner.setStyleSheet(
            "background: #f38ba8; color: #1e1e2e; font-weight: bold; "
            "font-size: 13px; padding: 6px; border-radius: 4px;"
        )
        self.gaming_banner.setVisible(False)
        root.addWidget(self.gaming_banner)

        # Tokens
        tg = QGroupBox("LLM Tokens")
        tl = QVBoxLayout(tg)
        self.lbl_active_model  = QLabel("Aktives Modell: —")
        self.lbl_tps_in        = QLabel("Tokens In/s:  0")
        self.lbl_tps_out       = QLabel("Tokens Out/s: 0")
        self.lbl_max_in        = QLabel("Max In/s:  0")
        self.lbl_max_out       = QLabel("Max Out/s: 0")
        self.lbl_ctx           = QLabel("Letzter Kontext: —")
        self.lbl_total_in      = QLabel("Tokens In (Session): 0")
        self.lbl_total_out     = QLabel("Tokens Out (Session): 0")
        for w in [self.lbl_active_model, self.lbl_tps_in, self.lbl_tps_out,
                  self.lbl_max_in, self.lbl_max_out, self.lbl_ctx,
                  self.lbl_total_in, self.lbl_total_out]:
            tl.addWidget(w)
        root.addWidget(tg)

        # Loaded models
        mg = QGroupBox("Geladene Modelle (VRAM)")
        self.models_layout = QVBoxLayout(mg)
        self.lbl_no_models = QLabel("Keine Modelle geladen.")
        self.models_layout.addWidget(self.lbl_no_models)
        root.addWidget(mg)

        # Hardware
        hg = QGroupBox("Hardware")
        hl = QVBoxLayout(hg)
        self.lbl_cpu  = QLabel("CPU:   — | RAM:  —")
        self.lbl_gpu0 = QLabel("GPU #0: —")
        self.lbl_gpu1 = QLabel("GPU #1: —")
        for w in [self.lbl_cpu, self.lbl_gpu0, self.lbl_gpu1]:
            hl.addWidget(w)
        root.addWidget(hg)

        self._apply_style()
        self._start_thread()

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1e1e2e; color: #cdd6f4; }
            QGroupBox {
                font-weight: bold; font-size: 13px; color: #b4befe;
                border: 1px solid #45475a; border-radius: 6px;
                margin-top: 16px; padding-top: 4px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLabel   { font-size: 13px; padding: 3px; color: #cdd6f4; }
            QPushButton {
                background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 4px; font-size: 14px;
            }
            QPushButton:hover { background: #45475a; }
        """)

    def _start_thread(self):
        self._thread = SSEMonitorThread(self.proxy_url)
        self._thread.stats_updated.connect(self._on_stats)
        self._thread.connection_changed.connect(self._on_conn_change)
        self._thread.start()

    def _open_config(self):
        dlg = ConfigDialog(self.proxy_url, self)
        if dlg.exec_() == QDialog.Accepted:
            new_url = dlg.get_url()
            if new_url != self.proxy_url:
                self.proxy_url = new_url
                _save_config(new_url)
                self._thread.stop()
                self._thread.quit()
                self._start_thread()

    def _on_conn_change(self, connected: bool, text: str):
        if connected:
            self.lbl_conn.setText(f"● {text}")
            self.lbl_conn.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        else:
            self.lbl_conn.setText(f"● {text}")
            self.lbl_conn.setStyleSheet("color: #f38ba8; font-weight: bold;")

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self._show_window()

    def _show_window(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        self._force_quit = True
        self._tray.hide()
        self._thread.stop()
        self._thread.quit()
        self._thread.wait(2000)
        QApplication.quit()

    def _on_stats(self, snapshot: dict):
        self._last_snapshot = snapshot
        self.accumulator.update(snapshot)
        self._update_gaming(snapshot.get("gaming_mode", False))
        self._update_hw(snapshot.get("hw", {}))
        self._update_models(snapshot.get("models", []))
        self._update_tokens(snapshot)
        self._check_notifications(snapshot)

    def _check_notifications(self, snapshot: dict):
        unread = snapshot.get("unread_count", 0)
        if unread > self._last_unread and QSystemTrayIcon.isSystemTrayAvailable():
            new_count = unread - self._last_unread
            # Fetch the latest notification for the toast message
            try:
                resp = httpx.get(f"{self.proxy_url}/notifications?limit=1&unread_only=true",
                                 timeout=2.0)
                items = resp.json().get("items", [])
                if items:
                    n = items[0]
                    self._tray.showMessage(
                        n.get("title", "llmproxy Notification"),
                        n.get("message", ""),
                        QSystemTrayIcon.Information if n.get("priority") == "default" else QSystemTrayIcon.Warning,
                        5000,
                    )
                elif new_count > 0:
                    self._tray.showMessage(
                        "llmproxy", f"{new_count} neue Notification(s)",
                        QSystemTrayIcon.Information, 4000,
                    )
            except Exception:
                pass
        self._last_unread = unread

    def _update_gaming(self, gaming: bool):
        self.gaming_banner.setVisible(gaming)

    def _update_hw(self, hw: dict):
        if not hw:
            return
        cpu   = hw.get("cpu_pct", 0)
        ru    = hw.get("ram_used", 0)
        rt    = hw.get("ram_total", 0)
        rpct  = hw.get("ram_pct", 0)
        self.lbl_cpu.setText(
            f"CPU: {cpu:.1f}% | RAM: {ru:,} / {rt:,} MB ({rpct:.0f}%)".replace(",",".")
        )
        gpus = hw.get("gpus", [])
        gpu_labels = [self.lbl_gpu0, self.lbl_gpu1]
        for i, lbl in enumerate(gpu_labels):
            if i < len(gpus):
                g = gpus[i]
                ru_g  = g.get("ram_used", 0)
                rt_g  = g.get("ram_total", 0)
                pct_g = round(ru_g / rt_g * 100) if rt_g > 0 else 0
                pw    = f" | {g['power_w']:.0f}W" if g.get("power_w") else ""
                lbl.setText(
                    f"GPU #{g['index']}: {g['load_pct']:.0f}% | "
                    f"VRAM: {ru_g:,}/{rt_g:,} MB ({pct_g}%) | "
                    f"{g['temp_c']:.0f}°C{pw}".replace(",",".")
                )
                # Color by temp
                if g["temp_c"] >= 80:
                    lbl.setStyleSheet("color: #f38ba8;")
                elif g["temp_c"] >= 70:
                    lbl.setStyleSheet("color: #fab387;")
                else:
                    lbl.setStyleSheet("color: #a6e3a1;")
            else:
                lbl.setText(f"GPU #{i}: nicht verfügbar")
                lbl.setStyleSheet("color: #585b70;")

    def _update_models(self, models: list):
        for i in reversed(range(self.models_layout.count())):
            w = self.models_layout.itemAt(i).widget()
            if w:
                w.setParent(None)

        self._loaded_model_names = [m.get("name", "") for m in models]

        if not models:
            self.models_layout.addWidget(QLabel("Keine Modelle geladen."))
            return

        for m in models:
            name  = m.get("name", "—")
            vram  = m.get("size_vram", 0) / (1024**3)
            ctx   = m.get("context_length", 0)
            lbl   = QLabel(f"• {name} — {vram:.1f} GB VRAM  ctx: {ctx//1024}k")
            if ctx >= 32768:
                lbl.setStyleSheet("color: #f38ba8;")
            elif ctx > 8192:
                lbl.setStyleSheet("color: #fab387;")
            else:
                lbl.setStyleSheet("color: #a6e3a1;")
            self.models_layout.addWidget(lbl)

    def _update_tokens(self, snapshot: dict):
        recent  = snapshot.get("recent", [])
        active_reqs = snapshot.get("active", [])
        acc     = self.accumulator
        tps_out = recent[0].get("tokens_per_second") if recent else None

        # Active model: prefer in-flight request, fall back to last completed
        if active_reqs:
            models = list(dict.fromkeys(r["model"] for r in active_reqs))  # deduplicated, ordered
            label  = " + ".join(models)
            elapsed = active_reqs[0].get("elapsed_s", 0)
            self.lbl_active_model.setText(f"▶ {label}  ({elapsed:.0f}s)")
            self.lbl_active_model.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        elif recent:
            active = recent[0].get("model", "—")
            if active in self._loaded_model_names:
                self.lbl_active_model.setText(f"Letzter: {active}")
                self.lbl_active_model.setStyleSheet("color: #a6e3a1;")
            else:
                self.lbl_active_model.setText(f"Letzter: {active} (entladen)")
                self.lbl_active_model.setStyleSheet("color: #9399b2;")
        else:
            self.lbl_active_model.setText("Aktives Modell: —")
            self.lbl_active_model.setStyleSheet("color: #9399b2;")

        def _fmt(n): return f"{n:,}".replace(",",".")

        self.lbl_tps_in.setText(  f"Tokens In/s:  {_fmt(acc.last_tps_in)}")
        self.lbl_tps_out.setText( f"Tokens Out/s: {_fmt(acc.last_tps_out)}")
        self.lbl_max_in.setText(  f"Max In/s:  {_fmt(acc.max_tps_in)}")
        self.lbl_max_out.setText( f"Max Out/s: {_fmt(acc.max_tps_out)}")

        if tps_out:
            self.lbl_ctx.setText(f"Letzte tps: {tps_out:.1f}")
        else:
            self.lbl_ctx.setText("Letzte tps: —")

        self.lbl_total_in.setText( f"Tokens In  (Session): {_fmt(acc.prompt_total)}")
        self.lbl_total_out.setText(f"Tokens Out (Session): {_fmt(acc.completion_total)}")

    def closeEvent(self, event):
        if getattr(self, "_force_quit", False):
            event.accept()
            return
        # Minimize to tray instead of closing
        event.ignore()
        self.hide()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.showMessage(
                "LLM Monitor",
                "Läuft weiter im Hintergrund. Über das Tray-Icon wieder öffnen.",
                QSystemTrayIcon.Information, 3000,
            )


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("LLM Monitor")
    app.setWindowIcon(_app_icon())

    proxy_url    = _load_config()
    first_run    = not CONFIG_FILE.exists()

    if first_run:
        dlg = ConfigDialog(proxy_url)
        if dlg.exec_() == QDialog.Accepted:
            proxy_url = dlg.get_url()
            _save_config(proxy_url)
        else:
            sys.exit(0)

    window = LLMMonitorGUI(proxy_url)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
