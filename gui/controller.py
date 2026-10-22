"""Desktop controller for launching and monitoring the connector demo."""

import codecs
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from dotenv import dotenv_values
from PySide6.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QDesktopServices, QFontDatabase
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from theme import TONES, Card, Pill, apply_theme, divider

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
WEB_BUILD = ROOT / "mcp" / "web" / "index.html"
LOCAL_URL = "http://127.0.0.1:8000"
PYTHON = sys.executable
Command = Sequence[str]
HISTORY_TONES = {"Running": "info", "Succeeded": "success", "Failed": "danger"}


class Runner(QObject):
    """Runs commands sequentially in one QProcess and can stop the whole process tree."""

    output = Signal(str)
    state_changed = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.state = "Idle"
        self.exit_code: int | None = None
        self.started_at = 0.0
        self.ended_at = 0.0
        self._steps: list[Command] = []
        self._stopping = False
        self._pending = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)

    @property
    def active(self) -> bool:
        return self.state in {"Running", "Stopping"}

    @property
    def elapsed(self) -> float:
        return (time.monotonic() if self.active else self.ended_at) - self.started_at

    def start(self, steps: Sequence[Command]) -> None:
        if self.active:
            raise RuntimeError(f"{self.name} is already running")
        self._steps, self._stopping, self.exit_code = list(steps), False, None
        self.started_at = time.monotonic()
        self._set("Running")
        self._next()

    def stop(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._stopping = True
        self._steps.clear()
        self._set("Stopping")
        if sys.platform == "win32":
            # uv, npm, and pwsh may start child processes; stop the complete tree by PID.
            subprocess.run(
                ["taskkill", "/PID", str(self.process.processId()), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        else:
            self.process.kill()

    def _next(self) -> None:
        program, *args = self._steps.pop(0)
        resolved = shutil.which(program)
        if resolved is None:
            self.output.emit(f"[{self.name}] '{program}' was not found on PATH.")
            self._done("Failed", None)
            return
        self._decoder.reset()
        self.output.emit(f"[{self.name}] $ {subprocess.list2cmdline([program, *args])}")
        self.process.start(resolved, args)

    def _read(self) -> None:
        chunk = bytes(self.process.readAllStandardOutput().data())
        text = self._pending + self._decoder.decode(chunk)
        lines = text.replace("\r\n", "\n").split("\n")
        self._pending = lines.pop()
        for line in lines:
            if line.strip():
                self.output.emit(f"[{self.name}] {line}")

    def _finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._read()
        if self._pending.strip():
            self.output.emit(f"[{self.name}] {self._pending}")
        self._pending = ""
        if self._stopping:
            self._done("Stopped", None)
        elif status == QProcess.ExitStatus.NormalExit and code == 0:
            if self._steps:
                self._next()
            else:
                self._done("Succeeded", 0)
        else:
            self._done("Failed", code)

    def _error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.output.emit(f"[{self.name}] Failed to start: {self.process.errorString()}")
            self._done("Failed", None)

    def _done(self, state: str, code: int | None) -> None:
        self.exit_code, self.ended_at = code, time.monotonic()
        self._steps.clear()
        suffix = "" if code is None else f" (exit code {code})"
        self.output.emit(f"[{self.name}] {state}{suffix} after {self.elapsed:.1f}s")
        self._set(state)

    def _set(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)


class SetupDialog(QDialog):
    FIELDS = [
        ("TenantId", "Tenant ID"),
        ("PublicUrl", "Public HTTPS URL"),
        ("AliceUpn", "Editor UPN — copy the Email value"),
        ("BobUpn", "Reader UPN — copy the Email value"),
        ("EveUpn", "Finance UPN — copy the Email value"),
        ("Prefix", "App name prefix"),
    ]

    def __init__(self, settings: QSettings, public_url: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Tenant setup")
        layout = QVBoxLayout(self)
        note = QLabel(
            "Creates three app registrations, grants admin consent, and writes a private "
            ".env file. Use the exact Email (UPN) values from this tenant's Microsoft 365 "
            "test-user list; do not guess or replace the tenant domain."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.inputs: dict[str, QLineEdit] = {}
        defaults = {
            "PublicUrl": public_url,
            "Prefix": "helpdesk-demo",
        }
        for key, label in self.FIELDS:
            saved = str(settings.value(f"setup/{key}", "") or "")
            self.inputs[key] = QLineEdit(saved or defaults.get(key, ""))
            form.addRow(label, self.inputs[key])
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Run setup")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, str]:
        return {key: field.text().strip() for key, field in self.inputs.items()}

    def validation_error(self) -> str:
        v = self.values()
        try:
            UUID(v["TenantId"])
        except ValueError:
            return "Tenant ID must be a GUID."
        url = urlparse(v["PublicUrl"])
        if url.scheme != "https" or not url.hostname or url.path not in ("", "/"):
            return "Public URL must be an HTTPS origin, e.g. https://name-8000.devtunnels.ms"
        users = [v["AliceUpn"].casefold(), v["BobUpn"].casefold(), v["EveUpn"].casefold()]
        if any("@" not in user for user in users) or len(set(users)) != 3:
            return "Enter three different user principal names."
        if not re.fullmatch(r"[a-zA-Z0-9-]{3,40}", v["Prefix"]):
            return "Prefix must be 3-40 letters, digits, or hyphens."
        return ""

    def accept(self) -> None:
        if error := self.validation_error():
            QMessageBox.warning(self, "Tenant setup", error)
            return
        for key, value in self.values().items():
            self.settings.setValue(f"setup/{key}", value)
        super().accept()

    def arguments(self) -> list[str]:
        return [item for key, value in self.values().items() for item in (f"-{key}", value)]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Connector demo controller")
        self.resize(1050, 780)
        self.settings = QSettings("workiq-connector", "demo-controller")
        self.env: dict[str, str | None] = {}
        self.health = {"local": "Not checked", "public": "Not checked"}
        self.checking: set[str] = set()
        self.tunnel_urls: list[str] = []
        self.server_env_mtime = 0.0
        self.network = QNetworkAccessManager(self)
        self.server = Runner("server", self)
        self.tunnel = Runner("tunnel", self)
        self.script = Runner("script", self)
        for runner in (self.server, self.tunnel, self.script):
            runner.output.connect(self.log)
            runner.state_changed.connect(self.refresh)
        self.tunnel.output.connect(self._tunnel_output)
        self.script.state_changed.connect(self._script_state)
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll()

    def _button(self, text: str, action, primary: bool = False, tip: str = "") -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(action)
        if primary:
            button.setProperty("variant", "primary")
        if tip:
            button.setToolTip(tip)
        return button

    def _build_ui(self) -> None:
        top = QWidget()
        layout = QVBoxLayout(top)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        env_card = Card("Environment", "Steps 1-4")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self.env_label, self.web_label = Pill(), Pill()
        self.url_label = QLabel()
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tool_pills = {name: Pill() for name in ("npm", "pwsh", "devtunnel")}
        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        for pill in self.tool_pills.values():
            tools_row.addWidget(pill)
        tools_row.addStretch()
        for row, (name, widget) in enumerate(
            [
                (".env file", self.env_label),
                ("Public URL", self.url_label),
                ("Web page", self.web_label),
                ("Tools", tools_row),
            ]
        ):
            caption = QLabel(name)
            caption.setProperty("muted", True)
            grid.addWidget(caption, row, 0)
            if isinstance(widget, QHBoxLayout):
                grid.addLayout(widget, row, 1)
            else:
                grid.addWidget(widget, row, 1, Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(2, 1)
        env_card.body.addLayout(grid)
        layout.addWidget(env_card)

        services = Card("Services", "Steps 5 and 8")
        self.server_state, self.tunnel_state = Pill(), Pill()
        self.server_detail, self.tunnel_detail = QLabel(), QLabel()
        for detail in (self.server_detail, self.tunnel_detail):
            detail.setProperty("muted", True)
        self.server_start = self._button("Start", self.start_server, True, "Step 8")
        self.server_stop = self._button("Stop", self.server.stop)
        self.server_open = self._button("Open page", self.open_page)
        self.tunnel_id = QLineEdit(str(self.settings.value("tunnel/id", "") or ""))
        self.tunnel_id.setPlaceholderText("Dev tunnel ID")
        self.tunnel_id.setMinimumWidth(180)
        self.tunnel_id.textChanged.connect(self.refresh)
        self.tunnel_start = self._button("Start", self.start_tunnel, True, "Step 5")
        self.tunnel_stop = self._button("Stop", self.tunnel.stop)
        rows = QGridLayout()
        rows.setHorizontalSpacing(12)
        rows.setVerticalSpacing(10)
        name_server, name_tunnel = QLabel("MCP server"), QLabel("Dev tunnel")
        for name in (name_server, name_tunnel):
            name.setObjectName("rowName")
            name.setMinimumWidth(90)
        rows.addWidget(name_server, 0, 0)
        rows.addWidget(self.server_state, 0, 1)
        rows.addWidget(self.server_detail, 0, 2)
        rows.addWidget(self.server_start, 0, 4)
        rows.addWidget(self.server_stop, 0, 5)
        rows.addWidget(self.server_open, 0, 6)
        rows.addWidget(divider(), 1, 0, 1, 7)
        rows.addWidget(name_tunnel, 2, 0)
        rows.addWidget(self.tunnel_state, 2, 1)
        rows.addWidget(self.tunnel_detail, 2, 2)
        rows.addWidget(self.tunnel_id, 2, 3)
        rows.addWidget(self.tunnel_start, 2, 4)
        rows.addWidget(self.tunnel_stop, 2, 5)
        rows.setColumnStretch(2, 1)
        services.body.addLayout(rows)
        layout.addWidget(services)

        scripts = Card("Scripts", "Steps 3, 4, 6 and 7")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.script_buttons = {
            "npm": self._button("Install web packages", self.install_web, tip="Step 3"),
            "build": self._button("Build web page", self.build_web, tip="Step 3"),
            "setup": self._button("Tenant setup...", self.setup_tenant, True, "Step 4"),
            "init": self._button("Create Graph connection", self.init_graph, tip="Step 6"),
            "sync": self._button("Index tickets", self.sync_graph, tip="Step 7"),
            "tests": self._button("Run tests", self.run_tests),
        }
        self.step_tips = {key: button.toolTip() for key, button in self.script_buttons.items()}
        for button in self.script_buttons.values():
            row.addWidget(button)
        row.addStretch()
        self.cancel_button = self._button("Cancel", self.script.stop)
        row.addWidget(self.cancel_button)
        scripts.body.addLayout(row)
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(["Script", "Status", "Started", "Duration", "Exit"])
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.history.setShowGrid(False)
        self.history.verticalHeader().hide()
        self.history.verticalHeader().setDefaultSectionSize(34)
        self.history.setMinimumHeight(120)
        scripts.body.addWidget(self.history)
        layout.addWidget(scripts)
        layout.addStretch()

        log_card = Card("Output")
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(5000)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        log_card.header.addWidget(self._button("Clear", self.output.clear))
        log_card.body.addWidget(self.output)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(top)
        splitter.addWidget(log_card)
        splitter.setSizes([560, 260])

        accent = QFrame()
        accent.setObjectName("accent")
        accent.setFixedSize(4, 30)
        title = QLabel("Connector demo controller")
        title.setObjectName("appTitle")
        subtitle = QLabel("Start services, run setup scripts, and watch their status.")
        subtitle.setProperty("muted", True)
        heading = QVBoxLayout()
        heading.setSpacing(0)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(accent)
        header.addLayout(heading)
        header.addStretch()

        root = QWidget()
        page = QVBoxLayout(root)
        page.setContentsMargins(24, 20, 24, 20)
        page.setSpacing(16)
        page.addLayout(header)
        page.addWidget(splitter, 1)
        self.setCentralWidget(root)

    def log(self, text: str) -> None:
        self.output.appendPlainText(f"{datetime.now():%H:%M:%S} {text}")

    def poll(self) -> None:
        self.env = dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}
        self._check("local", f"{LOCAL_URL}/health")
        public = self.env.get("DEMO_PUBLIC_URL")
        if public and self.tunnel.active:
            self._check("public", f"{public.rstrip('/')}/health")
        else:
            self.health["public"] = "Not checked"
        self.refresh()

    def _check(self, key: str, url: str) -> None:
        if key in self.checking:
            return
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(1500)
        request.setRawHeader(b"X-Tunnel-Skip-AntiPhishing-Page", b"true")
        self.checking.add(key)
        reply = self.network.get(request)
        reply.finished.connect(lambda: self._checked(key, reply))

    def _checked(self, key: str, reply: QNetworkReply) -> None:
        self.checking.discard(key)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        try:
            healthy = json.loads(bytes(reply.readAll().data())).get("status") == "ok"
        except (ValueError, AttributeError):
            healthy = False
        if reply.error() == QNetworkReply.NetworkError.NoError and healthy:
            self.health[key] = "Healthy"
        else:
            self.health[key] = f"HTTP {status}" if status else "Unreachable"
        reply.deleteLater()
        self.refresh()

    def refresh(self) -> None:
        has_env, has_web = ENV_FILE.is_file(), WEB_BUILD.is_file()
        stale = self.server.active and has_env and ENV_FILE.stat().st_mtime > self.server_env_mtime
        if not has_env:
            self.env_label.set_state("Missing - run Tenant setup", "warning")
        elif stale:
            self.env_label.set_state("Changed - restart the server", "warning")
        else:
            self.env_label.set_state("Found", "success")
        public = self.env.get("DEMO_PUBLIC_URL") or ""
        self.url_label.setText(public or "Not configured")
        if has_web:
            self.web_label.set_state("Built", "success")
        else:
            self.web_label.set_state("Missing - install packages and build", "warning")
        tools = {name: shutil.which(name) for name in ("npm", "pwsh", "devtunnel")}
        for name, pill in self.tool_pills.items():
            pill.set_state(
                f"{name} {'found' if tools[name] else 'missing'}",
                "success" if tools[name] else "danger",
            )

        local = self.health["local"]
        if self.server.state == "Running":
            if local == "Healthy":
                state, tone = "Running", "success"
            else:
                state, tone = "Starting / not responding", "warning"
        elif self.server.state == "Stopping":
            state, tone = "Stopping", "warning"
        elif local == "Healthy":
            state, tone = "Port 8000 is used by another process", "danger"
        elif self.server.state in {"Failed", "Succeeded"}:
            state = f"Exited (code {self.server.exit_code})"
            tone = "danger" if self.server.exit_code else "neutral"
        else:
            state, tone = "Stopped", "neutral"
        self.server_state.set_state(state, tone)
        self.server_detail.setText(
            f"PID {self.server.process.processId()}, up {self.server.elapsed:.0f}s, local {local}"
            if self.server.active
            else ""
        )
        self.server_start.setEnabled(not self.server.active and local != "Healthy")
        self.server_stop.setEnabled(self.server.state == "Running")
        self.server_open.setEnabled(local == "Healthy")

        tunnel_state = "Stopped" if self.tunnel.state == "Idle" else self.tunnel.state
        tunnel_tone = {"Running": "info", "Failed": "danger", "Stopping": "warning"}.get(
            tunnel_state, "neutral"
        )
        if tunnel_state == "Running" and self.health["public"] == "Healthy":
            tunnel_tone = "success"
        self.tunnel_state.set_state(tunnel_state, tunnel_tone)
        if self.tunnel.active:
            matches = public.rstrip("/") in {url.rstrip("/") for url in self.tunnel_urls}
            url = self.tunnel_urls[0] if self.tunnel_urls else "waiting for URL"
            note = "" if not self.tunnel_urls or matches else " (differs from .env)"
            self.tunnel_detail.setText(f"{url}{note}, public {self.health['public']}")
        else:
            self.tunnel_detail.setText("")
        self.tunnel_start.setEnabled(
            not self.tunnel.active and bool(tools["devtunnel"]) and bool(self.tunnel_id.text())
        )
        self.tunnel_stop.setEnabled(self.tunnel.state == "Running")

        ready = {
            "npm": bool(tools["npm"]),
            "build": bool(tools["npm"]),
            "setup": bool(tools["pwsh"]) and not has_env,
            "init": has_env,
            "sync": has_env,
            "tests": True,
        }
        for key, button in self.script_buttons.items():
            button.setEnabled(not self.script.active and ready[key])
        self.script_buttons["setup"].setToolTip(
            ".env already exists; setup will not overwrite it."
            if has_env
            else self.step_tips["setup"]
        )
        self.cancel_button.setEnabled(self.script.state == "Running")

    def start_server(self) -> None:
        if not ENV_FILE.is_file() or not WEB_BUILD.is_file():
            QMessageBox.warning(
                self, "MCP server", "Run Tenant setup and build the web page first."
            )
            return
        self.server_env_mtime = ENV_FILE.stat().st_mtime
        self.server.start([(PYTHON, "mcp/server.py", "serve")])

    def open_page(self) -> None:
        public = self.env.get("DEMO_PUBLIC_URL")
        url = public if public and self.health["public"] == "Healthy" else "http://localhost:8000"
        QDesktopServices.openUrl(QUrl(url))

    def start_tunnel(self) -> None:
        tunnel_id = self.tunnel_id.text().strip()
        if not re.fullmatch(r"[\w.-]+", tunnel_id):
            QMessageBox.warning(self, "Dev tunnel", "Enter a valid dev tunnel ID.")
            return
        self.settings.setValue("tunnel/id", tunnel_id)
        self.tunnel_urls.clear()
        self.tunnel.start([("devtunnel", "host", tunnel_id)])

    def _tunnel_output(self, line: str) -> None:
        for url in re.findall(r"https://[^\s,]+", line):
            if "-inspect." not in url and url not in self.tunnel_urls:
                self.tunnel_urls.append(url)

    def run_script(self, name: str, steps: Sequence[Command]) -> None:
        row = self.history.rowCount()
        self.history.insertRow(row)
        for column, value in enumerate([name, "Running", f"{datetime.now():%H:%M:%S}", "", ""]):
            item = QTableWidgetItem(value)
            if column == 1:
                item.setForeground(QBrush(QColor(TONES["info"][1])))
                item.setBackground(QBrush(QColor(TONES["info"][0])))
            self.history.setItem(row, column, item)
        self.history.scrollToBottom()
        self.script.start(steps)

    def _script_state(self, state: str) -> None:
        row = self.history.rowCount() - 1
        if row < 0:
            return
        values = {1: state}
        if not self.script.active:
            code = self.script.exit_code
            values |= {3: f"{self.script.elapsed:.1f}s", 4: "" if code is None else str(code)}
        for column, value in values.items():
            item = QTableWidgetItem(value)
            if column == 1:
                tone = HISTORY_TONES.get(state, "neutral")
                item.setForeground(QBrush(QColor(TONES[tone][1])))
                item.setBackground(QBrush(QColor(TONES[tone][0])))
            self.history.setItem(row, column, item)

    def install_web(self) -> None:
        self.run_script("Install web packages", [("npm", "ci")])

    def build_web(self) -> None:
        self.run_script("Build web page", [("npm", "run", "build")])

    def setup_tenant(self) -> None:
        public = self.tunnel_urls[0] if self.tunnel_urls else ""
        dialog = SetupDialog(self.settings, public, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        pwsh = ("pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File")
        self.run_script(
            "Tenant setup",
            [(*pwsh, "scripts/setup-tenant.ps1", *dialog.arguments(), "-DeviceCode")],
        )

    def init_graph(self) -> None:
        answer = QMessageBox.question(
            self,
            "Create Graph connection",
            "Create or update the demo Graph connection and schema?\n"
            "Schema registration can take up to 20 minutes.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.run_script("Create Graph connection", [(PYTHON, "mcp/server.py", "init")])

    def sync_graph(self) -> None:
        self.run_script("Index tickets", [(PYTHON, "mcp/server.py", "sync")])

    def run_tests(self) -> None:
        self.run_script("Run tests", [(PYTHON, "-m", "pytest", "-q")])

    def closeEvent(self, event: QCloseEvent) -> None:
        running = [runner for runner in (self.script, self.tunnel, self.server) if runner.active]
        if running:
            names = ", ".join(runner.name for runner in running)
            answer = QMessageBox.question(self, "Exit", f"Stop {names} and exit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for runner in running:
                runner.stop()
                runner.process.waitForFinished(5000)
        self.timer.stop()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
