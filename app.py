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
DIFY_API_URL = 'https://api.dify.ai/v1/chat-messages'

conversation_ids = {}

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
        conversation_id = conversation_ids.get(user_id, '')
        answer, new_conv_id = ask_dify(user_id, user_message, conversation_id)
        if answer:
            conversation_ids[user_id] = new_conv_id
            send_line_push(user_id, answer)
        else:
            send_line_push(user_id, 'すみません、もう一度送ってね！')
    except Exception as e:
        print(f'エラー発生: {e}')

def get_line_image(message_id):
    """LINEから画像データを取得してbase64に変換する"""
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

def analyze_food_image(image_base64, user_id):
    """Claude APIで食事写真を分析する"""
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
絵文字は使わず、親しみやすい口調で。

---
【食事内容】
（何が写っているか簡潔に）

【栄養素の目安】
カロリー：約○kcal
タンパク質：約○g
脂質：約○g
炭水化物：約○g

【コーチからひとこと】
（筋トレ目線で短く一言。褒めるか、改善点を優しく）
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
    """画像メッセージを処理する"""
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

def process_registration(data):
    try:
        user_id = data.get('user_id')
        summary_message = f"""初回登録フォームに回答しました。以下の情報をもとにプランを提示してください。

性別：{data.get('gender')}
年齢：{data.get('age')}歳
身長：{data.get('height')}cm
体重：{data.get('weight')}kg
目標：{data.get('goal')}
目標体重：{data.get('goal_weight')}kg
期限：{data.get('deadline')}
開始日：{data.get('start_date')}
週の頻度：{data.get('gym_frequency')}
筋トレ歴：{data.get('training_history')}
1回の所要時間：{data.get('training_duration')}
トレーニング時間帯：{data.get('training_time')}
1日の歩数：{data.get('daily_steps')}"""

        print(f'登録データをDifyに送信: {user_id}')
        answer, new_conv_id = ask_dify(user_id, summary_message, '')

        if answer:
            conversation_ids[user_id] = new_conv_id
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

            # テキストメッセージ
            if message['type'] == 'text':
                user_message = message['text']
                print(f'テキスト受信: {user_message}')
                thread = threading.Thread(
                    target=process_message,
                    args=(user_id, user_message)
                )
                thread.start()

            # 画像メッセージ
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
