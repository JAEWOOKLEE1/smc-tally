"""서울시의회 입법예고 의견 찬반 실시간 집계기.

실행:  python server.py            (기본 게시글: boardId=191857)
       python server.py --board-id 123456 --port 8765
       python server.py --share      (Cloudflare 터널로 누구나 볼 수 있는 주소 만들기)

브라우저에서 http://127.0.0.1:8765 를 열면 집계 화면이 보입니다.
"""
import argparse
import atexit
import html as htmllib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_URL = "https://www.smc.seoul.kr/board/BoardDetail.do"
PAGE_SIZE = 10

POLL_SECONDS = 60          # 최신 페이지 확인 간격
FULL_RESCAN_SECONDS = 1800  # 전체 재집계 간격(삭제·밀림 보정)
PAGE_DELAY = 0.4           # 전체 집계 시 페이지 사이 대기(사이트 부담 줄이기)
SCAN_WORKERS = 4           # 전체 집계 때 동시에 받는 페이지 수(의견이 1만 건을 넘어 한 줄로 받으면 30분 넘게 걸림)
FETCH_TRIES = 3            # 페이지 하나가 실패해도 전체 집계가 통째로 날아가지 않게 재시도

PRO_WORDS = ("찬성", "지지", "동의", "찬동", "환영", "착성", "찬선", "참성")  # 뒤의 셋은 흔한 오타
CON_WORDS = ("반대", "철회", "폐기")
# "동의"가 들어 있어도 부정이면 반대로 봅니다.
NEG_AGREE = ("부동의", "비동의", "동의하지", "동의 하지", "동의할 수 없", "동의 할 수 없", "동의 못", "동의못")
# 띄어쓰기가 제각각인 "동의할수 없", "동의 할수없", "동의 불가" 같은 경우
NEG_AGREE_RE = re.compile(r"동의\s*(?:할\s*수\s*(?:가\s*)?없|하기\s*(?:어렵|힘들)|불가)")
NOT_STANCE = ("동의안", "지지율")  # 입장이 아니라 명사인 경우
# "반대)) …", "찬성! …", "반대: …"처럼 맨 앞에 입장을 적고 부호로 끊은 경우도 괄호 표시만큼 확실합니다.
LEAD_RE = re.compile(r"^\W*(찬성|동의|반대|빈대|부동의)\s*(?:[)\]】〉>:!.,~/\-]|$)")
# "반대에 동의", "찬성 의견에 반대"처럼 다른 입장을 대상으로 말하는 경우
STRONG_AFTER = re.compile(r"\s*(?:합니다|해요|합니당|함|입니다|드립니다|하오|[.!~]|$)")
ABOUT_RE = re.compile(r"(반대|찬성)\s*(?:의견|측|하는 \S+)?\s*에\s*(동의|찬성|지지|반대)")
# "…(반대)", "[찬성] …"처럼 괄호로 입장을 밝힌 경우가 가장 우선입니다. "빈대"는 "반대" 오타.
MARK_RE = re.compile(r"[(\[【〈<]\s*(찬성|동의|반대|빈대|부동의)\s*[)\]】〉>]")
MARK_CLS = {"찬성": "pro", "동의": "pro", "반대": "con", "빈대": "con", "부동의": "con"}

ROW_RE = re.compile(
    r'<td class="num">(\d+)</td>\s*'
    r'<td class="subnamelist_content ellipsis"><a [^>]*>(.*?)</a></td>'
    r'.*?<td class="num">(\d{4}-\d{2}-\d{2})</td>',
    re.S,
)
CONTENT_RE = re.compile(r'<span id="content\d+" style="display:none;">(.*?)</span>', re.S)
TOTAL_RE = re.compile(r'class="tCnt">(\d+)<')


def clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def classify_text(text):
    m = MARK_RE.search(text) or LEAD_RE.match(text)
    if m:
        return MARK_CLS[m.group(1)]
    m = ABOUT_RE.search(text)
    if m:
        target = "pro" if m.group(1) == "찬성" else "con"
        if m.group(2) != "반대":
            return target
        return "con" if target == "pro" else "pro"
    for w in NOT_STANCE:
        text = text.replace(w, " " * len(w))
    hits = []  # (단정형 여부, 위치, 분류)

    def add(words, cls, src):
        for w in words:
            for m in re.finditer(re.escape(w), src):
                strong = bool(STRONG_AFTER.match(src, m.end())) or src[:m.start()].rstrip().endswith("적극")
                hits.append((strong, m.start(), cls))

    text = NEG_AGREE_RE.sub(lambda m: "부동의" + " " * (len(m.group(0)) - 3), text)
    add(CON_WORDS + NEG_AGREE, "con", text)
    for w in NEG_AGREE:
        text = text.replace(w, " " * len(w))
    add(PRO_WORDS, "pro", text)
    # 둘 다 있으면 "…합니다", "적극 …"처럼 단정한 쪽, 그중에서도 뒤에 나온 쪽을 입장으로 봅니다.
    # 예) "이걸 왜 반대하죠? 적극 찬성" → 찬성, "반대합니다. 찬성 측 주장은…" → 반대
    return max(hits)[2] if hits else None


def classify(title, content):
    # 제목으로 먼저 판단하고, 애매하면 본문 앞부분을 봅니다.
    return classify_text(title) or classify_text(content[:150]) or "other"


class Collector:
    def __init__(self, board_id, menu_id, board_type_id):
        self.params = {"menuId": menu_id, "boardTypeId": board_type_id, "boardId": board_id}
        self.board_id = board_id
        self.history_path = HERE / f"history_{board_id}.json"
        self.lock = threading.Lock()
        self.comments = []      # 최신순
        self.total = None
        self.history = self._load_history()
        self.status = "starting"
        self.progress = [0, 0]
        self.last_check = None
        self.last_full = 0.0
        self.last_error = None
        self.recent_new = []    # (시각, 개수)
        self.public_url = None

    # ---------- 수집 ----------
    def fetch_page(self, page):
        q = dict(self.params, replyPageIndex=page)
        url = BASE_URL + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (smc-tally)"})
        for attempt in range(FETCH_TRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    html = r.read().decode("utf-8", "replace")
                break
            except OSError:
                if attempt == FETCH_TRIES - 1:
                    raise
                time.sleep(3 * (attempt + 1))
        m = TOTAL_RE.search(html)
        total = int(m.group(1)) if m else 0
        rows = ROW_RE.findall(html)
        contents = [clean(c) for c in CONTENT_RE.findall(html)]
        if len(contents) != len(rows):
            contents = [""] * len(rows)
        items = []
        for (_, title, date), content in zip(rows, contents):
            title = clean(title)
            items.append({
                "title": title,
                "date": date or "",
                "cls": classify(title, content),
                "snippet": content[:120],
            })
        return total, items

    def full_scan(self):
        total, first = self.fetch_page(1)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        with self.lock:
            self.status = "scanning"
            self.progress = [1, pages]
        done = [1]

        def fetch(p):
            time.sleep(PAGE_DELAY)
            items = self.fetch_page(p)[1]
            with self.lock:
                done[0] += 1
                self.progress = [done[0], pages]
            return items

        collected = list(first)
        with ThreadPoolExecutor(SCAN_WORKERS) as pool:
            for items in pool.map(fetch, range(2, pages + 1)):  # 페이지 순서는 그대로 유지됩니다
                collected.extend(items)
        with self.lock:
            self.comments = collected[:total]
            self.total = total
            self.last_full = time.time()
            self.status = "live"
        self._record()

    def poll(self):
        total, first = self.fetch_page(1)
        with self.lock:
            known = self.total
        # 새 의견이 많이 몰려도 새로 생긴 페이지만 받으면 됩니다. 전체 집계는 줄어들었을 때(삭제)만.
        if known is None or total < known:
            return self.full_scan()
        diff = total - known
        if diff > 0:
            new = list(first)
            for p in range(2, math.ceil(diff / PAGE_SIZE) + 1):
                time.sleep(PAGE_DELAY)
                new.extend(self.fetch_page(p)[1])
            with self.lock:
                self.comments = new[:diff] + self.comments
                self.total = total
                self.recent_new.append((time.time(), diff))
                self.recent_new = self.recent_new[-50:]
            self._record()

    def run(self):
        while True:
            try:
                if self.total is None or time.time() - self.last_full > FULL_RESCAN_SECONDS:
                    self.full_scan()
                else:
                    self.poll()
                with self.lock:
                    self.last_check = time.time()
                    self.last_error = None
            except Exception as e:  # 네트워크 오류 등: 다음 주기에 재시도
                with self.lock:
                    self.last_error = f"{type(e).__name__}: {e}"
                    if self.status == "starting":
                        self.status = "error"
                print("[오류]", self.last_error)
            time.sleep(POLL_SECONDS)

    # ---------- 집계 ----------
    def counts(self):
        c = {"pro": 0, "con": 0, "other": 0}
        for it in self.comments:
            c[it["cls"]] += 1
        return c

    def _record(self):
        with self.lock:
            c = self.counts()
            point = {"t": time.time(), **c, "total": self.total}
            last = self.history[-1] if self.history else None
            if last and all(last[k] == point[k] for k in ("pro", "con", "other", "total")):
                return
            self.history.append(point)
            self.history = self.history[-5000:]
            data = json.dumps(self.history, ensure_ascii=False)
        try:
            self.history_path.write_text(data, encoding="utf-8")
        except OSError:
            pass

    def _load_history(self):
        try:
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def snapshot(self):
        with self.lock:
            by_day = {}
            for it in self.comments:
                d = by_day.setdefault(it["date"] or "날짜 없음", {"pro": 0, "con": 0, "other": 0})
                d[it["cls"]] += 1
            return {
                "boardUrl": BASE_URL + "?" + urllib.parse.urlencode(self.params),
                "status": self.status,
                "progress": self.progress,
                "total": self.total,
                "counts": self.counts(),
                "history": self.history[-1500:],
                "publicUrl": self.public_url,
                "byDay": [{"date": k, **v} for k, v in sorted(by_day.items())],
                "recent": self.comments[:30],
                "others": [it for it in self.comments if it["cls"] == "other"],
                "lastCheck": self.last_check,
                "lastError": self.last_error,
                "pollSeconds": POLL_SECONDS,
                "serverTime": time.time(),
            }


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/api/state", "/data.json"):
                body = json.dumps(collector.snapshot(), ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            elif path == "/api/health":
                body, ctype = b"ok", "text/plain"
            elif path in ("/", "/index.html"):
                body = (HERE / "index.html").read_bytes()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:  # 보는 사람이 창을 닫은 경우
                pass

        def log_message(self, *args):
            pass

    return Handler


TUNNEL_CHECK_SECONDS = 60   # 공개 주소가 살아 있는지 확인하는 간격
TUNNEL_MAX_FAILS = 3        # 연속 실패하면 터널을 새로 엽니다(주소가 바뀝니다)
SHARE_FILE = HERE / "공유주소.txt"


def start_tunnel(port, collector):
    """cloudflared 무료 터널을 띄우고, 끊기면(절전 복귀 등) 새 터널로 다시 엽니다."""
    exe = HERE / "tools" / "cloudflared.exe"
    exe = str(exe) if exe.exists() else shutil.which("cloudflared")
    if not exe:
        print("[공유] cloudflared를 찾지 못했어요. tools\\cloudflared.exe 위치를 확인하세요.")
        return
    current = {}
    atexit.register(lambda: current.get("proc") and current["proc"].terminate())

    def read_output(proc):
        for line in proc.stdout:
            m = re.search(r"https://[\w-]+\.trycloudflare\.com", line)
            if m and not collector.public_url:
                collector.public_url = m.group(0)
                print(f"\n[{datetime.now():%m-%d %H:%M}] [공유] 다른 사람에게 이 주소를 보내세요:  {collector.public_url}\n")
                try:
                    SHARE_FILE.write_text(f"{collector.public_url}\n({datetime.now():%Y-%m-%d %H:%M} 발급)\n", encoding="utf-8")
                except OSError:
                    pass

    def alive(url):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=15) as r:
                return r.status == 200
        except Exception:
            return False

    def supervise():
        while True:
            collector.public_url = None
            proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
            current["proc"] = proc
            threading.Thread(target=read_output, args=(proc,), daemon=True).start()
            fails, waited = 0, 0
            while proc.poll() is None and fails < TUNNEL_MAX_FAILS:
                time.sleep(TUNNEL_CHECK_SECONDS)
                waited += TUNNEL_CHECK_SECONDS
                url = collector.public_url
                if url:
                    fails = 0 if alive(url) else fails + 1
                elif waited >= 180:  # 3분이 지나도 주소를 못 받으면 다시 시도
                    fails = TUNNEL_MAX_FAILS
            if proc.poll() is None:
                proc.terminate()
            print(f"[{datetime.now():%m-%d %H:%M}] [공유] 공개 주소가 끊겨서 새로 연결합니다. 주소가 바뀌어요.")
            time.sleep(5)

    threading.Thread(target=supervise, daemon=True).start()


def keep_awake():
    """이 프로그램이 실행 중인 동안만 윈도우 절전 모드 진입을 막습니다(종료하면 원래대로)."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        print("[공유] 실행 중에는 PC가 절전 모드로 들어가지 않아요. (노트북 덮개를 닫으면 잠들 수 있어요)")
    except (AttributeError, OSError):
        pass


def main():
    ap = argparse.ArgumentParser(description="서울시의회 의견 찬반 실시간 집계")
    ap.add_argument("--board-id", default="191857")
    ap.add_argument("--menu-id", default="006003")
    ap.add_argument("--board-type-id", default="128")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--share", action="store_true", help="Cloudflare 터널로 공개 주소 만들기")
    args = ap.parse_args()

    collector = Collector(args.board_id, args.menu_id, args.board_type_id)
    threading.Thread(target=collector.run, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(collector))
    url = f"http://127.0.0.1:{args.port}"
    print(f"[{datetime.now():%H:%M:%S}] 집계 화면: {url}  (종료: Ctrl+C)")
    if args.share:
        print("[공유] 공개 주소를 만드는 중… (10초쯤 걸려요)")
        keep_awake()
        start_tunnel(args.port, collector)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("종료합니다.")


if __name__ == "__main__":
    main()
