import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from controller import MainWindow, Runner, SetupDialog
from PySide6.QtCore import QCoreApplication, QSettings
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        QCoreApplication.processEvents()
        time.sleep(0.02)


def run(runner, steps):
    lines = []
    runner.output.connect(lines.append)
    runner.start(steps)
    wait_until(lambda: not runner.active)
    return lines


def test_runner_runs_steps_in_order(app):
    runner = Runner("test")
    lines = run(
        runner,
        [(sys.executable, "-c", "print('first')"), (sys.executable, "-c", "print('second')")],
    )
    assert runner.state == "Succeeded" and runner.exit_code == 0
    assert [line for line in lines if line.endswith(("first", "second"))] == [
        "[test] first",
        "[test] second",
    ]


def test_runner_stops_after_failure(app):
    runner = Runner("test")
    lines = run(
        runner,
        [(sys.executable, "-c", "raise SystemExit(3)"), (sys.executable, "-c", "print('skip')")],
    )
    assert runner.state == "Failed" and runner.exit_code == 3
    assert not any(line.endswith("skip") for line in lines)


def test_runner_reports_missing_program(app):
    runner = Runner("test")
    lines = run(runner, [("definitely-not-a-real-program",)])
    assert runner.state == "Failed"
    assert "not found" in lines[0]


def test_stop_kills_child_processes(app, tmp_path):
    beat = tmp_path / "heartbeat"
    child = (
        f"import pathlib,time\nwhile True:\n pathlib.Path({str(beat)!r}).touch(); time.sleep(.05)"
    )
    parent = f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\n"
    runner = Runner("test")
    runner.start([(sys.executable, "-c", parent + "time.sleep(60)")])
    wait_until(beat.exists)
    runner.stop()
    wait_until(lambda: not runner.active)
    assert runner.state == "Stopped"
    time.sleep(0.3)
    last = beat.stat().st_mtime_ns
    time.sleep(0.5)
    assert beat.stat().st_mtime_ns == last


def test_setup_dialog_validates_and_builds_arguments(app, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = SetupDialog(settings, "https://demo-8000.devtunnels.ms", None)
    assert "GUID" in dialog.validation_error()
    values = {
        "TenantId": "11111111-1111-4111-8111-111111111111",
        "AliceUpn": "a@x.com",
        "BobUpn": "b@x.com",
        "EveUpn": "e@x.com",
    }
    for key, value in values.items():
        dialog.inputs[key].setText(value)
    assert dialog.validation_error() == ""
    assert dialog.arguments()[:4] == [
        "-TenantId",
        values["TenantId"],
        "-PublicUrl",
        "https://demo-8000.devtunnels.ms",
    ]
    dialog.inputs["BobUpn"].setText("A@x.com")
    assert "different" in dialog.validation_error()


def test_main_window_starts_with_services_stopped(app):
    window = MainWindow()
    window.refresh()
    assert not window.server_stop.isEnabled()
    assert not window.tunnel_stop.isEnabled()
    assert not window.cancel_button.isEnabled()
    assert window.script_buttons["tests"].isEnabled()
    window.close()
