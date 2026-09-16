from flask import Flask, request, jsonify
import requests
import json

app = Flask(__name__)

SENSE_TOKEN = "YOUR_REAL_TOKEN"  # 换成你的真实Token

@app.route('/chat', methods=['POST'])
def chat():
    try:
        # 1. 接收Agent或Harness传过来的标准数据
        data = request.get_json()
        
        # 兼容两种传法：
        # (1) 简单插件传 message
        # (2) 标准Agent传 messages 列表
        if 'messages' in data:
            messages = data['messages']
        elif 'message' in data:
            messages = [{"role": "user", "content": data['message']}]
        else:
            return jsonify({"code": 400, "error": "缺少 message 或 messages 字段"})

        # 2. 直接使用传入的 messages，不加任何额外提示词
        # （把这行删掉，或者注释掉）
        # final_messages = [{"role": "system", "content": "你是一个严谨的人工智能助手。"}] + messages
        ## 如果传入的 messages 里没有 system 角色，才主动加上默认提示词
        has_system = any(msg.get('role') == 'system' for msg in messages)
        if not has_system:
            messages = [{"role": "system", "content": "你是一个严谨的人工智能助手。"}] + messages
        payload = {
            "model": "senseaudio-s2",
            "messages":messages,
            "temperature": data.get('temperature', 0.7),  # 允许Agent动态调整温度
            "stream": False
        }
        
        headers = {
            "Authorization": f"Bearer {SENSE_TOKEN}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(
            "https://api.senseaudio.cn/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=60
        )
        
        result = response.json()
        ai_reply = result['choices'][0]['message']['content']
        
        # 返回标准格式（OpenAI风格），方便Agent解析
        return jsonify({
            "choices": [{
                "message": {"role": "assistant", "content": ai_reply}
            }]
        })
        
    except Exception as e:
        return jsonify({"code": 500, "error": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)