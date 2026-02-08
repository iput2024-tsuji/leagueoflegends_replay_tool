import os
import sys
import time
import json
import subprocess
from pathlib import Path

import ctypes
from ctypes import wintypes

import requests
import urllib3
import obsws_python as obs
from obsws_python.error import OBSSDKRequestError
try:
    from .app_paths import get_app_root
except ImportError:
    from app_paths import get_app_root

ROOT_DIR = get_app_root()
CONFIG_PATH = ROOT_DIR / "config" / "setting.json"
SAMPLE_CONFIG_PATH = ROOT_DIR / "config" / "setting.sample.json"

LIVECLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
ACTIVE_PLAYER_URL = f"{LIVECLIENT_BASE}/activeplayername"
EVENT_URL = f"{LIVECLIENT_BASE}/eventdata"
ALL_GAME_URL = f"{LIVECLIENT_BASE}/allgamedata"

DEFAULT_OBS_PASSWORD = "password"
DEFAULT_OBS_SCENE_NAME = "lol_seen"
DEFAULT_OBS_SOURCE_NAME = "color"
DEFAULT_OBS_DIR = "C:/Program Files/obs-studio"
DEFAULT_BIN_DIR = "bin"
DEFAULT_RECORDINGS_DIR = "recordings"
DEFAULT_JSON_DIR = "recordings/json"
DEFAULT_CHAMPION_ICONS_DIR = "assets/champions/icons"
DEFAULT_OBS_HOST = "localhost"
DEFAULT_OBS_PORT = 4455
DEFAULT_END_ERROR_LIMIT = 3
DEFAULT_END_POLL_SEC = 5
DEFAULT_EVENT_POLL_SEC = 1
DEFAULT_MAX_STORAGE_GB = 50

OBS_PASSWORD = DEFAULT_OBS_PASSWORD
OBS_SCENE_NAME = DEFAULT_OBS_SCENE_NAME
OBS_SOURCE_NAME = DEFAULT_OBS_SOURCE_NAME
OBS_DIR = DEFAULT_OBS_DIR
BIN_DIR = DEFAULT_BIN_DIR
RECORDINGS_DIR = None
JSON_DIR = None
CHAMPION_ICONS_DIR = None
OBS_HOST = DEFAULT_OBS_HOST
OBS_PORT = DEFAULT_OBS_PORT
END_ERROR_LIMIT = DEFAULT_END_ERROR_LIMIT
END_POLL_SEC = DEFAULT_END_POLL_SEC
EVENT_POLL_SEC = DEFAULT_EVENT_POLL_SEC
MAX_STORAGE_BYTES = None

# ▼ 全員分保存する重要なイベント（オブジェクト）
GLOBAL_OBJECTIVES = [
    "DragonKill",   # ドラゴン
    "BaronKill",    # バロン
    "HeraldKill",   # ヘラルド
    "HordeKill"     # ヴォイドグラブ（内部名称）
]

# ▼ 自分が関与しているかチェックするイベント
COMBAT_EVENTS = [
    "ChampionKill"   # キル / デス（自分が関与したものだけ）
]

# SSL警告の無視
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def resolve_path(value, base_dir):
    if value is None:
        return None
    value = os.path.expandvars(str(value))
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def load_settings():
    if not CONFIG_PATH.exists():
        print("❌ 設定ファイルが見つかりません。")
        print(f"作成先: {CONFIG_PATH}")
        print(f"雛形: {SAMPLE_CONFIG_PATH}")
        print("雛形をコピーして setting.json を作成してください。")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_settings(cfg):
    global OBS_PASSWORD, OBS_SCENE_NAME, OBS_SOURCE_NAME, OBS_DIR, BIN_DIR
    global RECORDINGS_DIR, JSON_DIR, OBS_HOST, OBS_PORT, CHAMPION_ICONS_DIR
    global END_ERROR_LIMIT, END_POLL_SEC, EVENT_POLL_SEC, MAX_STORAGE_BYTES

    obs_cfg = cfg.get("obs", {})
    path_cfg = cfg.get("paths", {})
    poll_cfg = cfg.get("polling", {})
    storage_cfg = cfg.get("storage", {})

    OBS_PASSWORD = obs_cfg.get("password", DEFAULT_OBS_PASSWORD)
    OBS_SCENE_NAME = obs_cfg.get("scene_name", DEFAULT_OBS_SCENE_NAME)
    OBS_SOURCE_NAME = obs_cfg.get("source_name", DEFAULT_OBS_SOURCE_NAME)
    OBS_HOST = obs_cfg.get("host", DEFAULT_OBS_HOST)
    OBS_PORT = int(obs_cfg.get("port", DEFAULT_OBS_PORT))

    obs_dir = resolve_path(obs_cfg.get("dir", DEFAULT_OBS_DIR), ROOT_DIR)
    OBS_DIR = str(obs_dir) if obs_dir else None

    bin_dir = resolve_path(path_cfg.get("bin_dir", DEFAULT_BIN_DIR), ROOT_DIR)
    BIN_DIR = str(bin_dir) if bin_dir else ""

    recordings_dir = resolve_path(path_cfg.get("recordings_dir", DEFAULT_RECORDINGS_DIR), ROOT_DIR)
    json_dir_value = path_cfg.get("json_dir", DEFAULT_JSON_DIR)
    json_dir = resolve_path(json_dir_value, ROOT_DIR)
    champion_icons_dir = resolve_path(path_cfg.get("champion_icons_dir", DEFAULT_CHAMPION_ICONS_DIR), ROOT_DIR)
    if not json_dir and recordings_dir:
        json_dir = recordings_dir / "json"

    RECORDINGS_DIR = recordings_dir
    JSON_DIR = json_dir
    CHAMPION_ICONS_DIR = champion_icons_dir

    END_ERROR_LIMIT = int(poll_cfg.get("end_error_limit", DEFAULT_END_ERROR_LIMIT))
    END_POLL_SEC = float(poll_cfg.get("end_poll_sec", DEFAULT_END_POLL_SEC))
    EVENT_POLL_SEC = float(poll_cfg.get("event_poll_sec", DEFAULT_EVENT_POLL_SEC))

    MAX_STORAGE_BYTES = parse_max_storage_bytes(storage_cfg)

    if JSON_DIR is None:
        print("❌ json_dir の設定が無効です。")
        sys.exit(1)
    JSON_DIR.mkdir(parents=True, exist_ok=True)


def setup_environment():
    """環境変数の設定 (MPVのDLLを読み込めるようにする)"""
    if BIN_DIR:
        os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ["PATH"]

        if not (
            os.path.exists(os.path.join(BIN_DIR, "mpv-1.dll")) or
            os.path.exists(os.path.join(BIN_DIR, "libmpv-1.dll")) or
            os.path.exists(os.path.join(BIN_DIR, "mpv-2.dll")) or
            os.path.exists(os.path.join(BIN_DIR, "libmpv-2.dll"))
        ):
            print("⚠️ 警告: 'bin' フォルダ内に mpv-1.dll / mpv-2.dll (または libmpv-1.dll / libmpv-2.dll) が見つかりません。")
            print(f"探した場所: {BIN_DIR}")
    else:
        print("⚠️ 警告: bin_dir が未設定です。")


def parse_max_storage_bytes(storage_cfg):
    max_bytes = storage_cfg.get("max_size_bytes")
    if isinstance(max_bytes, (int, float)) and max_bytes > 0:
        return int(max_bytes)
    max_gb = storage_cfg.get("max_size_gb", DEFAULT_MAX_STORAGE_GB)
    if isinstance(max_gb, (int, float)) and max_gb > 0:
        return int(float(max_gb) * 1024 * 1024 * 1024)
    max_mb = storage_cfg.get("max_size_mb")
    if isinstance(max_mb, (int, float)) and max_mb > 0:
        return int(float(max_mb) * 1024 * 1024)
    return None


def is_within(child, parent):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def get_dir_size(path):
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except Exception:
                    continue
    except Exception:
        return 0
    return total


def total_storage_size():
    roots = []
    if RECORDINGS_DIR:
        roots.append(Path(RECORDINGS_DIR))
    if JSON_DIR:
        json_path = Path(JSON_DIR)
        if not roots or not is_within(json_path, roots[0]):
            roots.append(json_path)
    return sum(get_dir_size(root) for root in roots if root.exists())


def parse_saved_at(value):
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def load_json_metadata(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_at = parse_saved_at(data.get("saved_at"))
        video_path = data.get("obs_record_path")
        return saved_at, Path(video_path) if video_path else None
    except Exception:
        return None, None


def enforce_storage_limit(keep_paths=None):
    if not MAX_STORAGE_BYTES:
        return

    keep_paths = {Path(p).resolve() for p in keep_paths or [] if p}
    total = total_storage_size()
    if total <= MAX_STORAGE_BYTES:
        return

    if JSON_DIR and Path(JSON_DIR).exists():
        entries = []
        for json_path in Path(JSON_DIR).glob("*.json"):
            saved_at, video_path = load_json_metadata(json_path)
            ts = saved_at if saved_at else json_path.stat().st_mtime
            entries.append((ts, json_path, video_path))
        entries.sort(key=lambda item: item[0])

        for _, json_path, video_path in entries:
            if json_path.resolve() in keep_paths:
                continue
            try:
                if video_path and video_path.exists() and video_path.resolve() not in keep_paths:
                    video_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                json_path.unlink(missing_ok=True)
            except Exception:
                pass
            total = total_storage_size()
            if total <= MAX_STORAGE_BYTES:
                return

    if RECORDINGS_DIR and Path(RECORDINGS_DIR).exists():
        video_exts = {".mp4", ".mkv", ".flv", ".mov", ".avi"}
        video_files = sorted(
            [p for p in Path(RECORDINGS_DIR).rglob("*") if p.is_file() and p.suffix.lower() in video_exts],
            key=lambda p: p.stat().st_mtime
        )
        for video_path in video_files:
            if video_path.resolve() in keep_paths:
                continue
            try:
                video_path.unlink(missing_ok=True)
            except Exception:
                pass
            total = total_storage_size()
            if total <= MAX_STORAGE_BYTES:
                return


def launch_obs():
    """OBSを最小化モードで起動する"""
    if not OBS_DIR:
        print("❌ エラー: OBSのパスが未設定です。")
        sys.exit(1)

    obs_exe = os.path.join(OBS_DIR, "bin", "64bit", "obs64.exe")
    working_dir = os.path.join(OBS_DIR, "bin", "64bit")

    if not os.path.exists(obs_exe):
        print(f"❌ エラー: OBSの実行ファイルが見つかりません。\nパス: {obs_exe}")
        sys.exit(1)

    print("🚀 OBSを起動しています (タスクトレイに最小化)...")
    cmd = [obs_exe, "--portable", "--minimize-to-tray"]

    try:
        process = subprocess.Popen(cmd, cwd=working_dir)
        print("⏳ OBSの起動を待機中...")
        time.sleep(5)
        return process
    except Exception as e:
        print(f"❌ OBS起動エラー: {e}")
        sys.exit(1)


def get_active_player_name():
    """自分のサモナーネームを取得する"""
    try:
        response = requests.get(ACTIVE_PLAYER_URL, verify=False, timeout=5)
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return response.text.strip().replace('"', '')
    except Exception:
        return None


def normalize_summoner_name(value):
    if not value:
        return None
    name = str(value).strip()
    if "#" in name:
        name = name.split("#", 1)[0]
    return name.strip()


def get_event_data():
    """イベントデータを取得する"""
    try:
        response = requests.get(EVENT_URL, verify=False, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def get_all_game_data():
    """ゲーム全体データを取得する"""
    try:
        response = requests.get(ALL_GAME_URL, verify=False, timeout=1)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def build_output_path():
    """重複回避のため、存在しないファイル名を返す"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = JSON_DIR / f"lol_{timestamp}.json"
    if not candidate.exists():
        return candidate
    for i in range(1, 100):
        candidate = JSON_DIR / f"lol_{timestamp}_{i:02d}.json"
        if not candidate.exists():
            return candidate
    time.sleep(1)
    return build_output_path()


def save_payload(path, payload):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)


class LoLAutoRecorder:
    def __init__(self, obs_process=None, status_cb=None):
        self.client = None
        self.my_name = None
        self.obs_process = obs_process
        self.status_cb = status_cb
        self.stop_requested = False
        self.reset_session()
        self.connect_obs()

    def log(self, message):
        print(message)
        if self.status_cb:
            try:
                self.status_cb(message)
            except Exception:
                pass

    def request_stop(self):
        self.stop_requested = True

    def should_stop(self):
        return self.stop_requested

    def reset_session(self):
        self.output_file = None
        self.sync_game_time = 0.0
        self.record_path = None
        self.recording_started = False
        self.session_started = False
        self.saved_events = []
        self.all_events = []
        self.processed_event_keys = set()
        self.all_event_keys = set()
        self.my_name = None
        self.my_name_short = None
        self.last_game_data = None
        self.champion_name = None
        self.player_team = None
        self.game_result = None
        self.winning_team = None

    def has_session_data(self):
        return (
            self.session_started
            or self.recording_started
            or self.record_path is not None
            or self.sync_game_time > 0.0
            or bool(self.saved_events)
            or bool(self.all_events)
        )

    def connect_obs(self):
        """OBS WebSocketに接続"""
        retry_count = 0
        while retry_count < 5:
            try:
                self.client = obs.ReqClient(
                    host=OBS_HOST,
                    port=OBS_PORT,
                    password=OBS_PASSWORD
                )
                version = self.client.get_version()
                print(f"✅ OBS接続成功 (v{version.obs_version})")
                return
            except Exception:
                retry_count += 1
                print(f"Connection retrying... ({retry_count}/5)")
                time.sleep(2)

        print("❌ OBSへの接続に失敗しました。パスワードやポートを確認してください。")
        sys.exit(1)

    def get_source_id(self):
        """同期用ソース(赤色)のIDを取得"""
        try:
            items = self.client.get_scene_item_list(OBS_SCENE_NAME).scene_items
            for item in items:
                if item['sourceName'] == OBS_SOURCE_NAME:
                    return item['sceneItemId']
        except Exception as e:
            print(f"⚠️ シーンアイテム取得エラー: {e}")
        return None

    def try_update_player_name(self):
        name = get_active_player_name()
        if name and name != self.my_name:
            self.my_name = name
            self.my_name_short = normalize_summoner_name(name)
            self.log(f"プレイヤー名を特定: {self.my_name}")

    def update_player_info_from_game_data(self, data):
        if not data or not self.my_name:
            return
        players = data.get("allPlayers", [])
        for player in players:
            summoner = player.get("summonerName") or player.get("summoner_name")
            if summoner == self.my_name or summoner == self.my_name_short:
                self.champion_name = player.get("championName") or player.get("champion_name")
                self.player_team = player.get("team")
                return

    def update_result_from_events(self, events):
        if not events:
            return
        end_names = {"GameEnd", "EndGame", "GameEnded", "GameComplete"}
        for event in events:
            name = event.get("EventName")
            if name not in end_names:
                continue
            result_value = event.get("Result") or event.get("result") or event.get("GameResult") or event.get("gameResult")
            winning_team = event.get("WinningTeam") or event.get("winningTeam") or event.get("Team") or event.get("team")
            self.game_result = result_value
            self.winning_team = winning_team
            return

    def wait_for_game_start(self):
        """LoLの試合開始を監視"""
        self.log("⚔️  LoLの試合開始を待機中 (API監視)...")
        while True:
            if self.should_stop():
                return False
            data = get_all_game_data()
            if data:
                game_time = data.get('gameData', {}).get('gameTime', 0)
                if game_time > 0:
                    self.log(f"🔥 試合開始検知！ GameTime: {game_time:.2f}s")
                    self.output_file = build_output_path()
                    self.try_update_player_name()
                    self.session_started = True
                    return True
            time.sleep(1)

    def start_recording(self):
        """録画開始 -> 同期マーカー"""
        self.log("🎥 録画を開始します...")
        try:
            self.client.start_record()
            self.recording_started = True
        except OBSSDKRequestError as e:
            self.log(f"⚠️ 録画開始エラー: {e}")
            return
        except Exception as e:
            self.log(f"⚠️ 録画開始エラー: {e}")
            return
        time.sleep(2)

        item_id = self.get_source_id()
        if not item_id:
            self.log(f"⚠️ エラー: ソース '{OBS_SOURCE_NAME}' が見つかりません。同期なしで録画します。")
            return

        event_time = self.wait_until_game_start_event()
        if self.should_stop():
            return

        self.log("⚡ 同期シグナル送信 (Marker ON)")
        self.client.set_scene_item_enabled(OBS_SCENE_NAME, item_id, True)

        sync_time = 0.0
        data = get_all_game_data()
        if data:
            sync_time = data.get('gameData', {}).get('gameTime', 0.0)
        if (not sync_time or sync_time <= 0) and event_time is not None:
            sync_time = float(event_time)

        self.sync_game_time = sync_time
        self.log(f"📝 同期ログ記録: {sync_time:.4f}s")

        time.sleep(0.5)
        self.client.set_scene_item_enabled(OBS_SCENE_NAME, item_id, False)
        self.log("✅ シグナル消灯。録画継続中。")

    def wait_until_game_start_event(self, timeout_sec=180):
        start = time.time()
        while time.time() - start < timeout_sec:
            if self.should_stop():
                return None
            event_data = get_event_data()
            if event_data:
                for event in event_data.get("Events", []):
                    if event.get("EventName") == "GameStart":
                        return event.get("EventTime", 0.0)
            time.sleep(0.5)
        self.log("⚠️ GameStart を検知できませんでした。現在のゲーム時間で同期します。")
        return None

    def process_events(self, events):
        for event in events:
            event_id = event.get("EventID")
            event_name = event.get("EventName")
            event_time = event.get("EventTime", 0.0)

            event_key = event_id if event_id is not None else f"{event_name}_{event_time}"

            if event_key not in self.all_event_keys:
                self.all_events.append(event)
                self.all_event_keys.add(event_key)

            if event_key in self.processed_event_keys:
                continue

            should_save = False
            log_message = ""

            if event_name in GLOBAL_OBJECTIVES:
                should_save = True
                log_message = f"[OBJECTIVE] {event_name}"

            elif event_name in COMBAT_EVENTS and self.my_name:
                killer = event.get("KillerName")
                victim = event.get("VictimName")
                assisters = event.get("Assisters", [])

                # 自分が関与したキル or デスのみ
                is_involved = (
                    killer == self.my_name or victim == self.my_name or
                    killer == self.my_name_short or victim == self.my_name_short
                )

                if is_involved:
                    should_save = True
                    if killer == self.my_name:
                        role = "KILL"
                    elif victim == self.my_name:
                        role = "DEATH"
                    else:
                        role = "ASSIST"
                    log_message = f"[{role}] {event_name}"

            if should_save:
                try:
                    time_text = f"{float(event_time):.1f}"
                except Exception:
                    time_text = "?"
                print(f"{log_message} (Time: {time_text})")
                self.saved_events.append(event)

            self.processed_event_keys.add(event_key)

    def record_until_end(self):
        """試合終了まで待機して録画停止"""
        self.log("🛡️  試合終了を監視中...")
        error_count = 0
        while True:
            if self.should_stop():
                return False
            data = get_all_game_data()
            if not data:
                error_count += 1
                if error_count >= END_ERROR_LIMIT:
                    self.log("🏁 試合終了検知。録画を停止します。")
                    return True
                time.sleep(END_POLL_SEC)
                continue

            error_count = 0
            self.last_game_data = data
            if not self.my_name:
                self.try_update_player_name()
            self.update_player_info_from_game_data(data)

            event_data = get_event_data()
            if event_data:
                events = event_data.get("Events", [])
                self.process_events(events)
                self.update_result_from_events(events)

            time.sleep(EVENT_POLL_SEC)
        return True

    def stop_recording(self):
        if not self.client or self.record_path is not None:
            return
        if not self.recording_started:
            return

        try:
            status = self.client.get_record_status()
            is_active = getattr(status, "output_active", None)
            if is_active is False:
                self.recording_started = False
                return
        except Exception:
            pass

        try:
            res = self.client.stop_record()
            self.record_path = getattr(res, "output_path", None)
            if self.record_path:
                self.log(f"💾 保存完了: {self.record_path}")
            self.recording_started = False
        except OBSSDKRequestError as e:
            if e.code == 501:
                self.recording_started = False
                return
            self.log(f"⚠️ 録画停止エラー: {e}")
        except Exception as e:
            self.log(f"⚠️ 録画停止エラー: {e}")

    def shutdown_obs(self):
        if not self.obs_process:
            return
        self.log("🧹 OBSを終了しています...")

        if self.obs_process.poll() is not None:
            return

        def enum_windows_callback(hwnd, lparam):
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.obs_process.pid:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True

        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(callback(enum_windows_callback), 0)

        time.sleep(1)

        if self.client:
            self.client.disconnect()

    def save_json(self):
        if self.output_file is None:
            self.output_file = build_output_path()

        if self.last_game_data and self.game_result is None:
            game_data = self.last_game_data.get("gameData", {})
            if isinstance(game_data, dict):
                self.game_result = game_data.get("gameResult") or game_data.get("result")
                self.winning_team = self.winning_team or game_data.get("winningTeam") or game_data.get("winning_team")

        payload = {
            "summoner_name": self.my_name,
            "champion_name": self.champion_name,
            "player_team": self.player_team,
            "game_result": self.game_result,
            "winning_team": self.winning_team,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sync_game_time": self.sync_game_time,
            "obs_record_path": self.record_path,
            "paths": {
                "recordings_dir": str(RECORDINGS_DIR) if RECORDINGS_DIR else None,
                "json_path": str(self.output_file)
            },
            "events": self.saved_events,
            "events_all": self.all_events,
            "counts": {
                "filtered": len(self.saved_events),
                "all": len(self.all_events)
            }
        }
        save_payload(self.output_file, payload)
        self.log(f"ログ保存完了: {self.output_file}")
        enforce_storage_limit(keep_paths=[self.output_file, self.record_path])


if __name__ == "__main__":
    settings = load_settings()
    apply_settings(settings)

    setup_environment()
    obs_process = launch_obs()

    app = LoLAutoRecorder(obs_process=obs_process)
    try:
        while True:
            app.reset_session()
            app.wait_for_game_start()
            app.start_recording()
            app.record_until_end()
            app.stop_recording()
            app.save_json()
            print("✅ 試合記録完了。次の試合を待機します。")
    except KeyboardInterrupt:
        print("\n中断を検知しました。終了処理を行います。")
    finally:
        app.stop_recording()
        if app.has_session_data():
            app.save_json()
        app.shutdown_obs()
        print("👋 全ての処理が完了しました。")
