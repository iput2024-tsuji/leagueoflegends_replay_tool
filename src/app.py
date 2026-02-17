import json
import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QStackedWidget,
    QPlainTextEdit,
    QFormLayout,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon

try:
    from . import recordtest
    from .player import PlayerWidget
    from .app_paths import get_app_root
except ImportError:
    SRC_DIR = Path(__file__).resolve().parent
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import recordtest
    from player import PlayerWidget
    from app_paths import get_app_root


ROOT_DIR = get_app_root()
CONFIG_PATH = ROOT_DIR / "config" / "setting.json"
SAMPLE_CONFIG_PATH = ROOT_DIR / "config" / "setting.sample.json"
APP_ICON_CANDIDATES = [
    ROOT_DIR / "assets" / "app" / "app.ico",
    ROOT_DIR / "assets" / "app" / "app.png",
]


def get_app_icon():
    for path in APP_ICON_CANDIDATES:
        if path.exists():
            return QIcon(str(path))
    return None


def _normalize_obs_dir(path_value):
    if not path_value:
        return None
    path_text = os.path.expandvars(str(path_value).strip())
    if not path_text:
        return None
    return path_text


def apply_auto_defaults(data, force_obs_detect=False):
    changed = False
    notes = []

    if not isinstance(data, dict):
        data = {}
        changed = True

    obs = data.setdefault("obs", {})
    paths = data.setdefault("paths", {})
    polling = data.setdefault("polling", {})
    storage = data.setdefault("storage", {})
    app_cfg = data.setdefault("app", {})

    defaults_obs = {
        "host": recordtest.DEFAULT_OBS_HOST,
        "port": recordtest.DEFAULT_OBS_PORT,
        "scene_name": recordtest.DEFAULT_OBS_SCENE_NAME,
        "source_name": recordtest.DEFAULT_OBS_SOURCE_NAME,
    }
    for key, value in defaults_obs.items():
        if obs.get(key) in (None, ""):
            obs[key] = value
            changed = True

    if str(obs.get("password", "")).strip() == "your_password_here":
        obs["password"] = ""
        changed = True
        notes.append("OBSパスワードのプレースホルダを空欄にしました")

    current_obs_dir = _normalize_obs_dir(obs.get("dir"))
    has_valid_dir = bool(current_obs_dir and recordtest.is_valid_obs_dir(current_obs_dir))
    detected_obs_dir = recordtest.detect_obs_dir()
    if force_obs_detect and detected_obs_dir:
        if current_obs_dir != detected_obs_dir:
            obs["dir"] = detected_obs_dir
            changed = True
            notes.append(f"OBSフォルダを自動検出しました: {detected_obs_dir}")
            has_valid_dir = True
    elif not has_valid_dir and detected_obs_dir:
        obs["dir"] = detected_obs_dir
        changed = True
        notes.append(f"OBSフォルダを自動検出しました: {detected_obs_dir}")
        has_valid_dir = True
    elif not current_obs_dir:
        obs["dir"] = recordtest.DEFAULT_OBS_DIR
        changed = True

    defaults_paths = {
        "bin_dir": recordtest.DEFAULT_BIN_DIR,
        "recordings_dir": recordtest.DEFAULT_RECORDINGS_DIR,
        "json_dir": recordtest.DEFAULT_JSON_DIR,
        "champion_icons_dir": recordtest.DEFAULT_CHAMPION_ICONS_DIR,
        "champion_aliases_path": "config/champion_aliases.json",
    }
    for key, value in defaults_paths.items():
        if paths.get(key) in (None, ""):
            paths[key] = value
            changed = True

    defaults_polling = {
        "end_error_limit": recordtest.DEFAULT_END_ERROR_LIMIT,
        "end_poll_sec": recordtest.DEFAULT_END_POLL_SEC,
        "event_poll_sec": recordtest.DEFAULT_EVENT_POLL_SEC,
    }
    for key, value in defaults_polling.items():
        if polling.get(key) in (None, ""):
            polling[key] = value
            changed = True

    if storage.get("max_size_gb") in (None, ""):
        storage["max_size_gb"] = recordtest.DEFAULT_MAX_STORAGE_GB
        changed = True

    if app_cfg.get("setup_completed") is None:
        app_cfg["setup_completed"] = bool(has_valid_dir)
        changed = True

    return data, changed, notes


def format_report_lines(lines):
    if not lines:
        return "- なし"
    return "\n".join(f"- {line}" for line in lines)


def run_preflight(config_data=None, auto_fix=True, force_obs_detect=True):
    data = config_data if config_data is not None else load_config()
    data, changed_defaults, default_notes = apply_auto_defaults(data, force_obs_detect=force_obs_detect)
    report = recordtest.run_preflight_checks(data, auto_fix=auto_fix, ensure_dirs=True)
    report["changed"] = bool(changed_defaults or report.get("changed"))
    report["notes"] = list(default_notes) + list(report.get("notes", []))
    return report


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SAMPLE_CONFIG_PATH.exists():
            CONFIG_PATH.write_text(SAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            CONFIG_PATH.write_text(json.dumps({}, indent=4), encoding="utf-8")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data, changed, _ = apply_auto_defaults(data, force_obs_detect=False)
    if changed:
        save_config(data)
    return data


def save_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


class RecorderWorker(QThread):
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.stop_flag = False
        self.recorder = None

    def run(self):
        try:
            settings = load_config()
            report = run_preflight(settings, auto_fix=True, force_obs_detect=True)
            if report.get("changed"):
                save_config(report["config"])

            for note in report.get("notes", []):
                self.status.emit(f"🛠️ {note}")
            for warning in report.get("warnings", []):
                self.status.emit(f"⚠️ {warning}")

            errors = report.get("errors", [])
            if errors:
                raise recordtest.RecorderError("\n".join(errors))

            settings = report["config"]
            recordtest.apply_settings(settings)
            recordtest.setup_environment()
            obs_process = recordtest.launch_obs()

            self.recorder = recordtest.LoLAutoRecorder(
                obs_process=obs_process,
                status_cb=self.status.emit
            )

            while not self.stop_flag:
                self.recorder.reset_session()
                started = self.recorder.wait_for_game_start()
                if not started or self.stop_flag:
                    break
                self.recorder.start_recording()
                if self.stop_flag:
                    break
                self.recorder.record_until_end()
                self.recorder.stop_recording()
                if self.recorder.has_session_data():
                    self.recorder.save_json()
                self.status.emit("✅ 試合記録完了。次の試合を待機します。")
        except recordtest.RecorderError as e:
            message = str(e).strip() or "録画処理でエラーが発生しました。"
            first_line = message.splitlines()[0]
            self.status.emit(f"❌ {first_line}")
            self.error.emit(message)
        except BaseException as e:
            message = f"{type(e).__name__}: {e}"
            self.status.emit(f"❌ {message}")
            self.error.emit(message)
        finally:
            if self.recorder:
                self.recorder.request_stop()
                self.recorder.stop_recording()
                if self.recorder.has_session_data():
                    self.recorder.save_json()
                self.recorder.shutdown_obs()
            self.finished.emit()

    def stop(self):
        self.stop_flag = True
        if self.recorder:
            self.recorder.request_stop()


class SetupWizardDialog(QDialog):
    def __init__(self, parent=None, startup_mode=False):
        super().__init__(parent)
        self.startup_mode = startup_mode
        self.setWindowTitle("初回セットアップ")
        self.resize(520, 320)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "必要な設定を自動補完します。\n"
            "OBSフォルダ・WebSocket設定・保存先を確認して保存してください。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.fields = {
            "obs.dir": QLineEdit(),
            "obs.port": QLineEdit(),
            "obs.password": QLineEdit(),
            "paths.recordings_dir": QLineEdit(),
            "paths.json_dir": QLineEdit(),
        }
        form.addRow("OBSフォルダ", self.fields["obs.dir"])
        form.addRow("OBSポート", self.fields["obs.port"])
        form.addRow("OBSパスワード", self.fields["obs.password"])
        form.addRow("録画ディレクトリ", self.fields["paths.recordings_dir"])
        form.addRow("JSONディレクトリ", self.fields["paths.json_dir"])
        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.detect_btn = QPushButton("OBSを自動検出")
        self.detect_btn.clicked.connect(self.auto_detect_obs_dir)
        action_row.addWidget(self.detect_btn)

        self.test_btn = QPushButton("接続テスト")
        self.test_btn.clicked.connect(self.test_obs_connection)
        action_row.addWidget(self.test_btn)

        self.preflight_btn = QPushButton("自動診断")
        self.preflight_btn.clicked.connect(self.run_diagnosis)
        action_row.addWidget(self.preflight_btn)
        layout.addLayout(action_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setText("保存して開始" if self.startup_mode else "保存")
        layout.addWidget(buttons)

        self.load_values()

    def load_values(self):
        data = load_config()
        obs = data.get("obs", {})
        paths = data.get("paths", {})
        self.fields["obs.dir"].setText(str(obs.get("dir", "")))
        self.fields["obs.port"].setText(str(obs.get("port", "")))
        self.fields["obs.password"].setText(str(obs.get("password", "")))
        self.fields["paths.recordings_dir"].setText(str(paths.get("recordings_dir", "")))
        self.fields["paths.json_dir"].setText(str(paths.get("json_dir", "")))

    def collect_data(self):
        data = load_config()
        data.setdefault("obs", {})
        data.setdefault("paths", {})
        data.setdefault("app", {})

        data["obs"]["dir"] = self.fields["obs.dir"].text().strip()
        data["obs"]["password"] = self.fields["obs.password"].text().strip()
        try:
            data["obs"]["port"] = int(self.fields["obs.port"].text().strip())
        except ValueError:
            data["obs"]["port"] = self.fields["obs.port"].text().strip()

        data["paths"]["recordings_dir"] = self.fields["paths.recordings_dir"].text().strip()
        data["paths"]["json_dir"] = self.fields["paths.json_dir"].text().strip()
        return data

    def auto_detect_obs_dir(self):
        detected = recordtest.detect_obs_dir()
        if not detected:
            QMessageBox.warning(self, "自動検出", "OBSフォルダを検出できませんでした。手動で指定してください。")
            return
        self.fields["obs.dir"].setText(detected)
        QMessageBox.information(self, "自動検出", f"OBSフォルダを検出しました。\n{detected}")

    def test_obs_connection(self):
        data = self.collect_data()
        host = data.get("obs", {}).get("host", recordtest.DEFAULT_OBS_HOST)
        port = data.get("obs", {}).get("port", recordtest.DEFAULT_OBS_PORT)
        password = data.get("obs", {}).get("password", "")
        ok, detail = recordtest.test_obs_connection(host, port, password)
        if ok:
            QMessageBox.information(self, "接続テスト", detail)
        else:
            QMessageBox.warning(
                self,
                "接続テスト",
                f"接続に失敗しました。\n{detail}\n\nOBS起動後に再試行してください。"
            )

    def run_diagnosis(self):
        data = self.collect_data()
        report = run_preflight(data, auto_fix=True, force_obs_detect=True)
        if report.get("changed"):
            save_config(report["config"])
        self.load_values()

        message = (
            f"修正内容:\n{format_report_lines(report.get('notes', []))}\n\n"
            f"警告:\n{format_report_lines(report.get('warnings', []))}"
        )
        if report.get("errors"):
            message += f"\n\nエラー:\n{format_report_lines(report.get('errors', []))}"
            QMessageBox.warning(self, "自動診断", message)
        else:
            QMessageBox.information(self, "自動診断", message)

    def save_and_accept(self):
        data = self.collect_data()
        report = run_preflight(data, auto_fix=True, force_obs_detect=True)
        report["config"].setdefault("app", {})["setup_completed"] = True

        if report.get("errors"):
            QMessageBox.critical(self, "保存できません", format_report_lines(report.get("errors", [])))
            return

        save_config(report["config"])
        self.accept()


class HomePage(QWidget):
    def __init__(self, on_record, on_play, on_settings):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("LoL Replay Tool")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        exit_btn = QPushButton("アプリを終了")
        exit_btn.setFixedHeight(38)
        exit_btn.clicked.connect(QApplication.instance().quit)
        layout.addWidget(exit_btn)

        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(12)

        record_btn = QPushButton("録画を開始")
        record_btn.setFixedHeight(50)
        record_btn.clicked.connect(on_record)
        btn_layout.addWidget(record_btn)

        play_btn = QPushButton("リプレイを再生")
        play_btn.setFixedHeight(50)
        play_btn.clicked.connect(on_play)
        btn_layout.addWidget(play_btn)

        settings_btn = QPushButton("設定")
        settings_btn.setFixedHeight(40)
        settings_btn.clicked.connect(on_settings)
        btn_layout.addWidget(settings_btn)

        layout.addLayout(btn_layout)


class RecorderPage(QWidget):
    def __init__(self, on_back):
        super().__init__()
        self.worker = None

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        back_btn = QPushButton("← 戻る")
        back_btn.clicked.connect(on_back)
        header.addWidget(back_btn)

        self.status_label = QLabel("待機中")
        header.addWidget(self.status_label)
        header.addStretch(1)
        layout.addLayout(header)

        button_row = QHBoxLayout()
        self.start_btn = QPushButton("録画待機開始")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        button_row.addWidget(self.start_btn)
        button_row.addWidget(self.stop_btn)
        layout.addLayout(button_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, stretch=1)

        self.start_btn.clicked.connect(self.start_worker)
        self.stop_btn.clicked.connect(self.stop_worker)

    def append_log(self, text):
        self.log_view.appendPlainText(text)
        self.status_label.setText(text)

    def start_worker(self):
        if self.worker and self.worker.isRunning():
            return
        self.worker = RecorderWorker()
        self.worker.status.connect(self.append_log)
        self.worker.error.connect(self.on_worker_error)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.append_log("録画待機を開始しました。")

    def stop_worker(self):
        if self.worker:
            self.worker.stop()
            self.append_log("停止リクエスト送信…")

    def on_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.append_log("録画待機を終了しました。")

    def on_worker_error(self, message):
        detail = (
            f"{message}\n\n"
            "設定画面で OBSフォルダ・ポート・パスワードを確認してください。"
        )
        QMessageBox.critical(self, "録画エラー", detail)


class PlayerPage(QWidget):
    def __init__(self, on_back):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QHBoxLayout()
        self.header.setContentsMargins(8, 6, 8, 6)
        self.back_btn = QPushButton("← 戻る")
        self.back_btn.clicked.connect(on_back)
        self.header.addWidget(self.back_btn)

        self.open_btn = QPushButton("JSONを選択")
        self.open_btn.clicked.connect(self.open_selector)
        self.header.addWidget(self.open_btn)

        self.header.addStretch(1)
        layout.addLayout(self.header)

        self.player_widget = PlayerWidget(auto_open=False, fullscreen_cb=self.handle_fullscreen)
        layout.addWidget(self.player_widget, stretch=1)

    def open_selector(self):
        self.player_widget.open_replay_selector()

    def handle_fullscreen(self, enabled):
        window = self.window()
        if enabled:
            self.back_btn.hide()
            self.open_btn.hide()
            if window:
                window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
                window.setStyleSheet("background-color: black;")
                window.showFullScreen()
        else:
            if window:
                window.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
                window.setStyleSheet("")
                window.showNormal()
            self.back_btn.show()
            self.open_btn.show()

    def on_leave(self):
        self.player_widget.stop_playback()


class SettingsPage(QWidget):
    def __init__(self, on_back):
        super().__init__()
        self.form = QFormLayout(self)
        self.fields = {}
        self.advanced_visible = False

        back_btn = QPushButton("← 戻る")
        back_btn.clicked.connect(on_back)
        self.form.addRow(back_btn)

        self.fields["obs.host"] = QLineEdit()
        self.fields["obs.dir"] = QLineEdit()
        self.fields["obs.password"] = QLineEdit()
        self.fields["obs.port"] = QLineEdit()
        self.fields["obs.scene_name"] = QLineEdit()
        self.fields["obs.source_name"] = QLineEdit()

        self.fields["paths.recordings_dir"] = QLineEdit()
        self.fields["paths.json_dir"] = QLineEdit()
        self.fields["paths.champion_icons_dir"] = QLineEdit()
        self.fields["storage.max_size_gb"] = QLineEdit()
        self.fields["polling.end_error_limit"] = QLineEdit()
        self.fields["polling.end_poll_sec"] = QLineEdit()
        self.fields["polling.event_poll_sec"] = QLineEdit()

        self.form.addRow("OBSフォルダ", self.fields["obs.dir"])
        self.form.addRow("OBSパスワード", self.fields["obs.password"])
        self.form.addRow("OBSポート", self.fields["obs.port"])
        self.form.addRow("録画ディレクトリ", self.fields["paths.recordings_dir"])
        self.form.addRow("JSONディレクトリ", self.fields["paths.json_dir"])
        self.form.addRow("アイコンディレクトリ", self.fields["paths.champion_icons_dir"])
        self.form.addRow("最大容量(GB)", self.fields["storage.max_size_gb"])

        self.advanced_toggle_btn = QPushButton("詳細設定を表示")
        self.advanced_toggle_btn.clicked.connect(self.toggle_advanced_settings)
        self.form.addRow(self.advanced_toggle_btn)

        self.advanced_widget = QWidget()
        advanced_form = QFormLayout(self.advanced_widget)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.addRow("OBSホスト", self.fields["obs.host"])
        advanced_form.addRow("シーン名", self.fields["obs.scene_name"])
        advanced_form.addRow("ソース名", self.fields["obs.source_name"])
        advanced_form.addRow("終了検知エラー閾値", self.fields["polling.end_error_limit"])
        advanced_form.addRow("終了監視間隔(秒)", self.fields["polling.end_poll_sec"])
        advanced_form.addRow("イベント監視間隔(秒)", self.fields["polling.event_poll_sec"])
        self.advanced_widget.setVisible(False)
        self.form.addRow(self.advanced_widget)

        self.setup_btn = QPushButton("初回セットアップを開く")
        self.setup_btn.clicked.connect(self.open_setup_wizard)
        self.form.addRow(self.setup_btn)

        self.auto_fill_btn = QPushButton("設定を自動補完")
        self.auto_fill_btn.clicked.connect(self.auto_fill_settings)
        self.form.addRow(self.auto_fill_btn)

        self.preflight_btn = QPushButton("録画前チェックを実行")
        self.preflight_btn.clicked.connect(self.run_preflight_fix)
        self.form.addRow(self.preflight_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Reset)
        buttons.accepted.connect(self.save_settings)
        buttons.rejected.connect(self.load_settings)
        self.form.addRow(buttons)

        self.load_settings()

    def toggle_advanced_settings(self):
        self.advanced_visible = not self.advanced_visible
        self.advanced_widget.setVisible(self.advanced_visible)
        self.advanced_toggle_btn.setText("詳細設定を隠す" if self.advanced_visible else "詳細設定を表示")

    def load_settings(self):
        data = load_config()
        obs = data.get("obs", {})
        paths = data.get("paths", {})
        storage = data.get("storage", {})
        polling = data.get("polling", {})

        self.fields["obs.host"].setText(str(obs.get("host", "")))
        self.fields["obs.dir"].setText(str(obs.get("dir", "")))
        self.fields["obs.password"].setText(str(obs.get("password", "")))
        self.fields["obs.port"].setText(str(obs.get("port", "")))
        self.fields["obs.scene_name"].setText(str(obs.get("scene_name", "")))
        self.fields["obs.source_name"].setText(str(obs.get("source_name", "")))
        self.fields["paths.recordings_dir"].setText(str(paths.get("recordings_dir", "")))
        self.fields["paths.json_dir"].setText(str(paths.get("json_dir", "")))
        self.fields["paths.champion_icons_dir"].setText(str(paths.get("champion_icons_dir", "")))
        self.fields["storage.max_size_gb"].setText(str(storage.get("max_size_gb", "")))
        self.fields["polling.end_error_limit"].setText(str(polling.get("end_error_limit", "")))
        self.fields["polling.end_poll_sec"].setText(str(polling.get("end_poll_sec", "")))
        self.fields["polling.event_poll_sec"].setText(str(polling.get("event_poll_sec", "")))

    def save_settings(self):
        data = load_config()
        data.setdefault("obs", {})
        data.setdefault("paths", {})
        data.setdefault("storage", {})
        data.setdefault("polling", {})
        data.setdefault("app", {})

        data["obs"]["host"] = self.fields["obs.host"].text().strip()
        data["obs"]["dir"] = self.fields["obs.dir"].text().strip()
        data["obs"]["password"] = self.fields["obs.password"].text().strip()
        try:
            data["obs"]["port"] = int(self.fields["obs.port"].text().strip())
        except ValueError:
            pass
        data["obs"]["scene_name"] = self.fields["obs.scene_name"].text().strip()
        data["obs"]["source_name"] = self.fields["obs.source_name"].text().strip()

        data["paths"]["recordings_dir"] = self.fields["paths.recordings_dir"].text().strip()
        data["paths"]["json_dir"] = self.fields["paths.json_dir"].text().strip()
        data["paths"]["champion_icons_dir"] = self.fields["paths.champion_icons_dir"].text().strip()
        try:
            data["storage"]["max_size_gb"] = float(self.fields["storage.max_size_gb"].text().strip())
        except ValueError:
            pass
        try:
            data["polling"]["end_error_limit"] = int(self.fields["polling.end_error_limit"].text().strip())
        except ValueError:
            pass
        try:
            data["polling"]["end_poll_sec"] = float(self.fields["polling.end_poll_sec"].text().strip())
        except ValueError:
            pass
        try:
            data["polling"]["event_poll_sec"] = float(self.fields["polling.event_poll_sec"].text().strip())
        except ValueError:
            pass

        report = run_preflight(data, auto_fix=True, force_obs_detect=False)
        if report.get("errors"):
            QMessageBox.critical(self, "保存エラー", format_report_lines(report.get("errors", [])))
            return

        report["config"]["app"]["setup_completed"] = True
        save_config(report["config"])
        self.load_settings()
        QMessageBox.information(self, "設定保存", "設定を保存しました。")

    def auto_fill_settings(self):
        data = load_config()
        data, changed, notes = apply_auto_defaults(data, force_obs_detect=True)
        if changed:
            save_config(data)
        self.load_settings()

        if notes:
            note_text = "\n".join(f"- {line}" for line in notes)
        else:
            note_text = "- 変更なし"
        QMessageBox.information(self, "自動補完", f"設定の自動補完を実行しました。\n{note_text}")

    def run_preflight_fix(self):
        data = load_config()
        report = run_preflight(data, auto_fix=True, force_obs_detect=True)
        if report.get("changed"):
            save_config(report["config"])
        self.load_settings()

        message = (
            f"修正内容:\n{format_report_lines(report.get('notes', []))}\n\n"
            f"警告:\n{format_report_lines(report.get('warnings', []))}"
        )
        if report.get("errors"):
            message += f"\n\nエラー:\n{format_report_lines(report.get('errors', []))}"
            QMessageBox.warning(self, "録画前チェック", message)
        else:
            QMessageBox.information(self, "録画前チェック", message)

    def open_setup_wizard(self):
        dialog = SetupWizardDialog(self, startup_mode=False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.load_settings()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LoL Replay Tool")
        self.resize(1200, 720)
        icon = get_app_icon()
        if icon:
            self.setWindowIcon(icon)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.home_page = HomePage(
            on_record=self.show_recorder,
            on_play=self.show_player,
            on_settings=self.show_settings
        )
        self.recorder_page = RecorderPage(on_back=self.show_home)
        self.player_page = PlayerPage(on_back=self.show_home)
        self.settings_page = SettingsPage(on_back=self.show_home)

        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.recorder_page)
        self.stack.addWidget(self.player_page)
        self.stack.addWidget(self.settings_page)

        self.show_home()
        self.run_startup_setup()

    def show_home(self):
        self.player_page.on_leave()
        self.stack.setCurrentWidget(self.home_page)

    def show_recorder(self):
        self.player_page.on_leave()
        self.stack.setCurrentWidget(self.recorder_page)

    def show_player(self):
        self.stack.setCurrentWidget(self.player_page)
        self.player_page.open_selector()

    def show_settings(self):
        self.player_page.on_leave()
        self.stack.setCurrentWidget(self.settings_page)

    def run_startup_setup(self):
        data = load_config()
        report = run_preflight(data, auto_fix=True, force_obs_detect=True)
        if report.get("changed"):
            save_config(report["config"])
            self.settings_page.load_settings()

        setup_completed = bool(report["config"].get("app", {}).get("setup_completed"))
        has_errors = bool(report.get("errors"))

        if has_errors:
            QMessageBox.warning(
                self,
                "初回セットアップ",
                "設定に不足があります。初回セットアップを開きます。\n\n"
                f"{format_report_lines(report.get('errors', []))}"
            )

        if (not setup_completed) or has_errors:
            dialog = SetupWizardDialog(self, startup_mode=True)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.settings_page.load_settings()


def main():
    app = QApplication([])
    icon = get_app_icon()
    if icon:
        app.setWindowIcon(icon)
    app.setStyleSheet("""
        QWidget { background-color: #1e1e1e; color: #e0e0e0; }
        QLabel { color: #e0e0e0; }
        QPushButton { background-color: #2b2b2b; color: #e0e0e0; border: 1px solid #3a3a3a; padding: 6px 10px; }
        QPushButton:hover { background-color: #3a3a3a; }
        QLineEdit, QPlainTextEdit, QListWidget { background-color: #242424; color: #e0e0e0; border: 1px solid #3a3a3a; }
        QSlider::groove:horizontal { height: 6px; background: #3a3a3a; }
        QSlider::handle:horizontal { background: #e0e0e0; width: 12px; margin: -4px 0; border-radius: 6px; }
    """)
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
