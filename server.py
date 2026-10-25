"""ESN 聊天伺服器：用這台電腦當伺服器，讓大家的訊息可以互傳。

只用 Python 內建的東西，不用另外安裝套件。

啟動：
    py server.py            （Windows）
    python3 server.py       （Mac / Linux）

啟動後：
    - 這台電腦：打開 http://localhost:8800/
    - 同一個 Wi-Fi 的手機：打開畫面上印出來的 http://<這台電腦的 IP>:8800/
    - 不在同一個網路的人：需要另外開「通道」（例如 Cloudflare Tunnel），把網址給他們

訊息存在 C:\ESNData\chat-data.json，帳號在 accounts.json、群組在 groups.json（可用 ESN_DATA_DIR 改）；
每次都先寫到暫存檔再換過去，關掉伺服器再開，訊息、帳號都還在。
平常用「啟動聊天伺服器.bat」雙擊啟動就好（只開給這台電腦 + Tailscale Funnel）。
帳號：註冊時設密碼，登入後拿到一組登入碼（token），之後每個請求都帶著它；別的裝置用 ID + 密碼登入。
原本的 4 個成員是內建帳號，還沒設密碼前照舊點頭像就能登入（設了密碼就要輸入密碼）。
每個人只收得到自己所在聊天室的訊息、通話和即時更新。
"""

import hashlib
import json
import os
import queue
import secrets
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get('ESN_PORT', '8800'))
# 0.0.0.0 = 同網路的手機也連得到；設成 127.0.0.1 就只有這台電腦能連
HOST = os.environ.get('ESN_HOST', '0.0.0.0')
ROOT = os.path.dirname(os.path.abspath(__file__))
# 聊天資料的資料夾：不要放在 OneDrive 裡（每次有訊息就寫檔，會一直觸發同步）。
# Windows 預設 C:\ESNData，其他系統預設跟程式同一個資料夾；可用 ESN_DATA_DIR 改。
DATA_DIR = os.environ.get('ESN_DATA_DIR') or (r'C:\ESNData' if os.name == 'nt' else ROOT)
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, 'chat-data.json')
# 上傳的圖片（照片、塗鴉）放這裡；用隨機檔名存，再用 /uploads/<檔名> 讀回來
UPLOADS_DIR = os.path.join(DATA_DIR, 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)

# 原本的 4 個成員（跟 Group.html 的 MEMBERS 一樣）：內建帳號，id 不變，群組聊天室 'group' 就是他們 4 個
LEGACY = {
    'fan': ('帆～', 'Bi_wuda0823', 'assets/member-5.jpg'),
    'zhang': ('張 O 寧', 'Bi_VIP1124', 'assets/member-2.jpg'),
    'liu': ('劉 O 佑', 'Bi_liug1217', 'assets/member-3.jpg'),
    'si': ('劉思妤', 'Bi_Robbyg309bbb', 'assets/member-4.jpg'),
}
LEGACY_IDS = set(LEGACY)
ACCOUNTS_FILE = os.path.join(DATA_DIR, 'accounts.json')
GROUPS_FILE = os.path.join(DATA_DIR, 'groups.json')
# 使用者看得到、可以改的 ID（登入、加好友用）；內部編號（uid）永遠不變，改 ID 不影響聊天紀錄
HANDLE_RE = re.compile(r'[A-Za-z0-9_.]{3,20}')
UPLOAD_RE = re.compile(r'[0-9a-f]{32}\.(png|jpg|gif|webp)')
MAX_GROUP = 50
MAX_TEXT = 300
MAX_PER_ROOM = 500
# 訊息種類：純文字，或圖片 / 塗鴉（上傳的檔）、貼圖 / GIF（網頁內建的，只存 id）
MSG_KINDS = {'text', 'image', 'doodle', 'sticker', 'gif'}
MAX_REF = 80  # 貼圖 / GIF 的 id、上傳檔名的長度上限
# 上傳圖片：最大 8MB，只收這幾種圖（用檔頭判斷，不信任副檔名）
MAX_UPLOAD = 8 * 1024 * 1024
IMAGE_TYPES = (
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpg'),
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
)


def sniff_image(head):
    """看檔頭認圖片格式；WebP 要額外看 RIFF....WEBP。認不出來回 None。"""
    for magic, ext in IMAGE_TYPES:
        if head.startswith(magic):
            return ext
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


# 通話（WebRTC）的訊號：只轉傳給對方，不存檔。g 開頭的是群組通話
SIGNAL_TYPES = {'ring', 'accept', 'decline', 'busy', 'end', 'offer', 'answer', 'ice',
                'gring', 'gjoin', 'gleave', 'ghere'}
# 這幾種的對象可以是 'group'（整組一起收到），其他一律指定某個人
GROUP_SIGNALS = {'gring', 'gjoin', 'gleave'}
MAX_SIGNAL = 65536

# 允許哪些網站連這台伺服器（GitHub 網站 + 本機測試）。要加別的網址就加在這裡，
# 或用環境變數 ESN_ORIGINS="https://a.com,https://b.com"
ALLOWED_ORIGINS = {
    'https://liug1217.github.io',
    'http://localhost:8765',
    'http://127.0.0.1:8765',
} | {o.strip() for o in os.environ.get('ESN_ORIGINS', '').split(',') if o.strip()}

ACCOUNT_ROUTES = {'/api/register', '/api/login', '/api/logout', '/api/account', '/api/friends', '/api/groups'}

# 只開放網站本身的檔案，不讓別人讀到 server.py、聊天資料或 .git
PUBLIC_FILES = {'/Group.html', '/sw.js', '/manifest.json'}
PUBLIC_DIRS = ('/assets/',)

lock = threading.Lock()
listeners = {}  # 每條即時通道 → 是誰（uid）
# 誰現在開著網頁（連著即時通道）：開著的人看得到畫面，就不用再推播通知
online = Counter()
# App 切到背景時網頁會送「我離開了」（/api/away）：連線還沒斷乾淨也算不在線上，照樣推播
away = set()


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    """先寫暫存檔、確定寫進硬碟，再換掉原本的檔：寫到一半當機或關掉，原本的資料也不會壞"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


rooms = load_json(DATA_FILE, {})


def save_rooms():
    save_json(DATA_FILE, rooms)


# ---------- 帳號 ----------
accounts = load_json(ACCOUNTS_FILE, {})
for _uid, (_name, _handle, _avatar) in LEGACY.items():
    accounts.setdefault(_uid, {'uid': _uid, 'handle': _handle, 'name': _name, 'avatar': _avatar, 'legacy': True,
                               'salt': '', 'hash': '', 'tokens': [], 'friends': [], 'created': 0})
groups = load_json(GROUPS_FILE, {})


def save_accounts():
    save_json(ACCOUNTS_FILE, accounts)


def save_groups():
    save_json(GROUPS_FILE, groups)


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 120000).hex()


def set_password(acc, password):
    acc['salt'] = secrets.token_hex(16)
    acc['hash'] = hash_password(password, acc['salt'])


def check_password(acc, password):
    return bool(acc.get('hash')) and secrets.compare_digest(hash_password(password, acc['salt']), acc['hash'])


def new_token(uid):
    token = secrets.token_urlsafe(24)
    acc = accounts[uid]
    acc['tokens'] = (acc.get('tokens', []) + [sha(token)])[-10:]  # 最多 10 個裝置同時登入
    save_accounts()
    return token


def uid_by_token(token):
    if not token:
        return None
    h = sha(token)
    for uid, acc in accounts.items():
        if h in acc.get('tokens', []):
            return uid
    return None


def uid_by_handle(handle):
    handle = handle.strip().lower()
    for uid, acc in accounts.items():
        if acc['handle'].lower() == handle:
            return uid
    return None


def identify(data):
    """這個請求是誰：先看登入碼；沒帶登入碼的話，只有還沒設密碼的原本 4 個成員可以直接報名字（舊版網頁）"""
    uid = uid_by_token(str(data.get('token') or ''))
    if uid:
        return uid
    claimed = str(data.get('from') or data.get('me') or data.get('member') or '')
    if claimed in LEGACY_IDS and not accounts[claimed].get('hash'):
        return claimed
    return None


def public_user(uid):
    acc = accounts[uid]
    return {'id': uid, 'handle': acc['handle'], 'name': acc['name'], 'avatar': acc.get('avatar', '')}


def name_of(uid):
    return accounts.get(uid, {}).get('name', '成員')


def room_members(room):
    if room == 'group':
        return set(LEGACY_IDS)
    if room.startswith('dm:'):
        pair = room[3:].split('-')
        if len(pair) == 2 and pair[0] != pair[1] and pair == sorted(pair) and all(p in accounts for p in pair):
            return set(pair)
        return set()
    if room.startswith('g:') and room[2:] in groups:
        return set(groups[room[2:]]['members'])
    return set()


def room_title(room):
    if room == 'group':
        return '★Bi_藍冰團★'
    if room.startswith('g:') and room[2:] in groups:
        return groups[room[2:]]['name']
    return ''


def broadcast(payload, to=None):
    """即時推給連著的人；to 是一組 uid（只推給這些人），None = 全部"""
    data = json.dumps(payload, ensure_ascii=False)
    for q, uid in list(listeners.items()):
        if to is None or uid in to:
            q.put(data)


# ---------- 推播通知（Web Push，App 關著也收得到） ----------
# 加密和簽章交給同一個資料夾的 push.js（Node 內建 crypto）。VAPID 金鑰第一次用時產生，
# 跟每個人的訂閱一起存在 DATA_DIR/push.json；換掉金鑰的話所有人的訂閱都會失效。
PUSH_FILE = os.path.join(DATA_DIR, 'push.json')
PUSH_SCRIPT = os.path.join(ROOT, 'push.js')
PUSH_SUBJECT = 'https://liug1217.github.io/Group/Group.html'
NODE = shutil.which('node')
push_lock = threading.Lock()
# 來電時對方沒開網頁：記住 30 秒，對方點通知打開網頁一連上就補送響鈴
pending_rings = {}


def load_push():
    try:
        with open(PUSH_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault('vapid', None)
    data.setdefault('subs', {})
    return data


push_data = load_push()


def save_push():
    tmp = PUSH_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(push_data, f, ensure_ascii=False)
    os.replace(tmp, PUSH_FILE)


def run_push_script(mode, payload=None):
    if not NODE:
        raise RuntimeError('node not found')
    result = subprocess.run(
        [NODE, PUSH_SCRIPT, mode],
        input=json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else b'',
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[:300])
    return json.loads(result.stdout.decode('utf-8'))


def get_vapid():
    with push_lock:
        if not push_data['vapid']:
            push_data['vapid'] = run_push_script('keys')
            save_push()
        return push_data['vapid']


def valid_subscription(sub):
    if not isinstance(sub, dict):
        return False
    keys = sub.get('keys') or {}
    return (isinstance(sub.get('endpoint'), str) and sub['endpoint'].startswith('https://')
            and len(sub['endpoint']) < 1000 and isinstance(keys.get('p256dh'), str) and isinstance(keys.get('auth'), str))


def add_subscription(member, sub):
    clean = {'endpoint': sub['endpoint'], 'keys': {'p256dh': sub['keys']['p256dh'], 'auth': sub['keys']['auth']}}
    with push_lock:
        # 同一支手機換成員登入：訂閱跟著換人
        for subs in push_data['subs'].values():
            subs[:] = [s for s in subs if s['endpoint'] != clean['endpoint']]
        push_data['subs'].setdefault(member, []).append(clean)
        save_push()


def remove_subscription(endpoint):
    with push_lock:
        for subs in push_data['subs'].values():
            subs[:] = [s for s in subs if s['endpoint'] != endpoint]
        save_push()


def push_to(members, message, urgency='normal', skip_online=True):
    """推播給這些成員（開著網頁的人跳過）。在背景跑，不會拖慢回應。"""
    targets = [m for m in members if not (skip_online and online[m] > 0 and m not in away)]
    with push_lock:
        subs = [s for m in targets for s in push_data['subs'].get(m, [])]
    if not subs or not NODE:
        return

    def work():
        try:
            out = run_push_script('send', {
                'vapid': get_vapid(),
                'subject': PUSH_SUBJECT,
                'subs': subs,
                'message': message,
                'urgency': urgency,
            })
            for r in out.get('results', []):
                if r.get('status') in (404, 410):
                    remove_subscription(r['endpoint'])
                elif not (200 <= int(r.get('status') or 0) < 300):
                    print('推播失敗', r.get('status'), r.get('error', ''))
        except Exception as error:  # 推播失敗不影響聊天
            print('推播失敗', error)

    threading.Thread(target=work, daemon=True).start()


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
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if path == '/api/messages':
            # 只給這個人所在的聊天室
            uid = identify(query)
            with lock:
                mine = {r: msgs for r, msgs in rooms.items() if uid and uid in room_members(r)}
            return self.send_json(200, mine)
        if path == '/api/me':
            uid = identify(query)
            if not uid:
                return self.send_json(401, {'error': 'login required'})
            return self.send_json(200, self.me_payload(uid))
        if path == '/api/events':
            return self.stream_events()
        if path == '/api/push':
            try:
                return self.send_json(200, {'publicKey': get_vapid()['publicKey']})
            except Exception:
                return self.send_json(503, {'error': 'push unavailable'})
        if path in ('/', '/index.html'):
            self.send_response(302)
            self.send_header('Location', '/Group.html')
            self.end_headers()
            return
        if path.startswith('/uploads/'):
            return self.serve_upload(path[len('/uploads/'):])
        if path in PUBLIC_FILES or path.startswith(PUBLIC_DIRS):
            if '..' in path:
                return self.send_error(404)
            return super().do_GET()
        return self.send_error(404)

    # 讀回上傳的圖片：檔名只能是我們自己產生的「亂碼.副檔名」，擋掉路徑跳脫
    def serve_upload(self, name):
        if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|gif|webp)', name or ''):
            return self.send_error(404)
        full = os.path.join(UPLOADS_DIR, name)
        try:
            with open(full, 'rb') as f:
                body = f.read()
        except OSError:
            return self.send_error(404)
        ext = name.rsplit('.', 1)[1]
        ctype = {'png': 'image/png', 'jpg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp'}[ext]
        self.send_response(200)
        self.send_cors()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'public, max-age=31536000, immutable')  # 檔名不重複，可以永久快取
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path.startswith('/uploads/') or path in PUBLIC_FILES or path.startswith(PUBLIC_DIRS):
            if path.startswith('/uploads/'):
                return self.serve_upload(path[len('/uploads/'):])
            return super().do_HEAD()
        return self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/signal':
            return self.handle_signal()
        if path == '/api/push':
            return self.handle_push()
        if path == '/api/away':
            return self.handle_away()
        if path == '/api/upload':
            return self.handle_upload()
        if path in ACCOUNT_ROUTES:
            return self.handle_account(path)
        if path != '/api/messages':
            return self.send_error(404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 8192:  # 圖片是另外上傳的，這裡的訊息只帶很短的文字或參照
                return self.send_json(413, {'error': 'too large'})
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'bad json'})

        room = str(data.get('room', ''))
        sender = identify(data)
        kind = str(data.get('kind', 'text')) or 'text'
        if not sender:
            return self.send_json(401, {'error': 'login required'})
        if kind not in MSG_KINDS:
            return self.send_json(400, {'error': 'invalid message'})
        if sender not in room_members(room):
            return self.send_json(403, {'error': 'not in this room'})

        now = time.time()
        msg = {
            'id': f'{int(now * 1000)}-{sender}',
            'from': sender,
            'kind': kind,
            'time': time.strftime('%H:%M', time.localtime(now)),
            'ts': int(now * 1000),
        }
        # 每種訊息帶的欄位不一樣，各自檢查；推播通知的內容也跟著換
        if kind == 'text':
            text = str(data.get('text', '')).strip()
            if not text or len(text) > MAX_TEXT:
                return self.send_json(400, {'error': 'invalid message'})
            msg['text'] = text
            preview = text if len(text) <= 120 else text[:118] + '…'
        elif kind in ('image', 'doodle'):
            src = str(data.get('src', ''))
            import re
            if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|gif|webp)', src) or \
                    not os.path.exists(os.path.join(UPLOADS_DIR, src)):
                return self.send_json(400, {'error': 'invalid image'})
            msg['src'] = src
            preview = '傳了一張塗鴉' if kind == 'doodle' else '傳了一張照片'
        else:  # sticker / gif：只是網頁內建素材的 id
            ref = str(data.get('ref', ''))
            import re
            if not re.fullmatch(r'[0-9a-zA-Z_-]{1,%d}' % MAX_REF, ref):
                return self.send_json(400, {'error': 'invalid ref'})
            msg['ref'] = ref
            preview = '傳了一張貼圖' if kind == 'sticker' else '傳了一個 GIF'

        # 回覆某一則訊息：只收對方給的訊息 id，引用的內容（誰、前 60 個字）由伺服器自己從這個聊天室找出來
        reply_id = str(data.get('reply', ''))
        if reply_id:
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', reply_id):
                return self.send_json(400, {'error': 'invalid reply'})
            with lock:
                original = next((m for m in rooms.get(room, []) if m.get('id') == reply_id), None)
            if original:
                if original.get('kind', 'text') == 'text':
                    snippet = str(original.get('text', ''))
                    snippet = snippet if len(snippet) <= 60 else snippet[:58] + '…'
                else:
                    snippet = {'image': '照片', 'doodle': '塗鴉', 'sticker': '貼圖', 'gif': 'GIF'}.get(original.get('kind'), '訊息')
                msg['reply'] = {'id': reply_id, 'from': original.get('from', ''), 'text': snippet}

        with lock:
            rooms.setdefault(room, []).append(msg)
            rooms[room] = rooms[room][-MAX_PER_ROOM:]
            save_rooms()
        broadcast({'room': room, 'msg': msg}, to=room_members(room))
        # 推播：上面是誰傳的，下面是訊息；群組的話名字後面加「（群組名稱）」
        receivers = room_members(room) - {sender}
        title = f'{name_of(sender)}（{room_title(room)}）' if room_title(room) else name_of(sender)
        push_to(receivers, {'title': title, 'body': preview, 'tag': room, 'url': PUSH_SUBJECT})
        return self.send_json(200, msg)

    # ---------- 帳號：註冊、登入、改資料、加好友、建群組 ----------
    def me_payload(self, uid):
        """自己的資料 + 會用到的人（好友、群組成員；原本 4 個成員互相看得到）+ 自己的群組"""
        acc = accounts[uid]
        my_groups = [{'room': f'g:{gid}', 'name': g['name'], 'members': g['members'], 'owner': g['owner']}
                     for gid, g in groups.items() if uid in g['members']]
        people = {uid} | set(acc.get('friends', [])) | {m for g in my_groups for m in g['members']}
        if uid in LEGACY_IDS:
            people |= LEGACY_IDS
        return {
            'user': {**public_user(uid), 'legacy': uid in LEGACY_IDS, 'hasPassword': bool(acc.get('hash')),
                     'friends': [f for f in acc.get('friends', []) if f in accounts]},
            'users': [public_user(p) for p in sorted(people) if p in accounts],
            'groups': my_groups,
        }

    def handle_account(self, path):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 8192:
                return self.send_json(413, {'error': 'too large'})
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'bad json'})

        if path == '/api/register':
            name = str(data.get('name', '')).strip()
            password = str(data.get('password', ''))
            handle = str(data.get('handle', '')).strip()
            avatar = str(data.get('avatar', ''))
            if not 1 <= len(name) <= 20:
                return self.send_json(400, {'error': '名稱要 1～20 個字'})
            if len(password) < 4:
                return self.send_json(400, {'error': '密碼至少 4 個字'})
            if avatar and not UPLOAD_RE.fullmatch(avatar):
                return self.send_json(400, {'error': 'bad avatar'})
            with lock:
                if handle:
                    if not HANDLE_RE.fullmatch(handle):
                        return self.send_json(400, {'error': 'ID 只能用英文、數字、底線、點，3～20 個字'})
                    if uid_by_handle(handle):
                        return self.send_json(409, {'error': '這個 ID 已經有人用了'})
                else:
                    # ID 沒填：自動給一個 user + 6 位數字
                    while True:
                        handle = f'user{secrets.randbelow(900000) + 100000}'
                        if not uid_by_handle(handle):
                            break
                uid = 'u' + secrets.token_hex(5)
                while uid in accounts:
                    uid = 'u' + secrets.token_hex(5)
                acc = {'uid': uid, 'handle': handle, 'name': name, 'avatar': avatar, 'legacy': False,
                       'tokens': [], 'friends': [], 'created': int(time.time())}
                set_password(acc, password)
                accounts[uid] = acc
                token = new_token(uid)
                return self.send_json(200, {'token': token, **self.me_payload(uid)})

        if path == '/api/login':
            with lock:
                legacy = str(data.get('legacy', ''))
                if legacy:
                    # 原本 4 個成員點頭像登入：還沒設密碼才可以不用密碼
                    if legacy not in LEGACY_IDS:
                        return self.send_json(400, {'error': 'bad member'})
                    if accounts[legacy].get('hash') and not check_password(accounts[legacy], str(data.get('password', ''))):
                        return self.send_json(401, {'error': '密碼錯誤', 'needPassword': True})
                    uid = legacy
                else:
                    uid = uid_by_handle(str(data.get('handle', '')))
                    if not uid or not check_password(accounts[uid], str(data.get('password', ''))):
                        if uid and uid in LEGACY_IDS and not accounts[uid].get('hash'):
                            pass  # 原本的成員還沒設密碼：打 ID 也能登入
                        else:
                            return self.send_json(401, {'error': 'ID 或密碼錯誤'})
                token = new_token(uid)
                return self.send_json(200, {'token': token, **self.me_payload(uid)})

        uid = identify(data)
        if not uid:
            return self.send_json(401, {'error': 'login required'})

        if path == '/api/logout':
            with lock:
                h = sha(str(data.get('token', '')))
                accounts[uid]['tokens'] = [t for t in accounts[uid].get('tokens', []) if t != h]
                save_accounts()
            return self.send_json(200, {'ok': True})

        if path == '/api/account':
            with lock:
                acc = accounts[uid]
                if 'name' in data:
                    name = str(data['name']).strip()
                    if not 1 <= len(name) <= 20:
                        return self.send_json(400, {'error': '名稱要 1～20 個字'})
                    acc['name'] = name
                if 'handle' in data:
                    handle = str(data['handle']).strip()
                    if not HANDLE_RE.fullmatch(handle):
                        return self.send_json(400, {'error': 'ID 只能用英文、數字、底線、點，3～20 個字'})
                    other = uid_by_handle(handle)
                    if other and other != uid:
                        return self.send_json(409, {'error': '這個 ID 已經有人用了'})
                    acc['handle'] = handle
                if data.get('password'):
                    password = str(data['password'])
                    if len(password) < 4:
                        return self.send_json(400, {'error': '密碼至少 4 個字'})
                    # 已經有密碼：要先輸入舊密碼
                    if acc.get('hash') and not check_password(acc, str(data.get('oldPassword', ''))):
                        return self.send_json(401, {'error': '舊密碼不對'})
                    set_password(acc, password)
                if 'avatar' in data:
                    avatar = str(data['avatar'])
                    if avatar and not UPLOAD_RE.fullmatch(avatar):
                        return self.send_json(400, {'error': 'bad avatar'})
                    acc['avatar'] = avatar or (LEGACY[uid][2] if uid in LEGACY else '')
                save_accounts()
                payload = self.me_payload(uid)
                watchers = set(acc.get('friends', [])) | {m for g in payload['groups'] for m in g['members']}
            broadcast({'directory': True}, to=watchers | {uid})  # 好友那邊的名字、頭像也要更新
            return self.send_json(200, payload)

        if path == '/api/friends':
            with lock:
                other = uid_by_handle(str(data.get('handle', '')))
                if not other:
                    return self.send_json(404, {'error': '找不到這個 ID'})
                if other == uid:
                    return self.send_json(400, {'error': '不能加自己'})
                for a, b in ((uid, other), (other, uid)):
                    friends = accounts[a].setdefault('friends', [])
                    if b not in friends:
                        friends.append(b)
                save_accounts()
                payload = self.me_payload(uid)
            broadcast({'directory': True}, to={other})
            return self.send_json(200, {**payload, 'added': public_user(other)})

        if path == '/api/groups':
            name = str(data.get('name', '')).strip()
            with lock:
                gid = str(data.get('group', ''))
                if gid:
                    # 把好友加進已經有的群組
                    group = groups.get(gid)
                    if not group or uid not in group['members']:
                        return self.send_json(404, {'error': '找不到群組'})
                    add = [m for m in data.get('members', []) if m in accounts.get(uid, {}).get('friends', [])]
                    group['members'] = (group['members'] + [m for m in add if m not in group['members']])[:MAX_GROUP]
                else:
                    if not 1 <= len(name) <= 30:
                        return self.send_json(400, {'error': '群組名稱要 1～30 個字'})
                    friends = accounts[uid].get('friends', [])
                    members = [uid] + [m for m in dict.fromkeys(data.get('members', [])) if m in friends]
                    gid = secrets.token_hex(5)
                    group = {'name': name, 'members': members[:MAX_GROUP], 'owner': uid, 'created': int(time.time())}
                    groups[gid] = group
                save_groups()
                payload = self.me_payload(uid)
            broadcast({'directory': True}, to=set(group['members']))
            return self.send_json(200, {**payload, 'room': f'g:{gid}'})

        return self.send_error(404)

    # 上傳圖片（照片 / 塗鴉）：body 直接是圖片的位元組，存成亂碼檔名，回 {src}
    def handle_upload(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.send_json(400, {'error': 'bad length'})
        if length <= 0 or length > MAX_UPLOAD:
            return self.send_json(413, {'error': 'too large'})
        body = self.rfile.read(length)
        ext = sniff_image(body[:16])
        if not ext:
            return self.send_json(400, {'error': 'not an image'})
        name = f'{os.urandom(16).hex()}.{ext}'
        tmp = os.path.join(UPLOADS_DIR, name + '.tmp')
        with open(tmp, 'wb') as f:
            f.write(body)
        os.replace(tmp, os.path.join(UPLOADS_DIR, name))
        return self.send_json(200, {'src': name})

    # App 切到背景：{me}（網頁用 sendBeacon 送，內容是純文字的 JSON）
    def handle_away(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            data = json.loads(self.rfile.read(min(length, 1024)) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'bad json'})
        me = identify(data)
        if me:
            with lock:
                away.add(me)
        return self.send_json(200, {'ok': True})

    # 訂閱推播：{member, subscription}；取消：{unsubscribe: endpoint}
    def handle_push(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 8192:
                return self.send_json(413, {'error': 'too large'})
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'bad json'})
        if data.get('unsubscribe'):
            remove_subscription(str(data['unsubscribe']))
            return self.send_json(200, {'removed': True})
        member = identify(data)
        sub = data.get('subscription')
        if not member or not valid_subscription(sub):
            return self.send_json(400, {'error': 'bad subscription'})
        add_subscription(member, sub)
        return self.send_json(200, {'subscribed': True})

    # 通話訊號（響鈴、接聽、拒接、掛斷、WebRTC 的 offer / answer / ice）：
    # 透過同一條 Server-Sent Events 推給大家，網頁只理會「to 是自己」的
    def handle_signal(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > MAX_SIGNAL:
                return self.send_json(413, {'error': 'too large'})
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'bad json'})
        sender = identify(data)
        target = str(data.get('to', ''))
        kind = str(data.get('type', ''))
        # 全組通話的對象是聊天室（'group' 或自己建的群組 g:xxx），其他一律是某個人
        is_group = kind in GROUP_SIGNALS and (target == 'group' or target.startswith('g:'))
        if not sender or kind not in SIGNAL_TYPES:
            return self.send_json(400, {'error': 'invalid signal'})
        if is_group and sender not in room_members(target):
            return self.send_json(403, {'error': 'not in this room'})
        if not is_group and (target not in accounts or sender == target):
            return self.send_json(400, {'error': 'invalid signal'})
        signal = {'from': sender, 'to': target, 'type': kind, 'call': str(data.get('call', ''))[:40], 'data': data.get('data')}
        others = (room_members(target) if is_group else {target}) - {sender}
        broadcast({'signal': signal}, to=others)
        if kind == 'ring':
            # 對方沒開網頁：推播「來電」，對方 30 秒內打開網頁就補送響鈴
            with lock:
                pending_rings[target] = (time.time(), signal)
            mode = (data.get('data') or {}).get('mode')
            push_to([target], {
                'title': name_of(sender),
                'body': '語音來電' if mode == 'voice' else '視訊來電',
                'tag': 'call',
                'url': PUSH_SUBJECT,
            }, urgency='high')
        elif kind == 'gring':
            # 全組通話邀請：推播給其他每個人，並各自記住 30 秒內補送
            with lock:
                for member in others:
                    pending_rings[member] = (time.time(), signal)
            mode = (data.get('data') or {}).get('mode')
            push_to(list(others), {
                'title': f'{name_of(sender)}（{room_title(target)}）',
                'body': '邀請全組語音通話' if mode == 'voice' else '邀請全組視訊通話',
                'tag': 'call',
                'url': PUSH_SUBJECT,
            }, urgency='high')
        elif kind in ('accept', 'decline', 'busy', 'end'):
            with lock:
                for member in (sender, target):
                    ring = pending_rings.get(member)
                    if ring and ring[1]['call'] == signal['call']:
                        pending_rings.pop(member, None)
        elif kind in ('gjoin', 'gleave'):
            # 加入 / 離開全組通話：這個人自己的那通邀請就不用再補送了
            with lock:
                pending_rings.pop(sender, None)
        return self.send_json(200, {'ok': True})

    # Server-Sent Events：新訊息即時推給每支連著的手機
    def stream_events(self):
        self.send_response(200)
        self.send_cors()
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        q = queue.Queue()
        me = identify({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}) or ''
        listeners[q] = me
        if me:
            with lock:
                online[me] += 1
                away.discard(me)  # 回來了
                ring = pending_rings.get(me)
            # 點來電通知打開網頁：通話還在響就補送響鈴
            if ring and time.time() - ring[0] < 30:
                q.put(json.dumps({'signal': ring[1]}, ensure_ascii=False))
        try:
            self.wfile.write(b': connected\n\n')
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=10)
                    self.wfile.write(f'data: {data}\n\n'.encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')  # 保持連線
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            listeners.pop(q, None)
            if me:
                with lock:
                    online[me] -= 1


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
