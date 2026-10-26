"""GitHub Actions에서 주기적으로 실행: 의견 찬반을 세서 docs/data.json을 갱신합니다.

수집·분류 로직은 server.py(내 PC용 집계 서버)와 같은 것을 씁니다.
지난 실행의 의견 목록은 state/에 저장해 두고, 새로 늘어난 의견만 더합니다.
"""
import json
import time
from pathlib import Path

from server import Collector

BOARD_ID, MENU_ID, BOARD_TYPE_ID = "191857", "006003", "128"
FULL_RESCAN_SECONDS = 3600  # 한 시간에 한 번은 전체를 다시 세서 삭제·밀림을 보정

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "comments.json"
OUT = ROOT / "docs" / "data.json"


def main():
    STATE.parent.mkdir(exist_ok=True)
    c = Collector(BOARD_ID, MENU_ID, BOARD_TYPE_ID)
    c.history_path = ROOT / "state" / "history.json"
    c.history = c._load_history()
    try:
        saved = json.loads(STATE.read_text(encoding="utf-8"))
        c.comments, c.total, c.last_full = saved["comments"], saved["total"], saved["lastFull"]
    except (OSError, ValueError, KeyError):
        pass

    if c.total is None or time.time() - c.last_full > FULL_RESCAN_SECONDS:
        c.full_scan()
    else:
        c.poll()
    c.status = "live"
    c.last_check = time.time()

    STATE.write_text(json.dumps({"total": c.total, "lastFull": c.last_full, "comments": c.comments},
                                ensure_ascii=False), encoding="utf-8")
    snap = c.snapshot()
    snap.update(mode="github", pollSeconds=300)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    print(f"total={c.total} counts={c.counts()}")


if __name__ == "__main__":
    main()
