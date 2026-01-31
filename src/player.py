import sys
import os
import json
import time
from pathlib import Path

# --- 1. MPVのパス設定 ---
ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"

if BIN_DIR.exists():
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ["PATH"]
else:
    # 警告だけ出して続行（importエラーで詳細を出すため）
    pass

# --- 2. MPVインポート ---
try:
    import mpv
except OSError:
    print("❌ Error: mpv-1.dll が見つかりません。")
    print(f"   場所: {BIN_DIR}")
    sys.exit(1)

# --- 3. PyQt & その他ライブラリ ---
import cv2
import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, 
                             QFileDialog, QLabel, QSlider, QMessageBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

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
        roi_size = 120
        lower_red1 = np.array([0, 100, 100])
        upper_red1 = np.array([10, 255, 255])
        lower_red2 = np.array([160, 100, 100])
        upper_red2 = np.array([180, 255, 255])
        progress_step = max(1, int(fps * 5))

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
                roi = frame[0:roi_size, 0:roi_size]
                hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

                mask = cv2.inRange(hsv, lower_red1, upper_red1)
                mask += cv2.inRange(hsv, lower_red2, upper_red2)

                red_pixels = cv2.countNonZero(mask)
                threshold = (roi_size * roi_size) * 0.1
                if red_pixels > threshold:
                    found_time = frame_idx / fps
                    break

            if frame_idx % progress_step == 0:
                sec = frame_idx / fps
                self.progress.emit(f"高速捜索中... {sec:.0f}秒地点")

            frame_idx += 1

        cap.release()
        self.finished.emit(found_time)

class LoLReplayPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LoL Smart Replay Player")
        self.resize(1280, 720)
        
        self.offset = None
        self.duration = 0
        self.is_slider_pressed = False
        self.current_video_path = None
        self.is_fullscreen_mode = False # フルスクリーン状態管理
        self.video_fps = 30.0

        # メインウィジェット
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QHBoxLayout(central_widget) # selfをつけてアクセス可能に
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
        self.video_layout.addWidget(self.video_frame)

        # 2. コントロールパネル (フルスクリーン時に隠すため self にする)
        self.control_panel = QWidget()
        self.control_panel.setStyleSheet("background-color: #222; color: white;")
        control_layout = QHBoxLayout(self.control_panel)
        control_layout.setContentsMargins(10, 5, 10, 5)

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
        
        self.video_layout.addWidget(self.control_panel)

        # --- 右側: イベントリスト (フルスクリーン時に隠すため self にする) ---
        self.right_panel = QWidget()
        self.right_panel.setFixedWidth(300)
        self.right_panel.setStyleSheet("background-color: #333; color: white;")
        right_layout = QVBoxLayout(self.right_panel)

        self.info_label = QLabel("Load JSON to start")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("font-weight: bold; padding: 10px; color: #aaa;")
        
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
        right_layout.addWidget(self.event_list)

        # レイアウト統合
        self.main_layout.addWidget(video_container, stretch=1)
        self.main_layout.addWidget(self.right_panel)

        # --- MPV初期化 ---
        self.init_mpv()

        # ファイルオープン
        self.open_file_dialog()

    def init_mpv(self):
        try:
            self.player = mpv.MPV(wid=str(int(self.video_frame.winId())), 
                                  input_default_bindings=True, 
                                  input_vo_keyboard=True)
            self.player.observe_property('time-pos', self.on_time_update)
            self.player.observe_property('duration', self.on_duration_update)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"MPV Init Failed: {e}")
            sys.exit(1)

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
            self.showFullScreen()      # ウィンドウ枠を消して最大化
            self.is_fullscreen_mode = True
        else:
            # 通常モードへ復帰
            self.showNormal()
            self.right_panel.show()
            self.control_panel.show()
            self.is_fullscreen_mode = False

    def open_file_dialog(self):
        initial_dir = str(ROOT_DIR)
        fname, _ = QFileDialog.getOpenFileName(self, "Open JSON Log", initial_dir, "JSON Files (*.json)")
        if fname:
            self.load_data(fname)

    def load_data(self, json_path):
        json_path = Path(json_path)
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            video_path_str = data.get("obs_record_path")
            if not video_path_str:
                self.info_label.setText("Error: Path not found in JSON")
                return
            
            video_path = Path(video_path_str)
            if not video_path.exists():
                video_path = json_path.parent / video_path.name
                if not video_path.exists():
                    self.info_label.setText("Error: Video file missing")
                    return

            self.current_video_path = video_path
            self.sync_game_time = data.get("sync_game_time", 0.0)
            self.events = data.get("events", [])
            if not self.events:
                self.events = data.get("events_all", [])
            self.my_name = data.get("summoner_name", "Unknown")
            self.offset = None
            self.event_list.setEnabled(False)

            self.info_label.setText(f"Player: {self.my_name}\nSyncing...")
            self.populate_event_list()
            
            self.player.play(str(self.current_video_path))
            self.player.pause = True
            
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
        self.event_list.clear()
        self.add_event_item("🎬 Game Start", 0.0, "#4CAF50")
        for evt in self.events:
            name = evt.get("EventName", "Event")
            time_sec = evt.get("EventTime", 0)
            killer = evt.get("KillerName", "")
            display = f"⚔️ {name} ({killer})" if killer else f"🛡️ {name}"
            color = "#FFFFFF"
            if "Kill" in name and "Champion" in name: color = "#FF5252"
            elif "Dragon" in name or "Baron" in name: color = "#E040FB"
            self.add_event_item(display, time_sec, color)

    def add_event_item(self, text, game_time, color_hex):
        m, s = divmod(int(game_time), 60)
        item_text = f"[{m:02d}:{s:02d}] {text}"
        item = QListWidgetItem(item_text)
        item.setData(Qt.ItemDataRole.UserRole, game_time)
        item.setForeground(Qt.GlobalColor.white)
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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = LoLReplayPlayer()
    window.show()
    sys.exit(app.exec())
