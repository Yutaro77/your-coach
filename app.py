from flask import Flask, request, abort
import json
import hmac
import hashlib
import base64
import requests
import os
import threading

app = Flask(__name__)

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

def process_message(user_id, user_message):
    try:
        conversation_id = conversation_ids.get(user_id, '')

        print(f'Difyに送信開始: {user_message}')

        dify_response = requests.post(
            DIFY_API_URL,
            headers={
                'Authorization': f'Bearer {DIFY_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'inputs': {},
                'query': user_message,
                'response_mode': 'blocking',
                'conversation_id': conversation_id,
                'user': user_id
            },
            timeout=30
        )

        print(f'Difyステータス: {dify_response.status_code}')
        print(f'Dify返答: {dify_response.text[:200]}')

        if dify_response.status_code == 200:
            dify_data = dify_response.json()
            ai_message = dify_data.get('answer', 'すみません、もう一度送ってね！')
            conversation_ids[user_id] = dify_data.get('conversation_id', '')
        else:
            ai_message = 'すみません、もう一度送ってね！'

        # Push APIで送信（reply tokenが不要）
        line_response = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers={
                'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
                'Content-Type': 'application/json'
            },
            json={
                'to': user_id,
                'messages': [{'type': 'text', 'text': ai_message}]
            }
        )
        print(f'LINE Pushステータス: {line_response.status_code}')
        print(f'LINE Push内容: {line_response.text}')

    except Exception as e:
        print(f'エラー発生: {e}')

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()

    if not verify_signature(body, signature):
        print('署名エラー')
        abort(400)

    data = json.loads(body)
    print(f'受信データあり')

    for event in data.get('events', []):
        if event['type'] == 'message' and event['message']['type'] == 'text':
            user_id = event['source']['userId']
            user_message = event['message']['text']

            print(f'メッセージ受信: {user_message}')

            # 別スレッドで処理（タイムアウト回避）
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
