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
    QFormLayout,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QComboBox,
    QCheckBox,
    QTabWidget,
    QSlider,
    QSystemTrayIcon,
    QMenu,
    QStyle,
    QFileDialog,
    QProgressBar,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon, QAction

try:
    from . import recordtest
    from .controllers import AudioSettingsController, ConfigController, RecordingController
    from .player import PlayerWidget
    from .app_paths import get_app_root
except ImportError:
    SRC_DIR = Path(__file__).resolve().parent
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import recordtest
    from controllers import AudioSettingsController, ConfigController, RecordingController
    from player import PlayerWidget
    from app_paths import get_app_root


ROOT_DIR = get_app_root()
APP_ICON_CANDIDATES = [
    ROOT_DIR / "assets" / "app" / "app.ico",
    ROOT_DIR / "assets" / "app" / "app.png",
]


def get_app_icon():
    for path in APP_ICON_CANDIDATES:
        if path.exists():
            return QIcon(str(path))
    return None


CONFIG_CONTROLLER = ConfigController()
AUDIO_CONTROLLER = AudioSettingsController(CONFIG_CONTROLLER)
RECORDING_CONTROLLER = RecordingController()


def apply_auto_defaults(data, force_obs_detect=False):
    return CONFIG_CONTROLLER.apply_auto_defaults(data, force_obs_detect=force_obs_detect)


def format_report_lines(lines):
    return CONFIG_CONTROLLER.format_report_lines(lines)


def run_preflight(config_data=None, auto_fix=True, force_obs_detect=True):
    return CONFIG_CONTROLLER.run_preflight(
        config_data,
        auto_fix=auto_fix,
        force_obs_detect=force_obs_detect,
    )


def run_guided_auto_setup(config_data=None):
    return CONFIG_CONTROLLER.run_guided_auto_setup(config_data)


def load_config():
    return CONFIG_CONTROLLER.load_config()


def save_config(data):
    CONFIG_CONTROLLER.save_config(data)


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
            self.recorder = RECORDING_CONTROLLER.create_recorder(settings, status_cb=self.status.emit)
            try:
                self.recorder.apply_audio_profile(self.recorder.config)
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
            "bin/OBS-Studio に配置されたポータブルOBSを前提に、必要な設定を自動構成します。\n"
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
        report, ok, detail = CONFIG_CONTROLLER.test_obs_connection(data)
        if report.get("changed"):
            save_config(report["config"])
            self.load_values()
        if report.get("errors"):
            QMessageBox.warning(self, "接続テスト", format_report_lines(report.get("errors", [])))
            return

        if ok:
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
    def __init__(self, on_play, on_settings):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("LoL Replay Tool")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        self.status_label = QLabel("⚪ 停止")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            "padding: 10px 14px; border-radius: 8px; "
            "background-color: #2d2d2d; color: #cfcfcf; border: 1px solid #3a3a3a;"
        )
        layout.addWidget(self.status_label)

        self.status_detail_label = QLabel("バックグラウンド監視を開始していません。")
        self.status_detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_detail_label.setWordWrap(True)
        self.status_detail_label.setStyleSheet("color: #9a9a9a;")
        layout.addWidget(self.status_detail_label)

        layout.addStretch(1)

        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(12)

        play_btn = QPushButton("リプレイを再生")
        play_btn.setFixedHeight(50)
        play_btn.clicked.connect(on_play)
        btn_layout.addWidget(play_btn)

        settings_btn = QPushButton("設定")
        settings_btn.setFixedHeight(40)
        settings_btn.clicked.connect(on_settings)
        btn_layout.addWidget(settings_btn)

        layout.addLayout(btn_layout)
        layout.addStretch(1)

    def set_recorder_status(self, badge_text, color_hex="#cfcfcf", detail_text=None):
        self.status_label.setText(badge_text)
        self.status_label.setStyleSheet(
            "padding: 10px 14px; border-radius: 8px; "
            f"background-color: #2d2d2d; color: {color_hex}; border: 1px solid #3a3a3a;"
        )
        if detail_text is not None:
            self.status_detail_label.setText(detail_text)
            self.status_detail_label.setToolTip(detail_text)


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
        return self.player_widget.open_replay_selector()

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
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)
        self.fields = {}
        self.audio_device_cache = {"desktop": [], "mic": []}
        self._audio_ui_loading = False
        self._audio_refresh_in_progress = False
        self._audio_auto_refreshed_once = False
        self._audio_apply_timer = QTimer(self)
        self._audio_apply_timer.setSingleShot(True)
        self._audio_apply_timer.timeout.connect(self._apply_audio_settings_auto)

        back_btn = QPushButton("← 戻る")
        back_btn.clicked.connect(on_back)
        root_layout.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, stretch=1)

        general_tab = QWidget()
        general_form = QFormLayout(general_tab)
        general_form.setContentsMargins(8, 8, 8, 8)
        general_form.setSpacing(10)
        self.tabs.addTab(general_tab, "一般")

        audio_tab = QWidget()
        audio_form = QFormLayout(audio_tab)
        audio_form.setContentsMargins(8, 8, 8, 8)
        audio_form.setSpacing(10)
        self.tabs.addTab(audio_tab, "オーディオ")

        advanced_tab = QWidget()
        advanced_form = QFormLayout(advanced_tab)
        advanced_form.setContentsMargins(8, 8, 8, 8)
        advanced_form.setSpacing(10)
        self.tabs.addTab(advanced_tab, "高度な設定")

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

        general_form.addRow("OBSフォルダ(固定)", self.fields["obs.dir"])
        self.recordings_dir_row = QWidget()
        recordings_dir_layout = QHBoxLayout(self.recordings_dir_row)
        recordings_dir_layout.setContentsMargins(0, 0, 0, 0)
        recordings_dir_layout.setSpacing(8)
        recordings_dir_layout.addWidget(self.fields["paths.recordings_dir"], stretch=1)
        self.recordings_dir_browse_btn = QPushButton("参照...")
        self.recordings_dir_browse_btn.clicked.connect(self.browse_recordings_dir)
        recordings_dir_layout.addWidget(self.recordings_dir_browse_btn)
        general_form.addRow("録画保存ディレクトリ", self.recordings_dir_row)

        self.json_dir_row = QWidget()
        json_dir_layout = QHBoxLayout(self.json_dir_row)
        json_dir_layout.setContentsMargins(0, 0, 0, 0)
        json_dir_layout.setSpacing(8)
        json_dir_layout.addWidget(self.fields["paths.json_dir"], stretch=1)
        self.json_dir_browse_btn = QPushButton("参照...")
        self.json_dir_browse_btn.clicked.connect(self.browse_json_dir)
        json_dir_layout.addWidget(self.json_dir_browse_btn)
        general_form.addRow("JSONディレクトリ", self.json_dir_row)

        self.icons_dir_row = QWidget()
        icons_dir_layout = QHBoxLayout(self.icons_dir_row)
        icons_dir_layout.setContentsMargins(0, 0, 0, 0)
        icons_dir_layout.setSpacing(8)
        icons_dir_layout.addWidget(self.fields["paths.champion_icons_dir"], stretch=1)
        self.icons_dir_browse_btn = QPushButton("参照...")
        self.icons_dir_browse_btn.clicked.connect(self.browse_icons_dir)
        icons_dir_layout.addWidget(self.icons_dir_browse_btn)
        general_form.addRow("アイコンディレクトリ", self.icons_dir_row)
        general_form.addRow("最大容量(GB)", self.fields["storage.max_size_gb"])
        self.storage_progress = QProgressBar()
        self.storage_progress.setRange(0, 100)
        self.storage_progress.setValue(0)
        self.storage_progress.setTextVisible(True)
        self.storage_progress.setFormat("0.0 GB / 0.0 GB (0%)")
        general_form.addRow("ストレージ使用量", self.storage_progress)
        self.obs_fps_combo = QComboBox()
        self.obs_fps_combo.addItems(["30", "60", "120"])
        general_form.addRow("録画FPS", self.obs_fps_combo)
        self.minimize_to_tray_check = QCheckBox("ウィンドウを閉じた時にタスクトレイに格納する")
        general_form.addRow("", self.minimize_to_tray_check)

        self.audio_desktop_device = QComboBox()
        self.audio_desktop_volume_row, self.audio_desktop_volume, self.audio_desktop_volume_label = self._create_db_slider()
        self.audio_desktop_mute = QCheckBox("ミュート")

        self.audio_mic_device = QComboBox()
        self.audio_mic_volume_row, self.audio_mic_volume, self.audio_mic_volume_label = self._create_db_slider()
        self.audio_mic_mute = QCheckBox("ミュート")

        audio_form.addRow(QLabel("OBSを開かずに、デスクトップ音声/マイクをこの画面で設定します。"))
        audio_form.addRow("デスクトップ音声デバイス", self.audio_desktop_device)
        audio_form.addRow("デスクトップ音量 (dB)", self.audio_desktop_volume_row)
        audio_form.addRow("", self.audio_desktop_mute)
        audio_form.addRow("マイク入力デバイス", self.audio_mic_device)
        audio_form.addRow("マイク音量 (dB)", self.audio_mic_volume_row)
        audio_form.addRow("", self.audio_mic_mute)

        advanced_form.addRow("OBSホスト", self.fields["obs.host"])
        advanced_form.addRow("OBSポート", self.fields["obs.port"])
        advanced_form.addRow("OBSパスワード", self.fields["obs.password"])
        advanced_form.addRow("シーン名", self.fields["obs.scene_name"])
        advanced_form.addRow("ソース名", self.fields["obs.source_name"])
        advanced_form.addRow("ソース色", self.fields["obs.source_color"])
        advanced_form.addRow("終了検知エラー閾値", self.fields["polling.end_error_limit"])
        advanced_form.addRow("終了監視間隔(秒)", self.fields["polling.end_poll_sec"])
        advanced_form.addRow("イベント監視間隔(秒)", self.fields["polling.event_poll_sec"])

        self.setup_btn = QPushButton("初回セットアップを開く")
        self.setup_btn.clicked.connect(self.open_setup_wizard)
        advanced_form.addRow(self.setup_btn)

        self.auto_fill_btn = QPushButton("設定を自動補完")
        self.auto_fill_btn.clicked.connect(self.auto_fill_settings)
        advanced_form.addRow(self.auto_fill_btn)

        self.preflight_btn = QPushButton("録画前チェックを実行")
        self.preflight_btn.clicked.connect(self.run_preflight_fix)
        advanced_form.addRow(self.preflight_btn)

        self.quick_fix_btn = QPushButton("環境を自動修復")
        self.quick_fix_btn.clicked.connect(self.run_quick_setup)
        advanced_form.addRow(self.quick_fix_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Reset)
        buttons.accepted.connect(self.save_settings)
        buttons.rejected.connect(self.load_settings)
        root_layout.addWidget(buttons)

        self.audio_desktop_device.currentIndexChanged.connect(self.queue_audio_auto_apply)
        self.audio_desktop_volume.valueChanged.connect(self.queue_audio_auto_apply)
        self.audio_desktop_mute.stateChanged.connect(self.queue_audio_auto_apply)
        self.audio_mic_device.currentIndexChanged.connect(self.queue_audio_auto_apply)
        self.audio_mic_volume.valueChanged.connect(self.queue_audio_auto_apply)
        self.audio_mic_mute.stateChanged.connect(self.queue_audio_auto_apply)

        self.load_settings()
        QTimer.singleShot(0, lambda: self.refresh_audio_devices(show_message=False, show_error=False, auto_launch=False))

    def _create_db_slider(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-600, 200)  # -60.0dB ～ +20.0dB (0.1dB刻み)
        slider.setSingleStep(5)     # 0.5dB
        slider.setPageStep(10)
        value_label = QLabel("0.0 dB")
        value_label.setFixedWidth(64)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(lambda v, label=value_label: label.setText(f"{v / 10.0:.1f} dB"))
        layout.addWidget(slider, stretch=1)
        layout.addWidget(value_label)
        return row, slider, value_label

    def _set_db_slider_value(self, slider, value):
        try:
            db_value = float(value)
        except Exception:
            db_value = 0.0
        slider.setValue(int(round(db_value * 10)))

    def _get_db_slider_value(self, slider):
        return float(slider.value()) / 10.0

    def on_page_shown(self):
        if not self._audio_auto_refreshed_once:
            self._audio_auto_refreshed_once = True
            self.refresh_audio_devices(show_message=False, show_error=False, auto_launch=True)
            return
        self.refresh_audio_devices(show_message=False, show_error=False, auto_launch=False)

    def browse_recordings_dir(self):
        current = self.fields["paths.recordings_dir"].text().strip()
        start_dir = current or str(ROOT_DIR)
        selected = QFileDialog.getExistingDirectory(self, "録画保存ディレクトリを選択", start_dir)
        if selected:
            self.fields["paths.recordings_dir"].setText(selected)

    def browse_json_dir(self):
        current = self.fields["paths.json_dir"].text().strip()
        start_dir = current or str(ROOT_DIR)
        selected = QFileDialog.getExistingDirectory(self, "JSONディレクトリを選択", start_dir)
        if selected:
            self.fields["paths.json_dir"].setText(selected)

    def browse_icons_dir(self):
        current = self.fields["paths.champion_icons_dir"].text().strip()
        start_dir = current or str(ROOT_DIR)
        selected = QFileDialog.getExistingDirectory(self, "アイコンディレクトリを選択", start_dir)
        if selected:
            self.fields["paths.champion_icons_dir"].setText(selected)

    def update_storage_progress(self, data=None):
        cfg = data if isinstance(data, dict) else load_config()
        storage_cfg = cfg.get("storage", {}) if isinstance(cfg, dict) else {}
        max_bytes = recordtest.parse_max_storage_bytes(storage_cfg) or 0
        used_bytes = 0

        try:
            used_bytes = CONFIG_CONTROLLER.total_storage_size(cfg)
        except Exception:
            used_bytes = 0

        used_gb = used_bytes / (1024 ** 3)
        if max_bytes > 0:
            max_gb = max_bytes / (1024 ** 3)
            ratio = int(max(0, min(100, round((used_bytes / max_bytes) * 100))))
            text = f"{used_gb:.1f} GB / {max_gb:.1f} GB ({ratio}%)"
            self.storage_progress.setValue(ratio)
        else:
            text = f"{used_gb:.1f} GB / -- GB (0%)"
            self.storage_progress.setValue(0)

        self.storage_progress.setFormat(text)
        self.storage_progress.setToolTip(text)

    def load_settings(self):
        data = load_config()
        obs = data.get("obs", {})
        paths = data.get("paths", {})
        storage = data.get("storage", {})
        polling = data.get("polling", {})
        audio = data.get("audio", {})
        app_cfg = data.get("app", {})
        desktop_audio = audio.get("desktop", {})
        mic_audio = audio.get("mic", {})

        self._audio_ui_loading = True
        self.fields["obs.host"].setText(recordtest.DEFAULT_OBS_HOST)
        self.fields["obs.dir"].setText(recordtest.DEFAULT_OBS_DIR)
        self.fields["obs.password"].setText("")
        self.fields["obs.port"].setText(str(recordtest.DEFAULT_OBS_PORT))
        fps_text = str(obs.get("fps", recordtest.DEFAULT_OBS_FPS))
        if self.obs_fps_combo.findText(fps_text) < 0:
            self.obs_fps_combo.addItem(fps_text)
        self.obs_fps_combo.setCurrentText(fps_text)
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
        self.minimize_to_tray_check.setChecked(bool(app_cfg.get("minimize_to_tray", True)))
        self.update_storage_progress(data)
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
        try:
            data["obs"]["fps"] = int(self.obs_fps_combo.currentText())
        except Exception:
            data["obs"]["fps"] = recordtest.DEFAULT_OBS_FPS
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
        data["app"]["minimize_to_tray"] = bool(self.minimize_to_tray_check.isChecked())
        self._write_audio_settings_to_config(data)

        report = run_preflight(data, auto_fix=True, force_obs_detect=False)
        if report.get("errors"):
            QMessageBox.critical(self, "保存エラー", format_report_lines(report.get("errors", [])))
            return

        report["config"]["app"]["setup_completed"] = True
        save_config(report["config"])
        self.apply_runtime_output_settings_to_obs(report["config"], show_error=False)
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

        self._set_db_slider_value(volume, slot.get("volume_db", 0.0))
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
            "volume_db": self._get_db_slider_value(volume),
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
        data.setdefault("app", {})
        data.setdefault("audio", {})

        data["obs"]["host"] = recordtest.DEFAULT_OBS_HOST
        data["obs"]["dir"] = recordtest.DEFAULT_OBS_DIR
        data["obs"]["password"] = ""
        data["obs"]["port"] = recordtest.DEFAULT_OBS_PORT
        try:
            data["obs"]["fps"] = int(self.obs_fps_combo.currentText())
        except Exception:
            data["obs"]["fps"] = recordtest.DEFAULT_OBS_FPS
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
        data["app"]["minimize_to_tray"] = bool(self.minimize_to_tray_check.isChecked())
        self._write_audio_settings_to_config(data)
        return data

    def queue_audio_auto_apply(self, *_args):
        if self._audio_ui_loading:
            return
        self._audio_apply_timer.start(350)

    def _apply_audio_settings_auto(self):
        self.apply_audio_settings_to_obs(show_success=False, show_error=False, auto_launch=False)

    def refresh_audio_devices(self, show_message=True, show_error=True, auto_launch=True):
        if self._audio_refresh_in_progress:
            return False
        self._audio_refresh_in_progress = True
        try:
            data = self._collect_settings_data_from_ui()
            result = AUDIO_CONTROLLER.refresh_audio_devices(data, auto_launch=auto_launch)
            cfg = result["config"]
            catalog = result["catalog"]
            for key in ("desktop", "mic"):
                self.audio_device_cache[key] = list(catalog.get(key, []))
            self._audio_ui_loading = True
            self._set_audio_ui_from_config("desktop", cfg.get("audio", {}).get("desktop", {}))
            self._set_audio_ui_from_config("mic", cfg.get("audio", {}).get("mic", {}))
            self._audio_ui_loading = False
            save_config(cfg)
            if show_message:
                msg = "OBSが認識している音声デバイス一覧を更新しました。"
                if result.get("obs_launched"):
                    msg += "\n（ポータブルOBSをバックグラウンドで起動しました）"
                QMessageBox.information(self, "音声デバイス一覧", msg)
            return True
        except Exception as e:
            self._audio_ui_loading = False
            if show_error:
                QMessageBox.warning(self, "音声デバイス一覧", f"取得に失敗しました。\n{e}")
            return False
        finally:
            self._audio_refresh_in_progress = False

    def apply_audio_settings_to_obs(self, show_success=True, show_error=True, auto_launch=True):
        try:
            data = self._collect_settings_data_from_ui()
            result = AUDIO_CONTROLLER.apply_audio_settings(data, auto_launch=auto_launch)
            if show_success:
                msg = "音声設定をOBSへ反映しました。"
                if result.get("obs_launched"):
                    msg += "\n（ポータブルOBSをバックグラウンドで起動しました）"
                QMessageBox.information(self, "音声設定", msg)
            return True
        except Exception as e:
            if show_error:
                QMessageBox.warning(self, "音声設定", f"OBSへの反映に失敗しました。\n{e}")
            return False

    def apply_runtime_output_settings_to_obs(self, cfg=None, show_error=False):
        try:
            data = cfg if cfg is not None else load_config()
            return AUDIO_CONTROLLER.apply_runtime_output_settings(data)
        except Exception as e:
            if show_error:
                QMessageBox.warning(self, "設定反映", f"録画設定のOBS反映に失敗しました。\n{e}")
            return False

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
            self.refresh_audio_devices(show_message=False, show_error=False, auto_launch=False)

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
        self.refresh_audio_devices(show_message=False, show_error=False, auto_launch=False)
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
        self.bg_recorder_worker = None
        self._closing = False
        self._is_quitting = False
        self._tray_icon = None
        self._tray_notice_shown = False
        self.setWindowTitle("LoL Replay Tool")
        self.resize(1200, 720)
        icon = get_app_icon()
        if icon:
            self.setWindowIcon(icon)
        self.init_tray_icon()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.home_page = HomePage(
            on_play=self.show_player,
            on_settings=self.show_settings
        )
        self.player_page = PlayerPage(on_back=self.show_home)
        self.settings_page = SettingsPage(on_back=self.show_home)

        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.player_page)
        self.stack.addWidget(self.settings_page)

        self.show_home()
        self.run_startup_setup()
        self.start_background_recorder()

    def init_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_icon = self.windowIcon()
        if tray_icon.isNull():
            tray_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

        self._tray_icon = QSystemTrayIcon(tray_icon, self)
        self._tray_icon.setToolTip("LoL Replay Tool")

        tray_menu = QMenu(self)
        show_action = QAction("アプリを表示", self)
        show_action.triggered.connect(self.restore_from_tray)
        quit_action = QAction("終了", self)
        quit_action.triggered.connect(self.exit_from_tray)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self._tray_icon.setContextMenu(tray_menu)
        self._tray_icon.activated.connect(self.on_tray_activated)
        self._tray_icon.show()

    def on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_from_tray()

    def restore_from_tray(self):
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.show_home()

    def exit_from_tray(self):
        self._is_quitting = True
        self.close()

    def should_minimize_to_tray(self):
        if self._is_quitting:
            return False
        if not self._tray_icon or not self._tray_icon.isVisible():
            return False
        try:
            data = load_config()
            return bool(data.get("app", {}).get("minimize_to_tray", True))
        except Exception:
            return True

    def show_home(self):
        self.player_page.on_leave()
        self.stack.setCurrentWidget(self.home_page)
        if not self._closing:
            self.start_background_recorder()

    def show_player(self):
        # MPV native window focus issues are avoided by showing player page first.
        self.stack.setCurrentWidget(self.player_page)
        success = self.player_page.open_selector()
        if not success:
            self.show_home()

    def show_settings(self):
        self.player_page.on_leave()
        self.stack.setCurrentWidget(self.settings_page)
        self.settings_page.on_page_shown()

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

    def _derive_recorder_home_status(self, raw_message):
        text = str(raw_message or "").strip()
        if not text:
            return "⚪ 停止", "#cfcfcf", "状態情報なし"

        lowered = text.lower()

        if text.startswith("❌"):
            return "⚠️ 録画監視エラー", "#ffb74d", text
        if "録画を開始します" in text or "試合終了を監視中" in text or "録画継続中" in text:
            return "🔴 録画中", "#ff6b6b", text
        if "試合開始を待機中" in text or "次の試合を待機" in text:
            return "🟢 LoLの起動を待機中...", "#7bd88f", text
        if "停止リクエスト" in text or "終了しました" in text:
            return "⚪ 停止", "#cfcfcf", text
        if text.startswith("⚠️") or "warning" in lowered:
            return "🟡 監視中（警告あり）", "#ffd166", text
        if text.startswith("🛠️"):
            return "🟢 起動準備中...", "#7bd88f", text
        return "🟢 監視中", "#7bd88f", text

    def _set_home_status_from_worker_message(self, raw_message):
        badge, color, detail = self._derive_recorder_home_status(raw_message)
        self.home_page.set_recorder_status(badge, color_hex=color, detail_text=detail)

    def start_background_recorder(self):
        if self.bg_recorder_worker and self.bg_recorder_worker.isRunning():
            return

        self.bg_recorder_worker = RecorderWorker()
        self.bg_recorder_worker.status.connect(self.on_bg_recorder_status)
        self.bg_recorder_worker.error.connect(self.on_bg_recorder_error)
        self.bg_recorder_worker.finished.connect(self.on_bg_recorder_finished)
        self.home_page.set_recorder_status(
            "🟢 起動準備中...",
            color_hex="#7bd88f",
            detail_text="バックグラウンド録画監視を起動しています。"
        )
        self.bg_recorder_worker.start()

    def stop_background_recorder(self, wait_ms=5000):
        worker = self.bg_recorder_worker
        if not worker:
            return
        if worker.isRunning():
            worker.stop()
            worker.wait(wait_ms)

    def on_bg_recorder_status(self, message):
        self._set_home_status_from_worker_message(message)

    def on_bg_recorder_error(self, message):
        self.home_page.set_recorder_status(
            "⚠️ 録画監視エラー",
            color_hex="#ffb74d",
            detail_text=str(message),
        )

    def on_bg_recorder_finished(self):
        if self._closing:
            self.home_page.set_recorder_status("⚪ 停止", color_hex="#cfcfcf", detail_text="アプリ終了中")
            return
        self.home_page.set_recorder_status(
            "⚪ 停止",
            color_hex="#cfcfcf",
            detail_text="バックグラウンド録画監視が停止しました。",
        )

    def closeEvent(self, event):
        if (not self._is_quitting) and self.should_minimize_to_tray():
            event.ignore()
            self.hide()
            if self._tray_icon and not self._tray_notice_shown:
                self._tray_notice_shown = True
                try:
                    self._tray_icon.showMessage(
                        "LoL Replay Tool",
                        "バックグラウンドで録画監視を継続しています。",
                        QSystemTrayIcon.MessageIcon.Information,
                        3000,
                    )
                except Exception:
                    pass
            return

        self._is_quitting = True
        self._closing = True
        try:
            self.player_page.on_leave()
        except Exception:
            pass
        self.stop_background_recorder(wait_ms=6000)
        if self._tray_icon:
            try:
                self._tray_icon.hide()
            except Exception:
                pass
        event.accept()
        super().closeEvent(event)
        app = QApplication.instance()
        if app:
            app.quit()


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
        QTabWidget::pane { border: 1px solid #3a3a3a; background-color: #1f1f1f; top: -1px; }
        QTabBar::tab {
            background-color: #262626;
            color: #bdbdbd;
            border: 1px solid #3a3a3a;
            border-bottom: 1px solid #3a3a3a;
            padding: 7px 12px;
            margin-right: 2px;
        }
        QTabBar::tab:hover { background-color: #333333; color: #f0f0f0; }
        QTabBar::tab:selected {
            background-color: #2f2f2f;
            color: #ffffff;
            font-weight: 700;
            border-bottom: 2px solid #d32f2f;
        }
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
