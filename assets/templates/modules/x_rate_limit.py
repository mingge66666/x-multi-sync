import os
import time
import random
import fcntl
from pathlib import Path
from datetime import datetime, timezone, timedelta
from loguru import logger

UTC8 = timezone(timedelta(hours=8))
LOCK_PATH = Path(os.getenv("X_POST_LOCK_FILE", "./tmp/x_post_global.lock"))
STATE_PATH = Path(os.getenv("X_POST_STATE_FILE", "./tmp/x_post_state.txt"))


def _read_state():
    last_ts, day, count = 0.0, "", 0
    if STATE_PATH.exists():
        try:
            parts = STATE_PATH.read_text().strip().split(",")
            if len(parts) >= 3:
                last_ts, day, count = float(parts[0]), parts[1], int(parts[2])
        except Exception:
            pass
    return last_ts, day, count


def _write_state(last_ts, day, count):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(f"{last_ts},{day},{count}")


def acquire_x_post_slot():
    """跨调用限速；返回 (ok, reason)。"""
    min_interval = int(os.getenv("X_POST_MIN_INTERVAL_SEC", "240"))
    jitter = int(os.getenv("X_POST_JITTER_SEC", "60"))
    daily_limit = int(os.getenv("X_POST_DAILY_LIMIT", "25"))

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.touch(exist_ok=True)

    with open(LOCK_PATH, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            now = time.time()
            today = datetime.now(UTC8).strftime("%Y-%m-%d")
            last_ts, day, count = _read_state()
            if day != today:
                day, count = today, 0

            if count >= daily_limit:
                return False, f"已达今日发帖上限 {daily_limit}"

            wait_need = min_interval + random.randint(0, max(0, jitter))
            elapsed = now - last_ts
            if last_ts > 0 and elapsed < wait_need:
                sleep_for = wait_need - elapsed
                logger.info(f"X 发帖限速，等待 {sleep_for:.0f} 秒")
                time.sleep(sleep_for)
                now = time.time()

            count += 1
            _write_state(now, day, count)
            logger.info(f"获得发帖名额：今日 {count}/{daily_limit}")
            return True, "ok"
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
