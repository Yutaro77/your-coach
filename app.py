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
        print(f'LINE Push内容: {line_response.text}')
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

def process_registration(data):
    """LIFFフォームから送られたデータを処理する"""
    try:
        user_id = data.get('user_id')

        # フォームの回答を1つのメッセージにまとめてDifyに送る
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

        # 新しい会話として開始（conversation_idは空）
        answer, new_conv_id = ask_dify(user_id, summary_message, '')

        if answer:
            conversation_ids[user_id] = new_conv_id
            send_line_push(user_id, answer)
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

    # 別スレッドで処理（フォームをすぐ完了画面に進めるため）
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
        if event['type'] == 'message' and event['message']['type'] == 'text':
            user_id = event['source']['userId']
            user_message = event['message']['text']

            print(f'メッセージ受信: {user_message}')

            thread = threading.Thread(
                target=process_message,
                args=(user_id, user_message)
            )
            thread.start()

    return 'OK'

@app.route('/', methods=['GET'])
def health():
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
