"""ESN 聊天伺服器：用這台電腦當伺服器，讓大家的訊息可以互傳。

只用 Python 內建的東西，不用另外安裝套件。

啟動：
    py server.py            （Windows）
    python3 server.py       （Mac / Linux）

啟動後：
    - 這台電腦：打開 http://localhost:8800/
    - 同一個 Wi-Fi 的手機：打開畫面上印出來的 http://<這台電腦的 IP>:8800/
    - 不在同一個網路的人：需要另外開「通道」（例如 Cloudflare Tunnel），把網址給他們

訊息存在 C:\ESNData\chat-data.json（可用 ESN_DATA_DIR 改）；關掉伺服器再開，訊息還在。
平常用「啟動聊天伺服器.bat」雙擊啟動就好（只開給這台電腦 + Tailscale Funnel）。
注意：目前沒有帳號密碼，任何連得到這台電腦的人都能選任何身分、看所有聊天室。
"""

import json
import os
import queue
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get('ESN_PORT', '8800'))
# 0.0.0.0 = 同網路的手機也連得到；設成 127.0.0.1 就只有這台電腦能連
HOST = os.environ.get('ESN_HOST', '0.0.0.0')
ROOT = os.path.dirname(os.path.abspath(__file__))
# 聊天資料的資料夾：不要放在 OneDrive 裡（每次有訊息就寫檔，會一直觸發同步）。
# Windows 預設 C:\ESNData，其他系統預設跟程式同一個資料夾；可用 ESN_DATA_DIR 改。
DATA_DIR = os.environ.get('ESN_DATA_DIR') or (r'C:\ESNData' if os.name == 'nt' else ROOT)
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, 'chat-data.json')

# 跟 Group.html 的 MEMBERS 一樣
MEMBER_IDS = {'fan', 'chen', 'zhang', 'liu', 'si'}
MAX_TEXT = 300
MAX_PER_ROOM = 500

# 允許哪些網站連這台伺服器（GitHub 網站 + 本機測試）。要加別的網址就加在這裡，
# 或用環境變數 ESN_ORIGINS="https://a.com,https://b.com"
ALLOWED_ORIGINS = {
    'https://liug1217.github.io',
    'http://localhost:8765',
    'http://127.0.0.1:8765',
} | {o.strip() for o in os.environ.get('ESN_ORIGINS', '').split(',') if o.strip()}

# 只開放網站本身的檔案，不讓別人讀到 server.py、聊天資料或 .git
PUBLIC_FILES = {'/Group.html'}
PUBLIC_DIRS = ('/assets/',)

lock = threading.Lock()
listeners = set()


def load_rooms():
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # 第一次啟動：群組聊天室放幾則歡迎訊息（跟網頁上的範例一樣）
        return {
            'group': [
                {'id': 'seed-1', 'from': 'fan', 'text': '歡迎加入 ★Bi_藍冰團★！', 'time': '20:01', 'ts': 0},
                {'id': 'seed-2', 'from': 'chen', 'text': '週六團戰練習大家記得上線', 'time': '20:03', 'ts': 0},
                {'id': 'seed-3', 'from': 'si', 'text': '收到～', 'time': '20:05', 'ts': 0},
            ],
        }


rooms = load_rooms()


def save_rooms():
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rooms, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def valid_room(room):
    if room == 'group':
        return True
    if not room.startswith('dm:'):
        return False
    pair = room[3:].split('-')
    return len(pair) == 2 and pair[0] != pair[1] and set(pair) <= MEMBER_IDS and pair == sorted(pair)


def broadcast(payload):
    data = json.dumps(payload, ensure_ascii=False)
    for q in list(listeners):
        q.put(data)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        # 只印 API 的紀錄，圖片之類的不印
        if self.path.startswith('/api/messages') and self.command == 'POST':
            super().log_message(fmt, *args)

    # 讓 GitHub 網站（不同網址）也能呼叫這台伺服器的 API
    def send_cors(self):
        origin = self.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')

    def do_OPTIONS(self):
        if not urlparse(self.path).path.startswith('/api/'):
            return self.send_error(404)
        self.send_response(204)
        self.send_cors()
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # Chrome「私人網路存取」：用裝了 Tailscale 的裝置打開網頁時才不會被擋
        if self.headers.get('Access-Control-Request-Private-Network') == 'true':
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Max-Age', '600')
        self.end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/ping':
            return self.send_json(200, {'ok': True, 'server': 'esn'})
        if path == '/api/messages':
            with lock:
                return self.send_json(200, rooms)
        if path == '/api/events':
            return self.stream_events()
        if path in ('/', '/index.html'):
            self.send_response(302)
            self.send_header('Location', '/Group.html')
            self.end_headers()
            return
        if path in PUBLIC_FILES or path.startswith(PUBLIC_DIRS):
            if '..' in path:
                return self.send_error(404)
            return super().do_GET()
        return self.send_error(404)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in PUBLIC_FILES or path.startswith(PUBLIC_DIRS):
            return super().do_HEAD()
        return self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != '/api/messages':
            return self.send_error(404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 4096:
                return self.send_json(413, {'error': 'too large'})
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'bad json'})

        room = str(data.get('room', ''))
        sender = str(data.get('from', ''))
        text = str(data.get('text', '')).strip()
        if not valid_room(room) or sender not in MEMBER_IDS or not text or len(text) > MAX_TEXT:
            return self.send_json(400, {'error': 'invalid message'})
        if room.startswith('dm:') and sender not in room[3:].split('-'):
            return self.send_json(403, {'error': 'not in this room'})

        now = time.time()
        msg = {
            'id': f'{int(now * 1000)}-{sender}',
            'from': sender,
            'text': text,
            'time': time.strftime('%H:%M', time.localtime(now)),
            'ts': int(now * 1000),
        }
        with lock:
            rooms.setdefault(room, []).append(msg)
            rooms[room] = rooms[room][-MAX_PER_ROOM:]
            save_rooms()
        broadcast({'room': room, 'msg': msg})
        return self.send_json(200, msg)

    # Server-Sent Events：新訊息即時推給每支連著的手機
    def stream_events(self):
        self.send_response(200)
        self.send_cors()
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        q = queue.Queue()
        listeners.add(q)
        try:
            self.wfile.write(b': connected\n\n')
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f'data: {data}\n\n'.encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')  # 保持連線
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            listeners.discard(q)


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print('ESN 聊天伺服器已啟動')
    print(f'  這台電腦：      http://localhost:{PORT}/')
    print(f'  同 Wi-Fi 手機： http://{lan_ip()}:{PORT}/')
    print('  按 Ctrl+C 關閉')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已關閉')


if __name__ == '__main__':
    main()
