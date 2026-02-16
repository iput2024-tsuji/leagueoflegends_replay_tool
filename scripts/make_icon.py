from pathlib import Path
import os

from PIL import Image, ImageDraw, ImageFont

# --- 設定（ここを変えれば好きにいじれる） ---
TEXT = "辻"
BG_COLOR = "#2B0F4B"  # 決めた濃紫
TEXT_COLOR = "#FFFFFF"
SIZE = 256
PADDING = 0.12  # 余白12%
CORNER_RADIUS = 28  # 角丸の半径
ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "assets" / "app"
PNG_PATH = OUTPUT_DIR / "app.png"
ICO_PATH = OUTPUT_DIR / "app.ico"

def create_icon():
    # 1. ベース作成（透明背景）
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 2. 角丸正方形を描画
    # 左上(0,0)から右下(SIZE, SIZE)まで
    draw.rounded_rectangle((0, 0, SIZE, SIZE), radius=CORNER_RADIUS, fill=BG_COLOR)

    # 3. フォント設定（Windows標準の太いゴシックを探す）
    # MacやLinuxの場合は適宜パスが変わるが、Windowsを想定
    font_path = "C:\\Windows\\Fonts\\msgothic.ttc" # MSゴシック
    if not os.path.exists(font_path):
         # 見つからない場合のフォールバック（環境に合わせて変更可）
        font_path = "arial.ttf" 
    
    # フォントサイズを計算（枠に収まる最大サイズ）
    # ※少し大きめにして、余白を考慮
    font_size = int(SIZE * (1 - PADDING * 2))
    
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()
        print("警告: 太字フォントが見つからず、デフォルトフォントを使用しました。見た目が細くなる可能性があります。")

    # 4. 文字を中央に配置
    # テキストのバウンディングボックスを取得して中心を計算
    bbox = draw.textbbox((0, 0), TEXT, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    # 実際の中央位置（微調整含む）
    x = (SIZE - text_w) / 2 - bbox[0]
    y = (SIZE - text_h) / 2 - bbox[1] - (SIZE * 0.05) # 漢字は重心が下がりがちなので少し上げる

    draw.text((x, y), TEXT, font=font, fill=TEXT_COLOR)

    # 5. 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # PNG保存
    img.save(PNG_PATH)
    print(f"PNG作成完了: {PNG_PATH}")

    # ICO保存（マルチサイズ格納）
    # 256, 48, 32, 16のサイズを含める
    icon_sizes = [(256, 256), (48, 48), (32, 32), (16, 16)]
    img.save(ICO_PATH, format='ICO', sizes=icon_sizes)
    print(f"ICO作成完了: {ICO_PATH}")

if __name__ == "__main__":
    create_icon()
