import json
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
    QComboBox,
    QCheckBox,
    QDoubleSpinBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
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
    audio_cfg = data.setdefault("audio", {})

    defaults_obs = {
        "host": recordtest.DEFAULT_OBS_HOST,
        "port": recordtest.DEFAULT_OBS_PORT,
        "scene_name": recordtest.DEFAULT_OBS_SCENE_NAME,
        "source_name": recordtest.DEFAULT_OBS_SOURCE_NAME,
        "source_color": recordtest.DEFAULT_OBS_SOURCE_COLOR,
    }
    for key, value in defaults_obs.items():
        if obs.get(key) in (None, ""):
            obs[key] = value
            changed = True

    if str(obs.get("password", "")).strip() == "your_password_here":
        obs["password"] = ""
        changed = True
        notes.append("OBSパスワードのプレースホルダを空欄にしました")

    # OBSは配布同梱のポータブル版のみ利用する。
    if obs.get("dir") != recordtest.DEFAULT_OBS_DIR:
        obs["dir"] = recordtest.DEFAULT_OBS_DIR
        changed = True
        notes.append(f"OBSフォルダを固定しました: {recordtest.DEFAULT_OBS_DIR}")

    has_valid_dir = bool(recordtest.detect_obs_dir())

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
    elif not bool(app_cfg.get("setup_completed")) and has_valid_dir:
        app_cfg["setup_completed"] = True
        changed = True

    audio_defaults = recordtest.get_audio_config_defaults()
    for key, defaults in audio_defaults.items():
        slot = audio_cfg.setdefault(key, {})
        if not isinstance(slot, dict):
            audio_cfg[key] = {}
            slot = audio_cfg[key]
            changed = True
        for field, value in defaults.items():
            if slot.get(field) in (None, ""):
                slot[field] = value
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


def run_guided_auto_setup(config_data=None):
    report = run_preflight(config_data, auto_fix=True, force_obs_detect=True)
    if report.get("errors"):
        return report, None

    try:
        info = recordtest.setup_obs_sync_elements(report["config"])
    except recordtest.RecorderError as e:
        report["errors"].append(str(e))
        return report, None
    except Exception as e:
        report["errors"].append(f"{type(e).__name__}: {e}")
        return report, None

    report["config"].setdefault("app", {})["setup_completed"] = True
    save_config(report["config"])
    return report, info


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
            try:
                recordtest.apply_audio_profile_from_config(
                    self.recorder.client,
                    settings,
                    scene_name=recordtest.OBS_SCENE_NAME,
                )
                self.status.emit("🔊 音声設定をOBSへ適用しました。")
            except Exception as e:
                self.status.emit(f"⚠️ 音声設定の適用に失敗: {e}")

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
        self.resize(560, 420)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "配布同梱のポータブルOBSを前提に、必要な設定を自動構成します。\n"
            "保存先だけ確認して「環境を自動修復」を実行してください。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.fields = {
            "obs.dir": QLineEdit(),
            "obs.scene_name": QLineEdit(),
            "obs.source_name": QLineEdit(),
            "obs.source_color": QLineEdit(),
            "paths.recordings_dir": QLineEdit(),
            "paths.json_dir": QLineEdit(),
        }
        form.addRow("OBSフォルダ", self.fields["obs.dir"])
        form.addRow("シーン名", self.fields["obs.scene_name"])
        form.addRow("色ソース名", self.fields["obs.source_name"])
        form.addRow("色ソース色", self.fields["obs.source_color"])
        form.addRow("録画ディレクトリ", self.fields["paths.recordings_dir"])
        form.addRow("JSONディレクトリ", self.fields["paths.json_dir"])
        layout.addLayout(form)
        self.fields["obs.dir"].setReadOnly(True)

        action_row = QHBoxLayout()
        self.quick_fix_btn = QPushButton("環境を自動修復")
        self.quick_fix_btn.clicked.connect(self.run_quick_setup)
        action_row.addWidget(self.quick_fix_btn)

        self.test_btn = QPushButton("接続テスト")
        self.test_btn.clicked.connect(self.test_obs_connection)
        action_row.addWidget(self.test_btn)

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
        self.fields["obs.dir"].setText(recordtest.DEFAULT_OBS_DIR)
        self.fields["obs.scene_name"].setText(str(obs.get("scene_name", "")))
        self.fields["obs.source_name"].setText(str(obs.get("source_name", "")))
        self.fields["obs.source_color"].setText(recordtest.obs_color_to_hex(obs.get("source_color")))
        self.fields["paths.recordings_dir"].setText(str(paths.get("recordings_dir", "")))
        self.fields["paths.json_dir"].setText(str(paths.get("json_dir", "")))

    def collect_data(self):
        data = load_config()
        data.setdefault("obs", {})
        data.setdefault("paths", {})
        data.setdefault("app", {})

        data["obs"]["host"] = recordtest.DEFAULT_OBS_HOST
        data["obs"]["port"] = recordtest.DEFAULT_OBS_PORT
        data["obs"]["password"] = ""
        data["obs"]["dir"] = recordtest.DEFAULT_OBS_DIR
        data["obs"]["scene_name"] = self.fields["obs.scene_name"].text().strip()
        data["obs"]["source_name"] = self.fields["obs.source_name"].text().strip()
        data["obs"]["source_color"] = self.fields["obs.source_color"].text().strip()

        data["paths"]["recordings_dir"] = self.fields["paths.recordings_dir"].text().strip()
        data["paths"]["json_dir"] = self.fields["paths.json_dir"].text().strip()
        return data

    def test_obs_connection(self):
        data = self.collect_data()
        report = run_preflight(data, auto_fix=True, force_obs_detect=True)
        if report.get("changed"):
            save_config(report["config"])
            self.load_values()
        if report.get("errors"):
            QMessageBox.warning(self, "接続テスト", format_report_lines(report.get("errors", [])))
            return

        cfg = report["config"]
        host = cfg.get("obs", {}).get("host", recordtest.DEFAULT_OBS_HOST)
        port = cfg.get("obs", {}).get("port", recordtest.DEFAULT_OBS_PORT)
        password = cfg.get("obs", {}).get("password", "")
        ok, detail = recordtest.test_obs_connection(host, port, password)
        if ok:
            if report.get("changed"):
                save_config(cfg)
                self.load_values()
            QMessageBox.information(self, "接続テスト", detail)
        else:
            QMessageBox.warning(
                self,
                "接続テスト",
                f"接続に失敗しました。\n{detail}"
            )

    def run_quick_setup(self):
        data = self.collect_data()
        report, info = run_guided_auto_setup(data)
        if report.get("errors"):
            QMessageBox.critical(self, "環境修復", format_report_lines(report.get("errors", [])))
            return False

        if info is None:
            QMessageBox.critical(self, "環境修復", "初期化に失敗しました。")
            return False

        self.load_values()
        color_hex = recordtest.obs_color_to_hex(info.get("source_color"))
        launch_note = "（セットアップのためポータブルOBSを自動起動しました）" if info.get("obs_launched") else ""
        message = (
            "環境修復が完了しました。\n"
            f"シーン: {info.get('scene_name')}\n"
            f"色ソース: {info.get('source_name')} ({color_hex})"
        )
        if launch_note:
            message += f"\n{launch_note}"
        QMessageBox.information(self, "環境修復", message)
        return True

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
        if not self.run_quick_setup():
            return

        config = load_config()
        config.setdefault("app", {})["setup_completed"] = True
        save_config(config)
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
        self.audio_device_cache = {"desktop": [], "mic": []}
        self._audio_ui_loading = False
        self._audio_apply_timer = QTimer(self)
        self._audio_apply_timer.setSingleShot(True)
        self._audio_apply_timer.timeout.connect(self._apply_audio_settings_auto)

        back_btn = QPushButton("← 戻る")
        back_btn.clicked.connect(on_back)
        self.form.addRow(back_btn)

        self.fields["obs.host"] = QLineEdit()
        self.fields["obs.dir"] = QLineEdit()
        self.fields["obs.password"] = QLineEdit()
        self.fields["obs.port"] = QLineEdit()
        self.fields["obs.scene_name"] = QLineEdit()
        self.fields["obs.source_name"] = QLineEdit()
        self.fields["obs.source_color"] = QLineEdit()

        self.fields["paths.recordings_dir"] = QLineEdit()
        self.fields["paths.json_dir"] = QLineEdit()
        self.fields["paths.champion_icons_dir"] = QLineEdit()
        self.fields["storage.max_size_gb"] = QLineEdit()
        self.fields["polling.end_error_limit"] = QLineEdit()
        self.fields["polling.end_poll_sec"] = QLineEdit()
        self.fields["polling.event_poll_sec"] = QLineEdit()
        self.fields["obs.host"].setReadOnly(True)
        self.fields["obs.dir"].setReadOnly(True)
        self.fields["obs.password"].setReadOnly(True)
        self.fields["obs.port"].setReadOnly(True)

        self.form.addRow("OBSフォルダ(固定)", self.fields["obs.dir"])
        self.form.addRow("録画ディレクトリ", self.fields["paths.recordings_dir"])
        self.form.addRow("JSONディレクトリ", self.fields["paths.json_dir"])
        self.form.addRow("アイコンディレクトリ", self.fields["paths.champion_icons_dir"])
        self.form.addRow("最大容量(GB)", self.fields["storage.max_size_gb"])

        self.audio_desktop_device = QComboBox()
        self.audio_desktop_volume = QDoubleSpinBox()
        self.audio_desktop_volume.setRange(-60.0, 20.0)
        self.audio_desktop_volume.setDecimals(1)
        self.audio_desktop_volume.setSingleStep(0.5)
        self.audio_desktop_mute = QCheckBox("ミュート")

        self.audio_mic_device = QComboBox()
        self.audio_mic_volume = QDoubleSpinBox()
        self.audio_mic_volume.setRange(-60.0, 20.0)
        self.audio_mic_volume.setDecimals(1)
        self.audio_mic_volume.setSingleStep(0.5)
        self.audio_mic_mute = QCheckBox("ミュート")

        self.audio_refresh_btn = QPushButton("音声デバイス一覧を更新")
        self.audio_refresh_btn.clicked.connect(self.refresh_audio_devices)
        self.audio_apply_btn = QPushButton("音声設定をOBSへ反映")
        self.audio_apply_btn.clicked.connect(self.apply_audio_settings_to_obs)

        self.form.addRow(QLabel("---- 音声設定 (OBSを開かずに設定) ----"))
        self.form.addRow("デスクトップ音声デバイス", self.audio_desktop_device)
        self.form.addRow("デスクトップ音量 (dB)", self.audio_desktop_volume)
        self.form.addRow("", self.audio_desktop_mute)
        self.form.addRow("マイク入力デバイス", self.audio_mic_device)
        self.form.addRow("マイク音量 (dB)", self.audio_mic_volume)
        self.form.addRow("", self.audio_mic_mute)
        self.form.addRow(self.audio_refresh_btn)
        self.form.addRow(self.audio_apply_btn)

        self.advanced_toggle_btn = QPushButton("詳細設定を表示")
        self.advanced_toggle_btn.clicked.connect(self.toggle_advanced_settings)
        self.form.addRow(self.advanced_toggle_btn)

        self.advanced_widget = QWidget()
        advanced_form = QFormLayout(self.advanced_widget)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.addRow("OBSホスト", self.fields["obs.host"])
        advanced_form.addRow("シーン名", self.fields["obs.scene_name"])
        advanced_form.addRow("ソース名", self.fields["obs.source_name"])
        advanced_form.addRow("ソース色", self.fields["obs.source_color"])
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

        self.quick_fix_btn = QPushButton("環境を自動修復")
        self.quick_fix_btn.clicked.connect(self.run_quick_setup)
        self.form.addRow(self.quick_fix_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Reset)
        buttons.accepted.connect(self.save_settings)
        buttons.rejected.connect(self.load_settings)
        self.form.addRow(buttons)

        self.audio_desktop_device.currentIndexChanged.connect(self.queue_audio_auto_apply)
        self.audio_desktop_volume.valueChanged.connect(self.queue_audio_auto_apply)
        self.audio_desktop_mute.stateChanged.connect(self.queue_audio_auto_apply)
        self.audio_mic_device.currentIndexChanged.connect(self.queue_audio_auto_apply)
        self.audio_mic_volume.valueChanged.connect(self.queue_audio_auto_apply)
        self.audio_mic_mute.stateChanged.connect(self.queue_audio_auto_apply)

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
        audio = data.get("audio", {})
        desktop_audio = audio.get("desktop", {})
        mic_audio = audio.get("mic", {})

        self._audio_ui_loading = True
        self.fields["obs.host"].setText(recordtest.DEFAULT_OBS_HOST)
        self.fields["obs.dir"].setText(recordtest.DEFAULT_OBS_DIR)
        self.fields["obs.password"].setText("")
        self.fields["obs.port"].setText(str(recordtest.DEFAULT_OBS_PORT))
        self.fields["obs.scene_name"].setText(str(obs.get("scene_name", "")))
        self.fields["obs.source_name"].setText(str(obs.get("source_name", "")))
        self.fields["obs.source_color"].setText(recordtest.obs_color_to_hex(obs.get("source_color")))
        self.fields["paths.recordings_dir"].setText(str(paths.get("recordings_dir", "")))
        self.fields["paths.json_dir"].setText(str(paths.get("json_dir", "")))
        self.fields["paths.champion_icons_dir"].setText(str(paths.get("champion_icons_dir", "")))
        self.fields["storage.max_size_gb"].setText(str(storage.get("max_size_gb", "")))
        self.fields["polling.end_error_limit"].setText(str(polling.get("end_error_limit", "")))
        self.fields["polling.end_poll_sec"].setText(str(polling.get("end_poll_sec", "")))
        self.fields["polling.event_poll_sec"].setText(str(polling.get("event_poll_sec", "")))
        self._set_audio_ui_from_config("desktop", desktop_audio)
        self._set_audio_ui_from_config("mic", mic_audio)
        self._audio_ui_loading = False

    def save_settings(self):
        data = load_config()
        data.setdefault("obs", {})
        data.setdefault("paths", {})
        data.setdefault("storage", {})
        data.setdefault("polling", {})
        data.setdefault("app", {})
        data.setdefault("audio", {})

        data["obs"]["host"] = recordtest.DEFAULT_OBS_HOST
        data["obs"]["dir"] = recordtest.DEFAULT_OBS_DIR
        data["obs"]["password"] = ""
        data["obs"]["port"] = recordtest.DEFAULT_OBS_PORT
        data["obs"]["scene_name"] = self.fields["obs.scene_name"].text().strip()
        data["obs"]["source_name"] = self.fields["obs.source_name"].text().strip()
        data["obs"]["source_color"] = self.fields["obs.source_color"].text().strip()

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
        self._write_audio_settings_to_config(data)

        report = run_preflight(data, auto_fix=True, force_obs_detect=False)
        if report.get("errors"):
            QMessageBox.critical(self, "保存エラー", format_report_lines(report.get("errors", [])))
            return

        report["config"]["app"]["setup_completed"] = True
        save_config(report["config"])
        self.load_settings()
        QMessageBox.information(self, "設定保存", "設定を保存しました。")

    def _get_audio_widgets(self, key):
        if key == "desktop":
            return self.audio_desktop_device, self.audio_desktop_volume, self.audio_desktop_mute
        if key == "mic":
            return self.audio_mic_device, self.audio_mic_volume, self.audio_mic_mute
        raise KeyError(key)

    def _add_or_update_audio_combo_items(self, combo, items):
        current_id = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for item in items:
            label = f"{item.get('name', '')} [{item.get('id', '')}]".strip()
            combo.addItem(label, item.get("id"))
        if current_id:
            self._select_combo_by_data(combo, current_id)
        combo.blockSignals(False)

    def _select_combo_by_data(self, combo, value):
        if value is None:
            return False
        value = str(value)
        for idx in range(combo.count()):
            if str(combo.itemData(idx)) == value:
                combo.setCurrentIndex(idx)
                return True
        return False

    def _set_audio_ui_from_config(self, key, slot_cfg):
        defaults = recordtest.get_audio_config_defaults()
        slot = dict(defaults.get(key, {}))
        if isinstance(slot_cfg, dict):
            slot.update(slot_cfg)
        combo, volume, mute = self._get_audio_widgets(key)

        device_id = str(slot.get("device_id") or recordtest.DEFAULT_AUDIO_DEVICE_ID)
        device_name = str(slot.get("device_name") or recordtest.DEFAULT_AUDIO_DEVICE_NAME)
        fallback_item = [{"id": device_id, "name": device_name}]
        if not self.audio_device_cache.get(key):
            self.audio_device_cache[key] = fallback_item
        self._add_or_update_audio_combo_items(combo, self.audio_device_cache.get(key) or fallback_item)
        if not self._select_combo_by_data(combo, device_id):
            combo.addItem(f"{device_name} [{device_id}]", device_id)
            self._select_combo_by_data(combo, device_id)

        try:
            volume.setValue(float(slot.get("volume_db", 0.0)))
        except Exception:
            volume.setValue(0.0)
        mute.setChecked(bool(slot.get("mute", False)))

    def _read_audio_slot_from_ui(self, key):
        combo, volume, mute = self._get_audio_widgets(key)
        defaults = recordtest.get_audio_config_defaults()[key]
        device_id = combo.currentData()
        if device_id in (None, ""):
            device_id = defaults["device_id"]
        device_name = combo.currentText().strip()
        if " [" in device_name and device_name.endswith("]"):
            device_name = device_name.rsplit(" [", 1)[0].strip()
        if not device_name:
            device_name = defaults["device_name"]
        return {
            "input_name": defaults["input_name"],
            "device_id": str(device_id),
            "device_name": device_name,
            "volume_db": float(volume.value()),
            "mute": bool(mute.isChecked()),
        }

    def _write_audio_settings_to_config(self, data):
        audio = data.setdefault("audio", {})
        audio["desktop"] = self._read_audio_slot_from_ui("desktop")
        audio["mic"] = self._read_audio_slot_from_ui("mic")

    def _collect_settings_data_from_ui(self):
        data = load_config()
        data.setdefault("obs", {})
        data.setdefault("paths", {})
        data.setdefault("storage", {})
        data.setdefault("polling", {})
        data.setdefault("audio", {})

        data["obs"]["host"] = recordtest.DEFAULT_OBS_HOST
        data["obs"]["dir"] = recordtest.DEFAULT_OBS_DIR
        data["obs"]["password"] = ""
        data["obs"]["port"] = recordtest.DEFAULT_OBS_PORT
        data["obs"]["scene_name"] = self.fields["obs.scene_name"].text().strip()
        data["obs"]["source_name"] = self.fields["obs.source_name"].text().strip()
        data["obs"]["source_color"] = self.fields["obs.source_color"].text().strip()
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
        self._write_audio_settings_to_config(data)
        return data

    def queue_audio_auto_apply(self, *_args):
        if self._audio_ui_loading:
            return
        self._audio_apply_timer.start(350)

    def _apply_audio_settings_auto(self):
        self.apply_audio_settings_to_obs(show_success=False, show_error=False, auto_launch=False)

    def _open_obs_client_for_audio(self, auto_launch=False):
        data = self._collect_settings_data_from_ui()
        report = run_preflight(data, auto_fix=True, force_obs_detect=True)
        if report.get("changed"):
            save_config(report["config"])
        if report.get("errors"):
            raise recordtest.RecorderError("\n".join(report.get("errors", [])))

        cfg = report["config"]
        recordtest.apply_settings(cfg)
        recordtest.setup_environment()

        ok, _detail = recordtest.test_obs_connection(
            cfg.get("obs", {}).get("host", recordtest.DEFAULT_OBS_HOST),
            cfg.get("obs", {}).get("port", recordtest.DEFAULT_OBS_PORT),
            cfg.get("obs", {}).get("password", ""),
            timeout=1.5,
        )

        launched_process = None
        if not ok and auto_launch:
            launched_process = recordtest.launch_obs()

        client, _used_host = recordtest.connect_obs_client(
            cfg.get("obs", {}).get("host", recordtest.DEFAULT_OBS_HOST),
            cfg.get("obs", {}).get("port", recordtest.DEFAULT_OBS_PORT),
            cfg.get("obs", {}).get("password", ""),
            timeout=2.5,
        )
        return client, launched_process, cfg

    def refresh_audio_devices(self):
        client = None
        launched_process = None
        try:
            client, launched_process, cfg = self._open_obs_client_for_audio(auto_launch=True)
            catalog = recordtest.get_audio_device_catalog(
                client,
                cfg=cfg,
                scene_name=cfg.get("obs", {}).get("scene_name", recordtest.DEFAULT_OBS_SCENE_NAME),
            )
            for key in ("desktop", "mic"):
                self.audio_device_cache[key] = list(catalog.get(key, []))
            self._audio_ui_loading = True
            self._set_audio_ui_from_config("desktop", cfg.get("audio", {}).get("desktop", {}))
            self._set_audio_ui_from_config("mic", cfg.get("audio", {}).get("mic", {}))
            self._audio_ui_loading = False
            save_config(cfg)
            msg = "OBSが認識している音声デバイス一覧を更新しました。"
            if launched_process:
                msg += "\n（ポータブルOBSをバックグラウンドで起動しました）"
            QMessageBox.information(self, "音声デバイス一覧", msg)
        except Exception as e:
            self._audio_ui_loading = False
            QMessageBox.warning(self, "音声デバイス一覧", f"取得に失敗しました。\n{e}")
        finally:
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def apply_audio_settings_to_obs(self, show_success=True, show_error=True, auto_launch=True):
        client = None
        launched_process = None
        try:
            data = self._collect_settings_data_from_ui()
            report = run_preflight(data, auto_fix=True, force_obs_detect=False)
            if report.get("errors"):
                raise recordtest.RecorderError("\n".join(report.get("errors", [])))
            cfg = report["config"]
            save_config(cfg)

            client, launched_process, cfg = self._open_obs_client_for_audio(auto_launch=auto_launch)
            recordtest.apply_audio_profile_from_config(
                client,
                cfg,
                scene_name=cfg.get("obs", {}).get("scene_name", recordtest.DEFAULT_OBS_SCENE_NAME),
            )
            if show_success:
                msg = "音声設定をOBSへ反映しました。"
                if launched_process:
                    msg += "\n（ポータブルOBSをバックグラウンドで起動しました）"
                QMessageBox.information(self, "音声設定", msg)
            return True
        except Exception as e:
            if show_error:
                QMessageBox.warning(self, "音声設定", f"OBSへの反映に失敗しました。\n{e}")
            return False
        finally:
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass

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

    def run_quick_setup(self):
        data = load_config()
        report, info = run_guided_auto_setup(data)
        if report.get("errors"):
            QMessageBox.critical(self, "環境修復", format_report_lines(report.get("errors", [])))
            return

        if info is None:
            QMessageBox.critical(self, "環境修復", "初期化に失敗しました。")
            return

        self.load_settings()
        color_hex = recordtest.obs_color_to_hex(info.get("source_color"))
        launch_note = "（セットアップのためポータブルOBSを自動起動しました）" if info.get("obs_launched") else ""
        message = (
            "環境修復が完了しました。\n"
            f"シーン: {info.get('scene_name')}\n"
            f"色ソース: {info.get('source_name')} ({color_hex})"
        )
        if launch_note:
            message += f"\n{launch_note}"
        if report.get("warnings"):
            message += f"\n\n警告:\n{format_report_lines(report.get('warnings', []))}"
        QMessageBox.information(self, "環境修復", message)


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
        QLineEdit, QPlainTextEdit, QListWidget, QComboBox, QDoubleSpinBox {
            background-color: #242424; color: #e0e0e0; border: 1px solid #3a3a3a;
        }
        QSlider::groove:horizontal { height: 6px; background: #3a3a3a; }
        QSlider::handle:horizontal { background: #e0e0e0; width: 12px; margin: -4px 0; border-radius: 6px; }
    """)
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
