import sys
import os
import json
import time
import re
import shutil
from pathlib import Path

# --- 1. MPVのパス設定 ---
try:
    from .app_paths import get_app_root
except ImportError:
    from app_paths import get_app_root

ROOT_DIR = get_app_root()
CONFIG_PATH = ROOT_DIR / "config" / "setting.json"
ALIASES_PATH = ROOT_DIR / "config" / "champion_aliases.json"
BIN_DIR = ROOT_DIR / "bin"
ICON_DIR = ROOT_DIR / "assets" / "champions" / "icons"
ICON_INDEX = None
ICON_ALIASES = None
DEFAULT_RECORDINGS_DIR = ROOT_DIR / "recordings"
DEFAULT_JSON_DIR = ROOT_DIR / "recordings" / "json"


def load_app_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def resolve_config_path(value, fallback):
    path = fallback
    if value not in (None, ""):
        path = Path(str(value))
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def get_config_media_paths(config_data=None):
    data = config_data if isinstance(config_data, dict) else load_app_config()
    paths = data.get("paths", {}) if isinstance(data, dict) else {}
    recordings_dir = resolve_config_path(paths.get("recordings_dir"), DEFAULT_RECORDINGS_DIR)
    json_dir = resolve_config_path(paths.get("json_dir"), DEFAULT_JSON_DIR)
    return data, recordings_dir, json_dir


def resolve_video_path(json_path: Path, payload: dict, recordings_dir: Path):
    value = payload.get("obs_record_path")
    if not value:
        return None

    raw = Path(str(value))
    candidates = []

    if raw.is_absolute():
        candidates.append(raw)
        candidates.append(json_path.parent / raw.name)
        if recordings_dir:
            candidates.append(recordings_dir / raw.name)
    else:
        candidates.append(json_path.parent / raw)
        if recordings_dir:
            candidates.append(recordings_dir / raw)
            candidates.append(recordings_dir / raw.name)

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists():
            return resolved
    return None

def ensure_mpv_dll(bin_dir: Path, root_dir: Path):
    candidates = []
    for base in (bin_dir, root_dir):
        if not base or not base.exists():
            continue
        for name in ("mpv-1.dll", "libmpv-1.dll", "mpv-2.dll", "libmpv-2.dll"):
            path = base / name
            if path.exists():
                candidates.append(path)

    if not candidates:
        return

    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    mpv1 = bin_dir / "mpv-1.dll"
    libmpv1 = bin_dir / "libmpv-1.dll"
    if mpv1.exists() or libmpv1.exists():
        return

    for src in candidates:
        if src.name in ("mpv-1.dll", "libmpv-1.dll"):
            try:
                shutil.copy2(src, mpv1)
            except Exception:
                pass
            return

    for src in candidates:
        if src.name in ("mpv-2.dll", "libmpv-2.dll"):
            try:
                shutil.copy2(src, mpv1)
            except Exception:
                pass
            return

ensure_mpv_dll(BIN_DIR, ROOT_DIR)
if BIN_DIR.exists():
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ["PATH"]

# --- 2. MPVインポート ---
try:
    import mpv as mpv_module
    MPV_IMPORT_ERROR = None
except Exception as e:
    mpv_module = None
    MPV_IMPORT_ERROR = e

# --- 3. PyQt & その他ライブラリ ---
import cv2
import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QListWidget, QListWidgetItem, QPushButton,
                             QFileDialog, QLabel, QSlider, QMessageBox, QDialog,
                             QDialogButtonBox, QLineEdit, QComboBox, QCheckBox,
                             QSizePolicy)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QPixmap, QFont, QColor


def show_mpv_missing_dialog_and_exit(parent=None):
    message = (
        "binフォルダに mpv-1.dll などのMPVコンポーネントが見つかりません。\n"
        "配置してから起動してください。\n\n"
        f"探した場所: {BIN_DIR}\n\n"
        "対応DLL: mpv-1.dll / libmpv-1.dll / mpv-2.dll / libmpv-2.dll"
    )
    app = QApplication.instance()
    created_app = False
    if app is None:
        app = QApplication(sys.argv)
        created_app = True
    QMessageBox.critical(parent, "MPV DLL Missing", message)
    if created_app:
        app.quit()
    raise SystemExit(1)


def ensure_mpv_available_or_exit(parent=None):
    if mpv_module is not None:
        return mpv_module
    show_mpv_missing_dialog_and_exit(parent)


class SyncWorker(QThread):
    """バックグラウンドで同期マーカーを探すスレッド"""
    finished = pyqtSignal(float)
    progress = pyqtSignal(str)

    def __init__(self, video_path, max_seconds=180):
        super().__init__()
        self.video_path = str(video_path)
        self.max_seconds = max_seconds

    def run(self):
        self.progress.emit("同期マーカーを高速捜索中...")
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            self.finished.emit(-1.0)
            return

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 60.0
        max_frames = int(fps * self.max_seconds)
        found_time = -1.0

        skip_step = 30
        roi_size = 140
        lower_red1 = np.array([0, 100, 100])
        upper_red1 = np.array([10, 255, 255])
        lower_red2 = np.array([160, 100, 100])
        upper_red2 = np.array([180, 255, 255])
        progress_step = max(1, int(fps * 5))
        threshold_ratio = 0.05

        frame_idx = 0
        while frame_idx < max_frames:
            if not cap.grab():
                break

            if frame_idx % skip_step != 0:
                frame_idx += 1
                continue

            ret, frame = cap.retrieve()
            if not ret:
                break

            if frame.shape[0] >= roi_size and frame.shape[1] >= roi_size:
                h, w = frame.shape[:2]
                rois = [
                    frame[0:roi_size, 0:roi_size],
                    frame[0:roi_size, w - roi_size:w],
                    frame[h - roi_size:h, 0:roi_size],
                    frame[h - roi_size:h, w - roi_size:w],
                ]
                for roi in rois:
                    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
                    mask = cv2.inRange(hsv, lower_red1, upper_red1)
                    mask += cv2.inRange(hsv, lower_red2, upper_red2)
                    red_pixels = cv2.countNonZero(mask)
                    threshold = (roi_size * roi_size) * threshold_ratio
                    if red_pixels > threshold:
                        found_time = frame_idx / fps
                        break
                if found_time >= 0:
                    break

            if frame_idx % progress_step == 0:
                sec = frame_idx / fps
                self.progress.emit(f"高速捜索中... {sec:.0f}秒地点")

            frame_idx += 1

        cap.release()
        self.finished.emit(found_time)


def normalize_result(result_value, team_value=None, winning_team=None):
    if isinstance(result_value, str):
        val = result_value.strip().lower()
        if "win" in val:
            return "Win"
        if "lose" in val or "loss" in val or "defeat" in val:
            return "Loss"
    if isinstance(result_value, bool):
        return "Win" if result_value else "Loss"

    team = None
    if isinstance(team_value, str):
        team = team_value.strip().upper()
    elif isinstance(team_value, int):
        team = "ORDER" if team_value == 100 else "CHAOS" if team_value == 200 else None

    winner = None
    if isinstance(winning_team, str):
        winner = winning_team.strip().upper()
    elif isinstance(winning_team, int):
        winner = "ORDER" if winning_team == 100 else "CHAOS" if winning_team == 200 else None

    if team and winner:
        return "Win" if team == winner else "Loss"
    return "Unknown"


def normalize_summoner_name(value):
    if not value:
        return None
    name = str(value).strip()
    if "#" in name:
        name = name.split("#", 1)[0]
    return name.strip()


def normalize_icon_key(value):
    return re.sub(r"[^\w]+", "", str(value or ""), flags=re.UNICODE).lower()


def build_icon_index():
    global ICON_INDEX
    ICON_INDEX = {}
    if not ICON_DIR or not ICON_DIR.exists():
        return
    for path in ICON_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        key = normalize_icon_key(path.stem)
        if key and key not in ICON_INDEX:
            ICON_INDEX[key] = path


def load_icon_aliases():
    global ICON_ALIASES
    ICON_ALIASES = {}
    if not ALIASES_PATH.exists():
        return
    try:
        with open(ALIASES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            ICON_ALIASES = data
    except Exception:
        ICON_ALIASES = {}


def find_champion_icon(champion_name):
    if not champion_name:
        return None
    global ICON_INDEX, ICON_ALIASES
    if ICON_INDEX is None:
        build_icon_index()
    if not ICON_INDEX:
        return None

    key = normalize_icon_key(champion_name)
    path = ICON_INDEX.get(key)
    if path:
        return path

    if ICON_ALIASES is None:
        load_icon_aliases()

    alias_map = {
        "nunuwillump": "nunu",
        "renataglasc": "renata",
        "wukong": "monkeyking",
        "belveth": "belveth",
        "chogath": "chogath",
        "kaisa": "kaisa",
        "khazix": "khazix",
        "velkoz": "velkoz",
        "leblanc": "leblanc",
        "reksai": "reksai",
        "tahmkench": "tahmkench",
        "twistedfate": "twistedfate",
        "xinzhao": "xinzhao",
        "missfortune": "missfortune",
        "masteryi": "masteryi",
        "drmundo": "drmundo",
        "aurelionsol": "aurelionsol",
        "ksante": "ksante",
        "jarvaniv": "jarvaniv",
        "leesin": "leesin"
    }

    alias_value = None
    if ICON_ALIASES:
        alias_value = ICON_ALIASES.get(champion_name) or ICON_ALIASES.get(key)
    for candidate in (alias_value, alias_map.get(key)):
        if not candidate:
            continue
        path = ICON_INDEX.get(normalize_icon_key(candidate))
        if path:
            return path
    return None


class ReplaySelectDialog(QDialog):
    def __init__(self, parent=None, json_dir=None, recordings_dir=None):
        super().__init__(parent)
        self.setWindowTitle("Replay Select")
        self.resize(820, 560)
        self.selected_path = None
        _cfg_data, cfg_recordings_dir, cfg_json_dir = get_config_media_paths()
        self.json_dir = resolve_config_path(json_dir, cfg_json_dir) if json_dir else cfg_json_dir
        self.recordings_dir = (
            resolve_config_path(recordings_dir, cfg_recordings_dir) if recordings_dir else cfg_recordings_dir
        )
        self.meta_cache = []

        layout = QVBoxLayout(self)

        filter_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("検索: チャンピオン / サモナー")
        self.search_input.textChanged.connect(self.apply_filters)
        filter_row.addWidget(self.search_input, stretch=1)

        self.result_filter = QComboBox()
        self.result_filter.addItems(["All", "Win", "Loss"])
        self.result_filter.currentIndexChanged.connect(self.apply_filters)
        filter_row.addWidget(self.result_filter)

        self.sort_filter = QComboBox()
        self.sort_filter.addItems(["新しい順", "古い順"])
        self.sort_filter.currentIndexChanged.connect(self.apply_filters)
        filter_row.addWidget(self.sort_filter)

        self.missing_filter = QCheckBox("Missingを非表示")
        self.missing_filter.setChecked(True)
        self.missing_filter.stateChanged.connect(self.apply_filters)
        filter_row.addWidget(self.missing_filter)

        layout.addLayout(filter_row)

        self.list_widget = QListWidget()
        self.list_widget.setSpacing(6)
        self.list_widget.itemDoubleClicked.connect(self.accept_selected)
        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_list)
        btn_row.addWidget(self.refresh_btn)

        self.open_btn = QPushButton("Open JSON...")
        self.open_btn.clicked.connect(self.open_file_dialog)
        btn_row.addWidget(self.open_btn)

        btn_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept_selected)
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)

        layout.addLayout(btn_row)
        self.refresh_list()

    def refresh_list(self):
        self.meta_cache = []
        if self.json_dir.exists():
            files = sorted(self.json_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files:
                self.meta_cache.append(self.load_meta(path))
        self.apply_filters()

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def load_meta(self, path):
        meta = {
            "path": path,
            "champion_name": "Unknown",
            "result": "Unknown",
            "summoner": "Unknown",
            "saved_at": path.stem,
            "video_exists": True,
            "video_path": None,
        }
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            meta["champion_name"] = data.get("champion_name") or data.get("player_champion") or "Unknown"
            meta["summoner"] = data.get("summoner_name") or "Unknown"
            meta["saved_at"] = data.get("saved_at") or path.stem
            result = data.get("game_result")
            team = data.get("player_team")
            winning = data.get("winning_team")
            meta["result"] = normalize_result(result, team_value=team, winning_team=winning)
            resolved_video = resolve_video_path(path, data, self.recordings_dir)
            meta["video_path"] = str(resolved_video) if resolved_video else None
            meta["video_exists"] = resolved_video is not None
        except Exception:
            pass
        return meta

    def build_item_widget(self, meta):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 6, 8, 6)

        icon_label = QLabel()
        icon_label.setFixedSize(48, 48)
        icon_label.setStyleSheet("background-color: #222; border-radius: 6px;")
        icon_path = find_champion_icon(meta["champion_name"])
        if icon_path:
            pixmap = QPixmap(str(icon_path)).scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        else:
            icon_label.setText("?")
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        title = QLabel(meta["champion_name"])
        title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        text_col.addWidget(title)

        result_text = meta["result"]
        result_label = QLabel(result_text)
        if result_text == "Win":
            result_label.setStyleSheet("color: #4CAF50;")
        elif result_text == "Loss":
            result_label.setStyleSheet("color: #F44336;")
        else:
            result_label.setStyleSheet("color: #9E9E9E;")

        sub_row = QHBoxLayout()
        sub_row.setContentsMargins(0, 0, 0, 0)
        sub_row.addWidget(result_label)

        detail_label = QLabel(f"· {meta['summoner']} · {meta['saved_at']}")
        detail_label.setStyleSheet("color: #aaa;")
        sub_row.addWidget(detail_label)
        sub_row.addStretch(1)
        text_col.addLayout(sub_row)
        layout.addLayout(text_col, stretch=1)

        status_label = QLabel("OK" if meta["video_exists"] else "Missing")
        status_label.setStyleSheet("color: #888;")
        layout.addWidget(status_label)
        return widget

    def apply_filters(self):
        self.list_widget.clear()
        if not self.meta_cache:
            return

        query = self.search_input.text().strip().lower()
        result_filter = self.result_filter.currentText()
        sort_mode = self.sort_filter.currentText()
        hide_missing = self.missing_filter.isChecked()

        def matches(meta):
            if hide_missing and not meta["video_exists"]:
                return False
            if result_filter != "All" and meta["result"] != result_filter:
                return False
            if query:
                hay = f"{meta['champion_name']} {meta['summoner']}".lower()
                return query in hay
            return True

        filtered = [m for m in self.meta_cache if matches(m)]
        reverse = sort_mode == "新しい順"
        filtered.sort(key=lambda m: m.get("saved_at") or "", reverse=reverse)

        for meta in filtered:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(meta["path"]))
            item.setData(Qt.ItemDataRole.UserRole + 1, bool(meta.get("video_exists", False)))
            widget = self.build_item_widget(meta)
            item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)

    def accept_selected(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        video_exists = item.data(Qt.ItemDataRole.UserRole + 1)
        if video_exists is False:
            QMessageBox.warning(self, "Missing Video", "このリプレイの動画ファイルが見つかりません。")
            return
        self.selected_path = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def open_file_dialog(self):
        fname, _ = QFileDialog.getOpenFileName(
            self,
            "Open JSON Log",
            str(self.json_dir if self.json_dir else ROOT_DIR),
            "JSON Files (*.json)"
        )
        if fname:
            self.selected_path = fname
            self.accept()


class PlayerWidget(QWidget):
    def __init__(self, auto_open=True, fullscreen_cb=None):
        super().__init__()
        self.fullscreen_cb = fullscreen_cb
        
        self.offset = None
        self.duration = 0
        self.is_slider_pressed = False
        self.current_video_path = None
        self.is_fullscreen_mode = False # フルスクリーン状態管理
        self.video_fps = 30.0
        self.events = []
        self.events_all = []
        self.my_name = None
        self.my_name_short = None

        self.load_settings()

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # メインレイアウト
        self.main_layout = QHBoxLayout(self) # selfをつけてアクセス可能に
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # --- 左側: 動画コンテナ ---
        video_container = QWidget()
        self.video_layout = QVBoxLayout(video_container) # selfをつけてアクセス可能に
        self.video_layout.setContentsMargins(0, 0, 0, 0)
        self.video_layout.setSpacing(0)

        # 1. MPV描画エリア
        self.video_frame = QWidget()
        self.video_frame.setStyleSheet("background-color: black;")
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.video_frame.setAutoFillBackground(False)
        self.video_layout.addWidget(self.video_frame, stretch=1)

        # 2. コントロールパネル (フルスクリーン時に隠すため self にする)
        self.control_panel = QWidget()
        self.control_panel.setStyleSheet("background-color: #222; color: white;")
        control_layout = QHBoxLayout(self.control_panel)
        control_layout.setContentsMargins(10, 4, 10, 4)
        self.control_panel.setFixedHeight(44)

        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self.toggle_playback)
        self.play_btn.setFixedWidth(60)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderReleased.connect(self.on_slider_released)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setFixedWidth(100)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        control_layout.addWidget(self.play_btn)
        control_layout.addWidget(self.slider)
        control_layout.addWidget(self.time_label)
        
        self.video_layout.addWidget(self.control_panel, stretch=0)

        # --- 右側: イベントリスト (フルスクリーン時に隠すため self にする) ---
        self.right_panel = QWidget()
        self.right_panel.setFixedWidth(300)
        self.right_panel.setStyleSheet("background-color: #333; color: white;")
        right_layout = QVBoxLayout(self.right_panel)

        self.info_label = QLabel("Load JSON to start")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("font-weight: bold; padding: 10px; color: #aaa;")

        self.offset_label = QLabel("Offset: --")
        self.offset_label.setStyleSheet("padding: 0 10px 6px 10px; color: #ccc;")

        self.event_list = QListWidget()
        self.event_list.setStyleSheet("""
            QListWidget { border: none; background-color: #2b2b2b; }
            QListWidget::item { padding: 10px; border-bottom: 1px solid #444; }
            QListWidget::item:selected { background-color: #d32f2f; color: white; }
            QListWidget::item:hover { background-color: #444; }
        """)
        self.event_list.setFocusPolicy(Qt.FocusPolicy.NoFocus) # キー入力をウィンドウに譲る
        self.event_list.itemClicked.connect(self.on_event_clicked)
        self.event_list.setEnabled(False)

        right_layout.addWidget(self.info_label)
        right_layout.addWidget(self.offset_label)

        filter_row = QHBoxLayout()
        self.filter_kill = QCheckBox("Kill")
        self.filter_objective = QCheckBox("Objective")
        self.filter_other = QCheckBox("Other")
        self.filter_kill.setChecked(True)
        self.filter_objective.setChecked(True)
        self.filter_other.setChecked(True)
        self.filter_kill.stateChanged.connect(self.populate_event_list)
        self.filter_objective.stateChanged.connect(self.populate_event_list)
        self.filter_other.stateChanged.connect(self.populate_event_list)
        filter_row.addWidget(self.filter_kill)
        filter_row.addWidget(self.filter_objective)
        filter_row.addWidget(self.filter_other)
        right_layout.addLayout(filter_row)

        offset_row = QHBoxLayout()
        offset_row.setContentsMargins(8, 0, 8, 2)
        for label, value in [("-5s", -5.0), ("-1s", -1.0), ("-0.1s", -0.1), ("+0.1s", 0.1), ("+1s", 1.0), ("+5s", 5.0)]:
            btn = QPushButton(label)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda _, v=value: self.adjust_offset(v))
            offset_row.addWidget(btn)
        right_layout.addLayout(offset_row)
        
        sync_btn = QPushButton("現在位置で同期")
        sync_btn.setFixedHeight(28)
        sync_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sync_btn.clicked.connect(self.sync_to_current_position)
        right_layout.addWidget(sync_btn)
        right_layout.addWidget(self.event_list)

        # レイアウト統合
        self.main_layout.addWidget(video_container, stretch=1)
        self.main_layout.addWidget(self.right_panel)

        # --- MPV初期化 ---
        self.init_mpv()

        # キーショートカット（フォーカスに依存しない）
        self.register_shortcuts()

        # リプレイ選択ダイアログ
        if auto_open:
            self.open_replay_selector()

    def init_mpv(self):
        try:
            mpv_runtime = ensure_mpv_available_or_exit(self)
            self.player = mpv_runtime.MPV(
                wid=str(int(self.video_frame.winId())),
                input_default_bindings=False,
                input_vo_keyboard=False,
                keepaspect=True,
                vo="gpu",
                gpu_context="d3d11"
            )
            self.player.observe_property('time-pos', self.on_time_update)
            self.player.observe_property('duration', self.on_duration_update)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"MPV Init Failed: {e}")
            sys.exit(1)

    def load_settings(self):
        global ICON_DIR, ICON_INDEX, ICON_ALIASES, ALIASES_PATH
        self.recordings_dir = DEFAULT_RECORDINGS_DIR
        self.json_dir = DEFAULT_JSON_DIR
        if not CONFIG_PATH.exists():
            return
        try:
            data = load_app_config()
            paths = data.get("paths", {}) if isinstance(data, dict) else {}

            self.recordings_dir = resolve_config_path(paths.get("recordings_dir"), DEFAULT_RECORDINGS_DIR)
            self.json_dir = resolve_config_path(paths.get("json_dir"), DEFAULT_JSON_DIR)

            icons = paths.get("champion_icons_dir")
            if icons:
                ICON_DIR = resolve_config_path(icons, ICON_DIR)
                ICON_INDEX = None
            aliases_path = paths.get("champion_aliases_path")
            if aliases_path:
                ALIASES_PATH = resolve_config_path(aliases_path, ALIASES_PATH)
                ICON_ALIASES = None
        except Exception:
            pass

    def register_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle_playback)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self.step_frame(1))
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self.step_frame(-1))
        QShortcut(QKeySequence(Qt.Key.Key_F), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.on_escape)
        QShortcut(QKeySequence(Qt.Key.Key_N), self, activated=self.next_event)
        QShortcut(QKeySequence(Qt.Key.Key_P), self, activated=self.prev_event)

    # --- キーボードイベント処理 (ここが重要) ---
    def keyPressEvent(self, event):
        key = event.key()

        # [Space] 再生/一時停止
        if key == Qt.Key.Key_Space:
            self.toggle_playback()
        
        # [→] コマ送り (1フレーム進む)
        elif key == Qt.Key.Key_Right:
            self.step_frame(1)

        # [←] コマ戻し (1フレーム戻る)
        elif key == Qt.Key.Key_Left:
            self.step_frame(-1)

        # [F] フルスクリーン切り替え
        elif key == Qt.Key.Key_F:
            self.toggle_fullscreen()

        # [Esc] フルスクリーン解除
        elif key == Qt.Key.Key_Escape:
            if self.is_fullscreen_mode:
                self.toggle_fullscreen()

        # [N] 次のイベント (簡易実装)
        elif key == Qt.Key.Key_N:
            row = self.event_list.currentRow()
            if row < self.event_list.count() - 1:
                self.event_list.setCurrentRow(row + 1)
                self.on_event_clicked(self.event_list.currentItem())

        # [P] 前のイベント
        elif key == Qt.Key.Key_P:
            row = self.event_list.currentRow()
            if row > 0:
                self.event_list.setCurrentRow(row - 1)
                self.on_event_clicked(self.event_list.currentItem())

        else:
            super().keyPressEvent(event)

    def toggle_fullscreen(self):
        if not self.is_fullscreen_mode:
            # フルスクリーン化
            self.right_panel.hide()    # サイドバーを消す
            self.control_panel.hide()  # 下のバーを消す
            self.set_fullscreen_mode(True)
            if self.fullscreen_cb:
                self.fullscreen_cb(True)
            else:
                window = self.window()
                if window:
                    window.showFullScreen()      # ウィンドウ枠を消して最大化
            self.is_fullscreen_mode = True
        else:
            # 通常モードへ復帰
            self.set_fullscreen_mode(False)
            if self.fullscreen_cb:
                self.fullscreen_cb(False)
            else:
                window = self.window()
                if window:
                    window.showNormal()
            self.right_panel.show()
            self.control_panel.show()
            self.is_fullscreen_mode = False

    def on_escape(self):
        if self.is_fullscreen_mode:
            self.toggle_fullscreen()

    def next_event(self):
        row = self.event_list.currentRow()
        if row < self.event_list.count() - 1:
            self.event_list.setCurrentRow(row + 1)
            self.on_event_clicked(self.event_list.currentItem())

    def prev_event(self):
        row = self.event_list.currentRow()
        if row > 0:
            self.event_list.setCurrentRow(row - 1)
            self.on_event_clicked(self.event_list.currentItem())

    def open_file_dialog(self):
        self.load_settings()
        initial_dir = str(self.json_dir if self.json_dir else ROOT_DIR)
        fname, _ = QFileDialog.getOpenFileName(self, "Open JSON Log", initial_dir, "JSON Files (*.json)")
        if fname:
            self.load_data(fname)

    def open_replay_selector(self):
        self.load_settings()
        dialog = ReplaySelectDialog(
            self,
            json_dir=self.json_dir,
            recordings_dir=self.recordings_dir,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_path:
            self.load_data(dialog.selected_path)

    def load_data(self, json_path):
        json_path = Path(json_path)
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            video_path = resolve_video_path(json_path, data, self.recordings_dir)
            if video_path is None:
                self.info_label.setText("Error: Video file missing")
                return

            self.current_video_path = video_path
            self.sync_game_time = data.get("sync_game_time", 0.0)
            self.events = data.get("events", []) or []
            self.events_all = data.get("events_all", []) or []
            self.my_name = data.get("summoner_name", "Unknown")
            self.my_name_short = normalize_summoner_name(self.my_name)
            self.offset = None
            self.event_list.setEnabled(False)

            self.info_label.setText(f"Player: {self.my_name}\nSyncing...")
            self.populate_event_list()
            
            self.player.play(str(self.current_video_path))
            self.player.pause = True
            self.video_frame.setFocus()
            
            self.update_video_fps()
            self.start_sync_worker()

        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def start_sync_worker(self):
        self.worker = SyncWorker(self.current_video_path, max_seconds=180)
        self.worker.progress.connect(lambda s: self.info_label.setText(s))
        self.worker.finished.connect(self.on_sync_finished)
        self.worker.start()

    def on_sync_finished(self, found_time):
        if found_time < 0:
            self.info_label.setText("⚠️ No Marker Found\nOffset: 0s")
            self.offset = 0
        else:
            self.offset = found_time - self.sync_game_time
            self.info_label.setText(f"✅ Synced\nOffset: {self.offset:.2f}s")
        self.update_offset_label()
        self.event_list.setEnabled(True)
        self.player.pause = False
        self.play_btn.setText("Pause")

    def update_video_fps(self):
        try:
            cap = cv2.VideoCapture(str(self.current_video_path))
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                if fps and fps > 0:
                    self.video_fps = fps
            cap.release()
        except Exception:
            pass

    def populate_event_list(self):
        def build_events():
            if self.events:
                return list(self.events)
            if self.events_all:
                return list(self.events_all)
            return []

        events = build_events()
        self.event_list.clear()
        self.add_event_item("🎬 Game Start", 0.0, "#4CAF50")
        for evt in events:
            name = evt.get("EventName", "Event")
            time_sec = evt.get("EventTime", 0)
            killer = evt.get("KillerName", "")
            victim = evt.get("VictimName", "")

            if name == "ChampionKill":
                if self.my_name_short:
                    if killer not in (self.my_name, self.my_name_short) and victim not in (self.my_name, self.my_name_short):
                        continue
                if not self.filter_kill.isChecked():
                    continue
                display = f"⚔️ {killer} → {victim}" if killer or victim else "⚔️ ChampionKill"
                if self.my_name and (victim == self.my_name or victim == self.my_name_short):
                    color = "#FFB74D"
                else:
                    color = "#FF5252"
            elif name == "DragonKill":
                if not self.filter_objective.isChecked():
                    continue
                display = "🐉 Dragon"
                color = "#29B6F6"
            elif name == "BaronKill":
                if not self.filter_objective.isChecked():
                    continue
                display = "🟣 Baron"
                color = "#8E24AA"
            elif name == "HeraldKill":
                if not self.filter_objective.isChecked():
                    continue
                display = "👁 Herald"
                color = "#7CB342"
            elif name == "HordeKill":
                if not self.filter_objective.isChecked():
                    continue
                display = "🟠 Voidgrub"
                color = "#FF8A65"
            else:
                if not self.filter_other.isChecked():
                    continue
                display = f"🛡️ {name}"
                color = "#FFFFFF"

            self.add_event_item(display, time_sec, color)

    def update_offset_label(self):
        if self.offset is None:
            self.offset_label.setText("Offset: --")
        else:
            self.offset_label.setText(f"Offset: {self.offset:+.2f}s")

    def adjust_offset(self, delta):
        if self.offset is None:
            self.offset = 0.0
        self.offset += delta
        self.update_offset_label()

    def sync_to_current_position(self):
        if not hasattr(self, "player"):
            return
        try:
            current = float(self.player.time_pos or 0.0)
        except Exception:
            current = None
        if current is None:
            return
        selected = self.event_list.currentItem()
        if not selected:
            return
        game_time = selected.data(Qt.ItemDataRole.UserRole)
        if game_time is None:
            return
        self.offset = current - float(game_time)
        self.update_offset_label()

    def add_event_item(self, text, game_time, color_hex):
        m, s = divmod(int(game_time), 60)
        item_text = f"[{m:02d}:{s:02d}] {text}"
        item = QListWidgetItem(item_text)
        item.setData(Qt.ItemDataRole.UserRole, game_time)
        color = QColor(str(color_hex))
        if not color.isValid():
            color = QColor("#FFFFFF")
        item.setForeground(color)
        self.event_list.addItem(item)

    def on_event_clicked(self, item):
        if self.offset is None:
            return
        game_time = item.data(Qt.ItemDataRole.UserRole)
        target = game_time + self.offset
        seek_pos = max(0, target - 5.0)
        self.player.seek(seek_pos, reference='absolute', precision='exact')
        self.player.pause = False
        self.play_btn.setText("Pause")
        
        # フォーカスを外してキー入力を有効にする
        self.event_list.clearFocus() 

    def toggle_playback(self):
        self.player.pause = not self.player.pause
        self.play_btn.setText("Play" if self.player.pause else "Pause")

    def stop_playback(self):
        if not hasattr(self, "player"):
            return
        try:
            self.player.command("stop")
        except Exception:
            pass
        try:
            self.player.pause = True
        except Exception:
            pass

    def set_fullscreen_mode(self, enabled):
        if not hasattr(self, "player"):
            return
        try:
            self.player["panscan"] = 1.0 if enabled else 0.0
        except Exception:
            try:
                self.player.panscan = 1.0 if enabled else 0.0
            except Exception:
                pass

    def step_frame(self, direction):
        if not self.player:
            return
        if self.video_fps <= 0:
            self.video_fps = 30.0
        try:
            current = float(self.player.time_pos or 0.0)
        except Exception:
            current = 0.0
        step = 1.0 / float(self.video_fps)
        target = max(0.0, current + (step * direction))
        self.player.pause = True
        self.player.seek(target, reference='absolute', precision='exact')
        self.play_btn.setText("Play")

    def on_time_update(self, name, time_pos):
        if time_pos is None: return
        if not self.is_slider_pressed and self.duration > 0:
            val = int((time_pos / self.duration) * 1000)
            self.slider.setValue(val)
        
        cm, cs = divmod(int(time_pos), 60)
        dm, ds = divmod(int(self.duration), 60)
        self.time_label.setText(f"{cm:02d}:{cs:02d} / {dm:02d}:{ds:02d}")

    def on_duration_update(self, name, duration):
        if duration: self.duration = duration

    def on_slider_pressed(self):
        self.is_slider_pressed = True

    def on_slider_released(self):
        self.is_slider_pressed = False
        val = self.slider.value()
        if self.duration > 0:
            target = (val / 1000) * self.duration
            # 【重要修正】ここで絶対時間指定をする
            self.player.seek(target, reference='absolute', precision='exact')

    def closeEvent(self, event):
        if hasattr(self, 'player'):
            self.player.terminate()
        event.accept()


class PlayerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LoL Smart Replay Player")
        self.resize(1280, 720)
        self.player_widget = PlayerWidget(auto_open=True)
        self.setCentralWidget(self.player_widget)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PlayerWindow()
    window.show()
    sys.exit(app.exec())
