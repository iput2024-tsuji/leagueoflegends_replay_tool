import requests
import json
import time
import urllib3

# SSL警告を無視
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://127.0.0.1:2999/liveclientdata"

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

    output_file = f"lol_events_{my_name}.json"
    processed_event_ids = set()
    saved_events = []

    print(f"ログ収集開始。出力先: {output_file}")

    try:
        while True:
            data = get_event_data()
            if data is None:
                time.sleep(5)
                continue

            events = data.get("Events", [])
            new_data_found = False

            for event in events:
                event_id = event.get("EventID")
                event_name = event.get("EventName")

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
                    new_data_found = True
                
                # 保存したかどうかにかかわらず、IDは処理済みとして記録
                # (関係ない他人のキルなども、毎回チェックしないようにするため)
                processed_event_ids.add(event_id)

            # ファイルへの書き込み
            if new_data_found:
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(saved_events, f, indent=4, ensure_ascii=False)
            
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nログ収集を終了します。")

if __name__ == "__main__":
    main()