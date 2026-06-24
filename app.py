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
conversation_ids = {}
cache_loaded = False
cache_lock = threading.Lock()

def load_conversation_cache():
    """起動時にスプレッドシートから会話IDを読み込む"""
    global conversation_ids, cache_loaded
    try:
        res = requests.get(SHEET_API_URL, timeout=15)
        if res.status_code == 200:
            data = res.json()
            with cache_lock:
                conversation_ids.update(data)
            print(f'会話キャッシュ読み込み完了: {len(data)}件')
        else:
            print(f'会話キャッシュ読み込み失敗: {res.status_code}')
    except Exception as e:
        print(f'会話キャッシュ読み込みエラー: {e}')
    cache_loaded = True

def save_conversation_id(user_id, conversation_id):
    """スプレッドシートに会話IDを保存する（非同期）"""
    def _save():
        try:
            res = requests.post(
                SHEET_API_URL,
                json={'user_id': user_id, 'conversation_id': conversation_id},
                timeout=15
            )
            print(f'会話ID保存: {res.status_code}')
        except Exception as e:
            print(f'会話ID保存エラー: {e}')
    threading.Thread(target=_save).start()

def get_conversation_id(user_id):
    """メモリにあれば使う。なければスプレッドシートを直接確認する"""
    with cache_lock:
        if user_id in conversation_ids:
            return conversation_ids[user_id]
    # メモリにない場合はシートを直接見に行く（再起動直後など）
    try:
        res = requests.get(SHEET_API_URL, timeout=15)
        if res.status_code == 200:
            data = res.json()
            with cache_lock:
                conversation_ids.update(data)
            return data.get(user_id, '')
    except Exception as e:
        print(f'個別会話ID取得エラー: {e}')
    return ''

def set_conversation_id(user_id, conversation_id):
    with cache_lock:
        conversation_ids[user_id] = conversation_id
    save_conversation_id(user_id, conversation_id)

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

def ask_dify(user_id, message, conversation_id=''):
    dify_response = requests.post(
        DIFY_API_URL,
        headers={
            'Authorization': f'Bearer {DIFY_API_KEY}',
            'Content-Type': 'application/json'
        },
        json={
            'inputs': {},
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

def process_message(user_id, user_message):
    try:
        from datetime import datetime
        import pytz
        jst = pytz.timezone('Asia/Tokyo')
        now = datetime.now(jst)
        weekdays = ['月', '火', '水', '木', '金', '土', '日']
        today_str = f"{now.year}年{now.month}月{now.day}日（{weekdays[now.weekday()]}）"

        # 起床・トレーニング開始・就寝ボタンなど、日付に関わる操作の時は
        # 実際の日付情報をメッセージに付与する
        message_with_date = f"[今日の日付：{today_str}]\n{user_message}"

        conversation_id = get_conversation_id(user_id)
        answer, new_conv_id = ask_dify(user_id, message_with_date, conversation_id)
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
1日の歩数：{data.get('daily_steps')}{body_fat_estimation_section}"""

        print(f'登録データをDifyに送信: {user_id}')
        answer, new_conv_id = ask_dify(user_id, summary_message, '')

        if answer:
            set_conversation_id(user_id, new_conv_id)
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
load_conversation_cache()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
