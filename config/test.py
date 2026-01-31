import requests
import json
import time
import urllib3
from pathlib import Path

# SSL警告を無視
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://127.0.0.1:2999/liveclientdata"
END_TIMEOUT_SEC = 5

# ▼ 全員分保存する重要なイベント（オブジェクト）
GLOBAL_OBJECTIVES = [
    "DragonKill",   # ドラゴン
    "BaronKill",    # バロン
    "HeraldKill",   # ヘラルド
    "HordeKill"     # ヴォイドグラブ（内部名称）
]

# ▼ 自分が関与しているかチェックするイベント
COMBAT_EVENTS = [
    "ChampionKill", # キル
    "TurretKilled", # タワー破壊
    "InhibKilled"   # インヒビター破壊
]

def get_active_player_name():
    """自分のサモナーネームを取得する"""
    try:
        url = f"{BASE_URL}/activeplayername"
        response = requests.get(url, verify=False, timeout=5)
        response.raise_for_status()
        # 文字列の整形（JSON形式の場合と生テキストの場合に対応）
        try:
            return response.json()
        except:
            return response.text.strip().replace('"', '')
    except:
        return None

def get_event_data():
    """イベントデータを取得する"""
    try:
        url = f"{BASE_URL}/eventdata"
        response = requests.get(url, verify=False, timeout=5)
        response.raise_for_status()
        return response.json()
    except:
        return None

def build_output_path(my_name):
    """重複回避のため、存在しないファイル名を返す"""
    output_dir = Path(__file__).resolve().parent.parent / "recordings" / "json"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"lol_{timestamp}.json"
    candidate = output_dir / base_name
    if not candidate.exists():
        return candidate
    # 同一秒での重複を避ける
    for i in range(1, 100):
        candidate = output_dir / f"lol_events_{my_name}_{timestamp}_{i:02d}.json"
        if not candidate.exists():
            return candidate
    # ここに来るのは稀なので、最後は時間を少し待って再生成
    time.sleep(1)
    return build_output_path(my_name)

def save_events(path, events):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(events, f, indent=4, ensure_ascii=False)

def main():
    print("--- LoL Hybrid Event Logger ---")
    print("試合開始とプレイヤー名を待機中...")

    # 1. 自分の名前を特定
    my_name = None
    while my_name is None:
        my_name = get_active_player_name()
        if my_name:
            print(f"プレイヤー名を特定: {my_name}")
        else:
            time.sleep(2)

    output_file = build_output_path(my_name)
    processed_event_ids = set()
    all_event_ids = set()
    saved_events = []
    all_events = []

    print(f"ログ収集開始。出力先: {output_file}")
    last_success = time.time()

    try:
        while True:
            data = get_event_data()
            if data is None:
                if time.time() - last_success >= END_TIMEOUT_SEC:
                    print("ゲーム終了を検知。ログを保存します。")
                    break
                time.sleep(2)
                continue
            last_success = time.time()

            events = data.get("Events", [])

            for event in events:
                event_id = event.get("EventID")
                event_name = event.get("EventName")

                # 全イベント保存（重複回避）
                if event_id not in all_event_ids:
                    all_events.append(event)
                    all_event_ids.add(event_id)

                # すでに処理済みのイベントはスキップ
                if event_id in processed_event_ids:
                    continue

                # --- 保存するかどうかの判定ロジック ---
                should_save = False
                log_message = ""

                # 判定1: 全保存するオブジェクトか？
                if event_name in GLOBAL_OBJECTIVES:
                    should_save = True
                    log_message = f"[OBJECTIVE] {event_name}"

                # 判定2: 戦闘・タワー破壊系か？（自分に関係あるかチェック）
                elif event_name in COMBAT_EVENTS:
                    killer = event.get("KillerName")
                    victim = event.get("VictimName")
                    assisters = event.get("Assisters", [])

                    # 自分が キルした OR 死んだ OR アシストした
                    is_involved = (
                        killer == my_name or 
                        victim == my_name or 
                        my_name in assisters
                    )

                    if is_involved:
                        should_save = True
                        if killer == my_name: role = "KILL"
                        elif victim == my_name: role = "DEATH"
                        else: role = "ASSIST"
                        log_message = f"[{role}] {event_name}"

                # 保存フラグが立っていたらリストに追加
                if should_save:
                    print(f"{log_message} (Time: {event.get('EventTime'):.1f})")
                    saved_events.append(event)
                
                # 保存したかどうかにかかわらず、IDは処理済みとして記録
                # (関係ない他人のキルなども、毎回チェックしないようにするため)
                processed_event_ids.add(event_id)

            time.sleep(1)

    except KeyboardInterrupt:
        print("\nログ収集を終了します。")
    finally:
        payload = {
            "summoner_name": my_name,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "events": saved_events,
            "events_all": all_events,
            "counts": {
                "filtered": len(saved_events),
                "all": len(all_events)
            }
        }
        save_events(output_file, payload)
        print(f"ログ保存完了: {output_file}")

if __name__ == "__main__":
    main()
