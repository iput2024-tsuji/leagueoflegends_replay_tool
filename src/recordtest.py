import os
import sys
import time
import json
import subprocess
from pathlib import Path

import requests
import urllib3
import obsws_python as obs

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "setting.json"
SAMPLE_CONFIG_PATH = ROOT_DIR / "config" / "setting.sample.json"

LIVECLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
ACTIVE_PLAYER_URL = f"{LIVECLIENT_BASE}/activeplayername"
EVENT_URL = f"{LIVECLIENT_BASE}/eventdata"
ALL_GAME_URL = f"{LIVECLIENT_BASE}/allgamedata"

DEFAULT_OBS_PASSWORD = "password"
DEFAULT_OBS_SCENE_NAME = "lol_seen"
DEFAULT_OBS_SOURCE_NAME = "color"
DEFAULT_OBS_DIR = "C:/dev/lol/bin/OBS-Studio"
DEFAULT_BIN_DIR = "bin"
DEFAULT_RECORDINGS_DIR = "recordings"
DEFAULT_JSON_DIR = "recordings/json"
DEFAULT_OBS_HOST = "localhost"
DEFAULT_OBS_PORT = 4455
DEFAULT_END_ERROR_LIMIT = 3
DEFAULT_END_POLL_SEC = 5
DEFAULT_EVENT_POLL_SEC = 1

OBS_PASSWORD = DEFAULT_OBS_PASSWORD
OBS_SCENE_NAME = DEFAULT_OBS_SCENE_NAME
OBS_SOURCE_NAME = DEFAULT_OBS_SOURCE_NAME
OBS_DIR = DEFAULT_OBS_DIR
BIN_DIR = DEFAULT_BIN_DIR
RECORDINGS_DIR = None
JSON_DIR = None
OBS_HOST = DEFAULT_OBS_HOST
OBS_PORT = DEFAULT_OBS_PORT
END_ERROR_LIMIT = DEFAULT_END_ERROR_LIMIT
END_POLL_SEC = DEFAULT_END_POLL_SEC
EVENT_POLL_SEC = DEFAULT_EVENT_POLL_SEC

# ▼ 全員分保存する重要なイベント（オブジェクト）
GLOBAL_OBJECTIVES = [
    "DragonKill",   # ドラゴン
    "BaronKill",    # バロン
    "HeraldKill",   # ヘラルド
    "HordeKill"     # ヴォイドグラブ（内部名称）
]

# ▼ 自分が関与しているかチェックするイベント
COMBAT_EVENTS = [
    "ChampionKill",  # キル
    "TurretKilled",  # タワー破壊
    "InhibKilled"    # インヒビター破壊
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
    global RECORDINGS_DIR, JSON_DIR, OBS_HOST, OBS_PORT
    global END_ERROR_LIMIT, END_POLL_SEC, EVENT_POLL_SEC

    obs_cfg = cfg.get("obs", {})
    path_cfg = cfg.get("paths", {})
    poll_cfg = cfg.get("polling", {})

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
    if not json_dir and recordings_dir:
        json_dir = recordings_dir / "json"

    RECORDINGS_DIR = recordings_dir
    JSON_DIR = json_dir

    END_ERROR_LIMIT = int(poll_cfg.get("end_error_limit", DEFAULT_END_ERROR_LIMIT))
    END_POLL_SEC = float(poll_cfg.get("end_poll_sec", DEFAULT_END_POLL_SEC))
    EVENT_POLL_SEC = float(poll_cfg.get("event_poll_sec", DEFAULT_EVENT_POLL_SEC))

    if JSON_DIR is None:
        print("❌ json_dir の設定が無効です。")
        sys.exit(1)
    JSON_DIR.mkdir(parents=True, exist_ok=True)


def setup_environment():
    """環境変数の設定 (MPVのDLLを読み込めるようにする)"""
    if BIN_DIR:
        os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ["PATH"]

        if not os.path.exists(os.path.join(BIN_DIR, "mpv-1.dll")) and \
           not os.path.exists(os.path.join(BIN_DIR, "libmpv-1.dll")):
            print("⚠️ 警告: 'bin' フォルダ内に mpv-1.dll (または libmpv-1.dll) が見つかりません。")
            print(f"探した場所: {BIN_DIR}")
    else:
        print("⚠️ 警告: bin_dir が未設定です。")


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
        subprocess.Popen(cmd, cwd=working_dir)
        print("⏳ OBSの起動を待機中...")
        time.sleep(5)
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
    def __init__(self):
        self.client = None
        self.output_file = None
        self.my_name = None
        self.sync_game_time = 0.0
        self.record_path = None
        self.saved_events = []
        self.all_events = []
        self.processed_event_keys = set()
        self.all_event_keys = set()
        self.connect_obs()

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
            print(f"プレイヤー名を特定: {self.my_name}")

    def wait_for_game_start(self):
        """LoLの試合開始を監視"""
        print("⚔️  LoLの試合開始を待機中 (API監視)...")
        while True:
            data = get_all_game_data()
            if data:
                game_time = data.get('gameData', {}).get('gameTime', 0)
                if game_time > 0:
                    print(f"🔥 試合開始検知！ GameTime: {game_time:.2f}s")
                    self.output_file = build_output_path()
                    self.try_update_player_name()
                    return
            time.sleep(1)

    def start_recording(self):
        """録画開始 -> 同期マーカー"""
        print("🎥 録画を開始します...")
        self.client.start_record()
        time.sleep(2)

        item_id = self.get_source_id()
        if not item_id:
            print(f"⚠️ エラー: ソース '{OBS_SOURCE_NAME}' が見つかりません。同期なしで録画します。")
            return

        print("⚡ 同期シグナル送信 (Marker ON)")
        self.client.set_scene_item_enabled(OBS_SCENE_NAME, item_id, True)

        sync_time = 0.0
        data = get_all_game_data()
        if data:
            sync_time = data.get('gameData', {}).get('gameTime', 0.0)

        self.sync_game_time = sync_time
        print(f"📝 同期ログ記録: {sync_time:.4f}s")

        time.sleep(0.5)
        self.client.set_scene_item_enabled(OBS_SCENE_NAME, item_id, False)
        print("✅ シグナル消灯。録画継続中。")

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

                is_involved = (
                    killer == self.my_name or
                    victim == self.my_name or
                    self.my_name in assisters
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
        print("🛡️  試合終了を監視中...")
        error_count = 0
        while True:
            data = get_all_game_data()
            if not data:
                error_count += 1
                if error_count >= END_ERROR_LIMIT:
                    print("🏁 試合終了検知。録画を停止します。")
                    break
                time.sleep(END_POLL_SEC)
                continue

            error_count = 0
            if not self.my_name:
                self.try_update_player_name()

            event_data = get_event_data()
            if event_data:
                self.process_events(event_data.get("Events", []))

            time.sleep(EVENT_POLL_SEC)

    def stop_recording(self):
        if not self.client or self.record_path is not None:
            return
        try:
            res = self.client.stop_record()
            self.record_path = getattr(res, "output_path", None)
            if self.record_path:
                print(f"💾 保存完了: {self.record_path}")
        except Exception as e:
            print(f"⚠️ 録画停止エラー: {e}")

    def save_json(self):
        if self.output_file is None:
            self.output_file = build_output_path()

        payload = {
            "summoner_name": self.my_name,
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
        print(f"ログ保存完了: {self.output_file}")


if __name__ == "__main__":
    settings = load_settings()
    apply_settings(settings)

    setup_environment()
    launch_obs()

    app = LoLAutoRecorder()
    app.wait_for_game_start()
    app.start_recording()

    try:
        app.record_until_end()
    except KeyboardInterrupt:
        print("\nログ収集を終了します。")
    finally:
        app.stop_recording()
        app.save_json()
        print("👋 全ての処理が完了しました。")
