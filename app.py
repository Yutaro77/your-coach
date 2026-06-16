from flask import Flask, request, abort
import json
import hmac
import hashlib
import base64
import requests
import os

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = 'https://api.dify.ai/v1/chat-messages'

# ユーザーごとの会話IDを保存
conversation_ids = {}

def verify_signature(body, signature):
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    return base64.b64encode(hash).decode('utf-8') == signature

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    data = json.loads(body)

    for event in data.get('events', []):
        if event['type'] == 'message' and event['message']['type'] == 'text':
            user_id = event['source']['userId']
            user_message = event['message']['text']
            reply_token = event['replyToken']

            # ユーザーの会話IDを取得
            conversation_id = conversation_ids.get(user_id, '')

            # DifyのAPIを呼び出す
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
                }
            )

            if dify_response.status_code == 200:
                dify_data = dify_response.json()
                ai_message = dify_data.get('answer', 'すみません、うまく答えられませんでした。')
                conversation_ids[user_id] = dify_data.get('conversation_id', '')
            else:
                ai_message = 'すみません、うまく答えられませんでした。'

            # LINEに返信する
            requests.post(
                'https://api.line.me/v2/bot/message/reply',
                headers={
                    'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
                    'Content-Type': 'application/json'
                },
                json={
                    'replyToken': reply_token,
                    'messages': [
                        {
                            'type': 'text',
                            'text': ai_message
                        }
                    ]
                }
            )

    return 'OK'

@app.route('/', methods=['GET'])
def health():
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
