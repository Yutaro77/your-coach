from flask import Flask, request, abort, jsonify
from flask_cors import CORS
import json
import hmac
import hashlib
import base64
import requests
import os
import threading

app = Flask(__name__)
CORS(app)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
SHEET_API_URL = os.environ.get('SHEET_API_URL')
DIFY_API_URL = 'https://api.dify.ai/v1/chat-messages'

# メモリ上のキャッシュ（サーバー起動中はここを優先的に使う）
# user_id → {"conversation_id": "...", "status": "...", "plan_type": ...} のようなJSON
user_states = {}
cache_loaded = False
cache_lock = threading.Lock()

def load_state_cache():
    """起動時にスプレッドシートから状態を読み込む"""
    global user_states, cache_loaded
    try:
        res = requests.get(SHEET_API_URL, timeout=15)
        if res.status_code == 200:
            data = res.json()
            with cache_lock:
                user_states.update(data)
            print(f'状態キャッシュ読み込み完了: {len(data)}件')
        else:
            print(f'状態キャッシュ読み込み失敗: {res.status_code}')
    except Exception as e:
        print(f'状態キャッシュ読み込みエラー: {e}')
    cache_loaded = True

def save_user_state(user_id, updates):
    """変更したいキーだけをApps Scriptに送る（非同期）。
    Apps Script側で既存データとマージしてくれる。"""
    def _save():
        try:
            payload = {'user_id': user_id}
            payload.update(updates)
            res = requests.post(SHEET_API_URL, json=payload, timeout=15)
            print(f'状態保存: {res.status_code}')
        except Exception as e:
            print(f'状態保存エラー: {e}')
    threading.Thread(target=_save).start()

def get_user_state(user_id):
    """メモリにあれば使う。なければスプレッドシートを直接確認する"""
    with cache_lock:
        if user_id in user_states:
            return user_states[user_id]
    # メモリにない場合はシートを直接見に行く（再起動直後など）
    try:
        res = requests.get(SHEET_API_URL, timeout=15)
        if res.status_code == 200:
            data = res.json()
            with cache_lock:
                user_states.update(data)
            return data.get(user_id, {})
    except Exception as e:
        print(f'個別状態取得エラー: {e}')
    return {}

def update_user_state(user_id, updates):
    """メモリとシートの両方を、指定したキーだけ更新する"""
    with cache_lock:
        current = user_states.get(user_id, {})
        current = {**current, **updates}
        user_states[user_id] = current
    save_user_state(user_id, updates)

def get_conversation_id(user_id):
    return get_user_state(user_id).get('conversation_id', '')

def set_conversation_id(user_id, conversation_id):
    update_user_state(user_id, {'conversation_id': conversation_id})

def verify_signature(body, signature):
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    return base64.b64encode(hash).decode('utf-8') == signature

def send_line_push(user_id, message):
    try:
        line_response = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers={
                'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
                'Content-Type': 'application/json'
            },
            json={
                'to': user_id,
                'messages': [{'type': 'text', 'text': message}]
            }
        )
        print(f'LINE Pushステータス: {line_response.status_code}')
    except Exception as e:
        print(f'LINE送信エラー: {e}')

# --- Dify Chatflow用：メッセージ種別の判定とinputs組み立て ---

BUTTON_TO_TYPE = {
    "起床": "起床",
    "トレーニング開始": "トレーニング開始",
    "就寝": "就寝",
    "トレーニングメニュー": "トレーニングメニュー",
    "食事プログラム": "食事プログラム",
    "このアプリの使い方": "使い方",
}

def resolve_message_type(line_text):
    """ボタンのテキストをmessage_typeに変換する。
    どれにも当てはまらなければフリー会話扱い"""
    return BUTTON_TO_TYPE.get(line_text.strip(), "フリー会話")

def build_dify_inputs(message_type):
    """Chatflowが必須とする6変数を組み立てる。
    phase以下4つは仮値（次フェーズで実データに差し替え予定）"""
    return {
        "message_type": message_type,
        "phase": "phase1",
        "plan_type": "標準",
        "is_first_day": "false",
        "today_is_training_day": "true",
        "missed_last_session": "false",
    }

def ask_dify(user_id, message, conversation_id='', inputs=None):
    dify_response = requests.post(
        DIFY_API_URL,
        headers={
            'Authorization': f'Bearer {DIFY_API_KEY}',
            'Content-Type': 'application/json'
        },
        json={
            'inputs': inputs or {},
            'query': message,
            'response_mode': 'blocking',
            'conversation_id': conversation_id,
            'user': user_id
        },
        timeout=60
    )
    print(f'Difyステータス: {dify_response.status_code}')
    if dify_response.status_code == 200:
        data = dify_response.json()
        return data.get('answer', ''), data.get('conversation_id', '')
    else:
        print(f'Dify失敗: {dify_response.text[:300]}')
        return None, None

PLAN_KEYWORDS = {
    '集中': ['集中'],
    '標準': ['標準'],
    'ゆっくり': ['ゆっくり'],
}

def resolve_plan_choice(text):
    """ユーザーの自由文からプラン選択（集中/標準/ゆっくり）を検知する。
    どれにも当てはまらなければNoneを返す。"""
    text = text.strip()
    if '①' in text or text in ('1', '１'):
        return '集中'
    if '②' in text or text in ('2', '２'):
        return '標準'
    if '③' in text or text in ('3', '３'):
        return 'ゆっくり'
    for plan, keywords in PLAN_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return plan
    return None

def handle_plan_confirmed(user_id, plan_choice):
    """プラン確定を検知した時の処理。
    週間サイクル生成・Sheets保存・出力（C・D・E・F）は次のステップで実装する。
    今は検知できたことを確認するための仮実装。"""
    print(f'プラン確定を検知: user={user_id} plan={plan_choice}')
    update_user_state(user_id, {'status': 'plan_confirmed', 'plan_type': plan_choice})
    send_line_push(
        user_id,
        f'「{plan_choice}プラン」を選んだね！\n（週間メニューの作成は次のステップで実装予定だよ）'
    )

def process_message(user_id, user_message):
    try:
        conversation_id = get_conversation_id(user_id)

        if not conversation_id:
            liff_url = os.environ.get('LIFF_REGISTER_URL', '')
            send_line_push(
                user_id,
                f'まずは登録をお願いします！\n{liff_url}\n登録が終わったら、いろいろ話しかけてね。'
            )
            return

        state = get_user_state(user_id)
        if state.get('status') == 'awaiting_plan':
            plan_choice = resolve_plan_choice(user_message)
            if plan_choice:
                handle_plan_confirmed(user_id, plan_choice)
                return
            # プランの言葉にマッチしなければ、通常のフリー会話として処理を続ける

        from datetime import datetime
        import pytz
        jst = pytz.timezone('Asia/Tokyo')
        now = datetime.now(jst)
        weekdays = ['月', '火', '水', '木', '金', '土', '日']
        today_str = f"{now.year}年{now.month}月{now.day}日（{weekdays[now.weekday()]}）"

        # 起床・トレーニング開始・就寝ボタンなど、日付に関わる操作の時は
        # 実際の日付情報をメッセージに付与する
        message_with_date = f"[今日の日付：{today_str}]\n{user_message}"

        message_type = resolve_message_type(user_message)
        dify_inputs = build_dify_inputs(message_type)

        answer, new_conv_id = ask_dify(user_id, message_with_date, conversation_id, dify_inputs)
        if answer:
            set_conversation_id(user_id, new_conv_id)
            send_line_push(user_id, answer)
        else:
            send_line_push(user_id, 'すみません、もう一度送ってね！')
    except Exception as e:
        print(f'エラー発生: {e}')

def get_line_image(message_id):
    try:
        res = requests.get(
            f'https://api-data.line.me/v2/bot/message/{message_id}/content',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'},
            timeout=30
        )
        if res.status_code == 200:
            return base64.b64encode(res.content).decode('utf-8')
        else:
            print(f'画像取得失敗: {res.status_code}')
            return None
    except Exception as e:
        print(f'画像取得エラー: {e}')
        return None

def analyze_goal_image(image_base64, media_type='image/jpeg'):
    """初回登録時にアップロードされた『なりたい体型』の画像を解析する"""
    try:
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'claude-sonnet-4-5',
                'max_tokens': 500,
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image',
                                'source': {
                                    'type': 'base64',
                                    'media_type': media_type,
                                    'data': image_base64
                                }
                            },
                            {
                                'type': 'text',
                                'text': """この画像は、ユーザーが「なりたい体型の参考」として
アップロードしたものです。トレーニング・食事プランの
作成に使うため、以下の項目を分析してください。

・推定される体脂肪率の範囲（％、目安として）
・筋肉のつき方の特徴（厚みがあるか、引き締まっているか等）
・特に発達している部位（胸・肩・腹筋・脚など）
・全体的な体型の方向性（細身/筋肉質/バランス型など）

写真が体型を判断できないもの（顔のみ、服を着ていて
体型が見えない、人物が写っていない等）の場合は
「体型を判断できる画像ではありません」とだけ答えてください。

出力は短く、以下の形式で。

体脂肪率目安：○〜○%
特徴：（1文）
重点部位：（部位名を2〜3個）
方向性：（1語〜数語）

写真の人物を特定したり、実名を推測したりはしないでください。
あくまで体型の特徴のみを分析してください。"""
                            }
                        ]
                    }
                ]
            },
            timeout=30
        )
        print(f'目標画像解析ステータス: {response.status_code}')
        if response.status_code == 200:
            data = response.json()
            return data['content'][0]['text']
        else:
            print(f'目標画像解析失敗: {response.text[:300]}')
            return None
    except Exception as e:
        print(f'目標画像解析エラー: {e}')
        return None

def analyze_food_image(image_base64, user_id):
    try:
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'claude-sonnet-4-5',
                'max_tokens': 1000,
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image',
                                'source': {
                                    'type': 'base64',
                                    'media_type': 'image/jpeg',
                                    'data': image_base64
                                }
                            },
                            {
                                'type': 'text',
                                'text': """この食事の写真を分析して、以下の形式で日本語で答えてください。
絵文字は使わず、親しみやすいが結果にこだわるコーチとして答えること。
根拠のない褒めはしない。数字で現実を示して、改善点は具体的に言う。

---
【食事内容】
（何が写っているか簡潔に）

【栄養素の目安】
カロリー：約○kcal
タンパク質：約○g
脂質：約○g
炭水化物：約○g

【コーチからひとこと】
以下のルールで必ずコメントする。
・タンパク質が体重×2gの目標に対して足りているか
・カロリーが多すぎる・少なすぎる場合は必ず指摘する
・「問題ない」で終わらず、次の食事で何をすべきか具体的に言う
・褒める場合も「〜はいい。ただ〜」の形で改善点を必ずセットで言う
・2〜3文で簡潔に。長くしない。
---

写真が食事でない場合は「食事の写真を送ってね！」とだけ返してください。"""
                            }
                        ]
                    }
                ]
            },
            timeout=30
        )
        print(f'Claude APIステータス: {response.status_code}')
        if response.status_code == 200:
            data = response.json()
            return data['content'][0]['text']
        else:
            print(f'Claude API失敗: {response.text[:300]}')
            return None
    except Exception as e:
        print(f'画像分析エラー: {e}')
        return None

def process_image_message(user_id, message_id):
    try:
        send_line_push(user_id, '写真を確認してるよ、少し待ってね！')

        image_base64 = get_line_image(message_id)
        if not image_base64:
            send_line_push(user_id, '画像の取得に失敗しました。もう一度送ってね！')
            return

        result = analyze_food_image(image_base64, user_id)
        if result:
            send_line_push(user_id, result)
        else:
            send_line_push(user_id, '分析に失敗しました。もう一度送ってね！')

    except Exception as e:
        print(f'画像処理エラー: {e}')
        send_line_push(user_id, 'エラーが起きました。もう一度送ってね！')

def link_rich_menu(user_id, rich_menu_id):
    try:
        res = requests.post(
            f'https://api.line.me/v2/bot/user/{user_id}/richmenu/{rich_menu_id}',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'}
        )
        print(f'リッチメニュー切替: {res.status_code}')
    except Exception as e:
        print(f'リッチメニュー切替エラー: {e}')

def analyze_current_body_image(image_base64, context_info='', media_type='image/jpeg'):
    """初回登録時にアップロードされた『今の体型』の画像を解析する"""
    try:
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'claude-sonnet-4-5',
                'max_tokens': 500,
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image',
                                'source': {
                                    'type': 'base64',
                                    'media_type': media_type,
                                    'data': image_base64
                                }
                            },
                            {
                                'type': 'text',
                                'text': f"""この画像は、ユーザー本人の「今の体型」の写真です。
トレーニング・食事プランの作成に使うため、
以下の項目を分析してください。

参考情報：
{context_info}

分析項目：
・推定される体脂肪率の範囲（％、目安として）
・お腹周りの脂肪の付き方
・全体的な筋肉量（多い/普通/少ない）
・体型の特徴（1文）

写真が体型を判断できないもの（顔のみ、服を着ていて
体型が見えない、人物が写っていない等）の場合は
「体型を判断できる画像ではありません」とだけ答えてください。

出力は短く、以下の形式で。

体脂肪率目安：○〜○%
お腹周り：（1文）
筋肉量：（多い/普通/少ない）
特徴：（1文）

写真の人物を特定したり、実名を推測したりはしないでください。
あくまで体型の特徴のみを分析してください。"""
                            }
                        ]
                    }
                ]
            },
            timeout=30
        )
        print(f'現在体型画像解析ステータス: {response.status_code}')
        if response.status_code == 200:
            data = response.json()
            return data['content'][0]['text']
        else:
            print(f'現在体型画像解析失敗: {response.text[:300]}')
            return None
    except Exception as e:
        print(f'現在体型画像解析エラー: {e}')
        return None

def calc_bmr(gender, weight, height, age):
    """Mifflin-St Jeor式でBMRを計算する"""
    if gender == '女性':
        return (10 * weight) + (6.25 * height) - (5 * age) - 161
    else:
        return (10 * weight) + (6.25 * height) - (5 * age) + 5

def calc_tdee_multiplier(daily_steps):
    """歩数からTDEE倍率を決める"""
    mapping = {
        '2000歩未満': 1.2,
        '2000〜5000歩': 1.375,
        '5000〜8000歩': 1.55,
        '8000〜12000歩': 1.725,
        '12000歩以上': 1.9,
    }
    # 「わからない」「デスクワーク」「立ち仕事」「体を使う仕事」等は1.375をデフォルトに
    return mapping.get(daily_steps, 1.375)

def calc_training_day_addon(training_duration):
    """トレーニング日の追加消費カロリー"""
    mapping = {
        '30分': 200,
        '45分': 300,
        '60分': 400,
        '90分以上': 500,
    }
    return mapping.get(training_duration, 300)

def extract_body_fat_pct(body_fat_estimation_section, gender):
    """body_fat_estimation_sectionのテキストから「約○%」を抜き出す。
    見つからなければ日本人平均値を返す。"""
    import re
    match = re.search(r'約(\d+(?:\.\d+)?)\s*%', body_fat_estimation_section)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    # 画像解析結果の「○〜○%」形式にも対応
    match2 = re.search(r'(\d+(?:\.\d+)?)\s*[〜~]\s*(\d+(?:\.\d+)?)\s*%', body_fat_estimation_section)
    if match2:
        try:
            return (float(match2.group(1)) + float(match2.group(2))) / 2
        except ValueError:
            pass
    return 20.0 if gender != '女性' else 28.0

def determine_goal_body_fat_pct(goal, gender, current_bf_pct, goal_image_analysis_text):
    """目標体脂肪率を決定する"""
    if '画像' in (goal or '') or goal_image_analysis_text:
        # 画像解析結果から目標体脂肪率を抜き出す
        import re
        match = re.search(r'体脂肪率目安[：:]\s*(\d+(?:\.\d+)?)\s*[〜~]\s*(\d+(?:\.\d+)?)\s*%', goal_image_analysis_text or '')
        if match:
            return (float(match.group(1)) + float(match.group(2))) / 2
        return 15.0 if gender != '女性' else 23.0
    elif goal in ('絞りたい', '両方'):
        return 12.0 if gender != '女性' else 23.0
    elif goal == '増やしたい':
        return current_bf_pct
    else:
        return current_bf_pct

def calc_goal_weight_from_bf(current_weight, current_bf_pct, goal_bf_pct):
    """現在の体脂肪率と目標体脂肪率から目標体重を逆算する"""
    lean_mass = current_weight * (1 - current_bf_pct / 100)
    if goal_bf_pct >= 100:
        return current_weight
    return lean_mass / (1 - goal_bf_pct / 100)

def build_three_plans(total_kcal_deficit, gym_frequency='週4回'):
    """総kcalマイナスから3プランの日数を計算する。
    増量の場合（total_kcal_deficit<=0）はNoneを返す。
    集中プランの頻度表示は、登録時の回答（週4回/週5回/週6回）に合わせる。
    標準・ゆっくりは固定（週3回・週2回）のまま。"""
    if total_kcal_deficit is None or total_kcal_deficit <= 0:
        return None

    intensive_freq_map = {
        '週4回': '週4回',
        '週5回': '週5回',
        '週6回': '週6回',
    }
    intensive_freq = intensive_freq_map.get(gym_frequency, '週4回')

    plans = []
    for label, daily_kcal, freq in [
        ('集中プラン', 500, intensive_freq),
        ('標準プラン', 350, '週3回'),
        ('ゆっくりプラン', 200, '週2回'),
    ]:
        days = round(total_kcal_deficit / daily_kcal)
        plans.append({
            'label': label,
            'days': days,
            'daily_kcal': daily_kcal,
            'frequency': freq,
        })
    return plans

def build_bulk_plans(weight_to_gain_kg):
    """増やすべき体重(kg)から、3段階の増量プランの日数を計算する。
    増量が不要な場合（weight_to_gain_kg<=0）はNoneを返す。
    日数は「週○kgペース」から直接計算し、kcal幅は表示用の目安として
    そのまま使う（AIに作らせないための固定値）。"""
    if weight_to_gain_kg is None or weight_to_gain_kg <= 0:
        return None

    plans = []
    for label, weekly_kg, kcal_low, kcal_high in [
        ('ゆっくり増量プラン', 0.25, 200, 300),
        ('標準増量プラン', 0.5, 400, 600),
        ('しっかり増量プラン', 0.75, 600, 900),
    ]:
        days = round((weight_to_gain_kg / weekly_kg) * 7)
        plans.append({
            'label': label,
            'days': days,
            'weekly_kg': weekly_kg,
            'kcal_low': kcal_low,
            'kcal_high': kcal_high,
        })
    return plans

def calc_plan_data(data, body_fat_estimation_section, goal_image_analysis_text):
    """登録データからTDEE・目標体脂肪率・3プランを計算し、
    Difyに渡すための『計算済みサマリーテキスト』を作る"""
    try:
        gender = data.get('gender', '男性')
        age = float(data.get('age', 30))
        height = float(data.get('height', 170))
        weight = float(data.get('weight', 70))
        goal = data.get('goal', '')
        daily_steps = data.get('daily_steps', '')
        training_duration = data.get('training_duration', '60分')

        # ① BMR・TDEE計算
        bmr = calc_bmr(gender, weight, height, age)
        multiplier = calc_tdee_multiplier(daily_steps)
        tdee = bmr * multiplier
        training_day_kcal = tdee + calc_training_day_addon(training_duration)

        # ② 現在の体脂肪率
        current_bf_pct = extract_body_fat_pct(body_fat_estimation_section, gender)

        # ③ 目標体脂肪率
        goal_bf_pct = determine_goal_body_fat_pct(goal, gender, current_bf_pct, goal_image_analysis_text)

        # ④ 目標体重（画像目標の場合は逆算、それ以外はフォーム入力値を優先）
        goal_weight_input = data.get('goal_weight')
        if goal_weight_input and goal_weight_input != 'スキップ':
            try:
                goal_weight = float(goal_weight_input)
            except (ValueError, TypeError):
                goal_weight = calc_goal_weight_from_bf(weight, current_bf_pct, goal_bf_pct)
        else:
            goal_weight = calc_goal_weight_from_bf(weight, current_bf_pct, goal_bf_pct)
            # 筋肉量が現状より多いと判断される場合は1〜3kg上方調整
            if goal_image_analysis_text and ('多い' in goal_image_analysis_text or '厚み' in goal_image_analysis_text):
                goal_weight += 2

        # ⑤ 減らすべき脂肪量・総kcalマイナス（絞る方向）
        current_fat_mass = weight * (current_bf_pct / 100)
        goal_fat_mass = goal_weight * (goal_bf_pct / 100)
        fat_to_lose = current_fat_mass - goal_fat_mass
        total_kcal_deficit = fat_to_lose * 7700 if fat_to_lose > 0 else None

        # ⑤b 増やすべき体重（増やす方向。絞る方向の数値が出ない場合のみ使う）
        weight_to_gain = goal_weight - weight if total_kcal_deficit is None else None

        # ⑥ 筋肉量の差が大きいかどうかの簡易判定（画像目標の場合のみ）
        muscle_gap_large = False
        if goal_image_analysis_text and ('多い' in goal_image_analysis_text or '厚み' in goal_image_analysis_text or 'カット' in goal_image_analysis_text):
            muscle_gap_large = True

        # ⑦ 3プラン計算（絞る方向 or 増やす方向のどちらか一方だけ算出される）
        plans = build_three_plans(total_kcal_deficit, data.get('gym_frequency', '週4回'))
        bulk_plans = build_bulk_plans(weight_to_gain)

        if plans:
            plans_label = '3つのプラン（絞る方向。この日数・kcalマイナスをそのまま提示する）'
            plans_text = '\n'.join([
                f"・{p['label']}：約{p['days']}日（1日{p['daily_kcal']}kcalマイナス・{p['frequency']}）"
                for p in plans
            ])
            extra_line = f"減らすべき脂肪量：約{fat_to_lose:.1f}kg\n必要な総kcalマイナス：約{total_kcal_deficit:.0f}kcal"
        elif bulk_plans:
            plans_label = '3つのプラン（増やす方向。この日数・kcal余剰の幅をそのまま提示する）'
            plans_text = '\n'.join([
                f"・{p['label']}：約{p['days']}日（週{p['weekly_kg']}kg増量ペース・1日{p['kcal_low']}〜{p['kcal_high']}kcal余剰）"
                for p in bulk_plans
            ])
            extra_line = f"増やすべき体重：約{weight_to_gain:.1f}kg"
        else:
            plans_label = '3つのプラン'
            plans_text = '（このユーザーは現状維持に近いため、3プランの提示は不要。現状維持の方針を伝えること）'
            extra_line = '（増減なし、または僅少）'

        summary = f"""
【計算済みサマリー（この数値をそのまま使う。再計算しないこと）】
BMR：{bmr:.0f}kcal
TDEE（非トレ日）：{tdee:.0f}kcal
TDEE（トレ日）：{training_day_kcal:.0f}kcal
現在の体脂肪率（推定）：約{current_bf_pct:.1f}%
目標体脂肪率：約{goal_bf_pct:.1f}%
目標体重（計算済み）：約{goal_weight:.1f}kg
{extra_line}

{plans_label}：
{plans_text}

筋肉量の差が大きいと判定：{'はい（2フェーズ戦略を提案すること）' if muscle_gap_large else 'いいえ'}
"""
        return summary
    except Exception as e:
        print(f'プラン計算エラー: {e}')
        return ''

def process_registration(data):
    try:
        user_id = data.get('user_id')

        # --- 現在の体脂肪率推定（①ウエスト ②つまみ厚み ③写真 のいずれか1つ） ---
        bodyfat_method = data.get('bodyfat_method', 'スキップ')
        body_fat_estimation_section = ''

        if bodyfat_method == 'waist':
            waist = data.get('waist')
            if waist:
                try:
                    waist_f = float(waist)
                    weight_f = float(data.get('weight', 0))
                    gender = data.get('gender', '男性')

                    # YMCA式での体脂肪率推定
                    if gender == '女性':
                        fat_mass = (4.15 * waist_f) - (0.082 * weight_f) - 76.76
                    else:
                        fat_mass = (4.15 * waist_f) - (0.082 * weight_f) - 98.42

                    body_fat_pct = (fat_mass / weight_f) * 100 if weight_f > 0 else None

                    if body_fat_pct is not None and 3 <= body_fat_pct <= 50:
                        body_fat_estimation_section = f"""

【体脂肪率推定の参考情報（ウエストサイズから計算）】
ウエストサイズ：{waist}cm
YMCA式による計算結果：体脂肪率 約{body_fat_pct:.1f}%
（この数値をTDEE計算の参考にしてください。
ユーザーに伝える際は「目安として」という前置きをすること）"""
                    else:
                        body_fat_estimation_section = """

【体脂肪率推定の参考情報】
ウエストサイズからの計算が非現実的な値になったため、
日本人の平均値（男性20%・女性28%）を使ってください。"""
                except (ValueError, TypeError):
                    pass

        elif bodyfat_method == 'pinch':
            pinch = data.get('pinch_thickness')
            if pinch:
                gender = data.get('gender', '男性')

                # つまみ厚みの選択肢を皮下脂肪厚A（mm）に変換
                # つまむ動作自体が皮膚を二重に折った厚みのため
                # 上腕後部＋肩甲骨下部の合計値（A）として直接使う
                pinch_to_a = {
                    'つまめない': 10,
                    '第一関節程度': 20,
                    '第二関節程度': 35,
                    'しっかりつまめる': 55
                }
                a_total = pinch_to_a.get(pinch)

                if a_total is not None:
                    # 長嶺・鈴木の式による身体密度(D)
                    if gender == '女性':
                        body_density = 1.0897 - (0.00133 * a_total)
                    else:
                        body_density = 1.0913 - (0.00116 * a_total)

                    # Brozekらの式による体脂肪率
                    estimated_pct = (4.570 / body_density - 4.142) * 100

                    if 3 <= estimated_pct <= 50:
                        body_fat_estimation_section = f"""

【体脂肪率推定の参考情報（キャリパー法・長嶺鈴木式）】
お腹をつまんだ厚み：{pinch}
キャリパー法による計算結果：体脂肪率 約{estimated_pct:.1f}%
（つまみ厚みから皮下脂肪厚を簡易推定し、
長嶺・鈴木の式とBrozek式で算出した値。
TDEE計算の参考にしてください。
ユーザーに伝える際は「目安として」という前置きをすること）"""
                    else:
                        body_fat_estimation_section = """

【体脂肪率推定の参考情報】
つまみ厚みからの計算が非現実的な値になったため、
日本人の平均値（男性20%・女性28%）を使ってください。"""

        elif bodyfat_method == 'photo':
            current_image_b64 = data.get('current_image')
            current_image_type = data.get('current_image_type') or 'image/jpeg'
            if current_image_b64:
                print(f'現在の体型画像を解析中: {user_id}（形式：{current_image_type}）')
                context_for_image = f"性別：{data.get('gender')}、身長：{data.get('height')}cm、体重：{data.get('weight')}kg"
                result = analyze_current_body_image(current_image_b64, context_for_image, current_image_type)
                if result:
                    body_fat_estimation_section = f"""

【体脂肪率推定の参考情報（写真解析）】
{result}
（この画像解析結果をTDEE計算の参考にしてください。
あくまで推定値として扱い、ユーザーに伝える際は
「目安として」という前置きをすること）"""

        # --- 目標体型（テキスト or 画像） ---
        goal_image_analysis = ''
        goal_image_b64 = data.get('goal_image')
        goal_image_type = data.get('goal_image_type') or 'image/jpeg'
        if goal_image_b64:
            print(f'目標体型画像を解析中: {user_id}（形式：{goal_image_type}）')
            result = analyze_goal_image(goal_image_b64, goal_image_type)
            if result:
                goal_image_analysis = f"\n目標体型の画像解析結果：\n{result}\n（この解析結果を目標設定の参考にしてください。あくまで推定値として扱ってください）"

        # --- 計算済みサマリーをPython側で確定する ---
        calculated_summary = calc_plan_data(data, body_fat_estimation_section, goal_image_analysis)

        summary_message = f"""初回登録フォームに回答しました。以下の情報をもとにプランを提示してください。

性別：{data.get('gender')}
年齢：{data.get('age')}歳
身長：{data.get('height')}cm
体重：{data.get('weight')}kg
目標：{data.get('goal')}{goal_image_analysis}
目標体重：{data.get('goal_weight')}kg
期限：{data.get('deadline')}
開始日：{data.get('start_date')}
週の頻度：{data.get('gym_frequency')}
筋トレ歴：{data.get('training_history')}
1回の所要時間：{data.get('training_duration')}
トレーニング時間帯：{data.get('training_time')}
1日の歩数：{data.get('daily_steps')}{body_fat_estimation_section}
{calculated_summary}"""

        print(f'登録データをDifyに送信: {user_id}')
        dify_inputs = build_dify_inputs("初回登録")
        answer, new_conv_id = ask_dify(user_id, summary_message, '', dify_inputs)

        if answer:
            set_conversation_id(user_id, new_conv_id)
            update_user_state(user_id, {'status': 'awaiting_plan'})
            send_line_push(user_id, answer)
            main_menu_id = os.environ.get('MAIN_RICH_MENU_ID')
            if main_menu_id:
                link_rich_menu(user_id, main_menu_id)
        else:
            send_line_push(user_id, 'プランの作成に失敗しました。もう一度LINEで「はじめまして」と送ってみてね。')

    except Exception as e:
        print(f'登録処理エラー: {e}')

@app.route('/liff_register', methods=['POST'])
def liff_register():
    data = request.get_json()
    print(f'LIFF登録データ受信: {data}')

    if not data or not data.get('user_id'):
        return jsonify({'error': 'user_id is required'}), 400

    thread = threading.Thread(target=process_registration, args=(data,))
    thread.start()

    return jsonify({'status': 'ok'}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()

    if not verify_signature(body, signature):
        print('署名エラー')
        abort(400)

    data = json.loads(body)

    for event in data.get('events', []):
        if event['type'] == 'message':
            user_id = event['source']['userId']
            message = event['message']

            if message['type'] == 'text':
                user_message = message['text']
                print(f'テキスト受信: {user_message}')
                thread = threading.Thread(
                    target=process_message,
                    args=(user_id, user_message)
                )
                thread.start()

            elif message['type'] == 'image':
                message_id = message['id']
                print(f'画像受信: messageId={message_id}')
                thread = threading.Thread(
                    target=process_image_message,
                    args=(user_id, message_id)
                )
                thread.start()

    return 'OK'

@app.route('/', methods=['GET'])
def health():
    return 'OK'

# サーバー起動時に一度だけキャッシュを読み込む
load_state_cache()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
