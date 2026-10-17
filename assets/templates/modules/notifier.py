import os
import json
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from loguru import logger

UTC8 = timezone(timedelta(hours=8))
OPS_TOKEN = os.getenv("TELEGRAM_OPS_BOT_TOKEN", "").strip()
OPS_CHAT = os.getenv("TELEGRAM_OPS_CHAT_ID", "").strip()
QUEUE_FILE = "./tmp/telegram_ops_queue.json"


async def _send_message(content: str) -> bool:
    if not OPS_TOKEN or not OPS_CHAT:
        return False
    if len(content) > 4000:
        content = content[:4000] + "..."
    url = f"https://api.telegram.org/bot{OPS_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id": OPS_CHAT,
                    "text": content,
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                return bool(data.get("ok"))
    except Exception as e:
        logger.error(f"运维 TG 发送失败: {e}")
        return False


async def _send_with_retry(content: str) -> bool:
    if await _send_message(content):
        return True
    await asyncio.sleep(30)
    if await _send_message(content):
        return True
    queue = []
    if os.path.exists(QUEUE_FILE):
        try:
            queue = json.loads(Path_read := open(QUEUE_FILE, encoding="utf-8").read())
        except Exception:
            queue = []
    os.makedirs("./tmp", exist_ok=True)
    queue.append({"content": content})
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False)
    return False


def send_alert(message, severity="INFO"):
    prefix = {
        "CRITICAL": "🔴【严重】",
        "ERROR": "🟠【错误】",
        "WARNING": "🟡【警告】",
        "INFO": "🔵【信息】",
    }.get(severity, "ℹ️【通知】")
    content = f"{prefix} [x-multi-sync]\n{message}"
    try:
        loop = asyncio.get_running_loop()
        asyncio.ensure_future(_send_with_retry(content))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send_with_retry(content))
        finally:
            loop.close()


def send_daily_digest(stats):
    now = datetime.now(UTC8)
    content = (
        f"📊 每日报告 — {now.strftime('%Y-%m-%d')}\n"
        f"采集发X：{stats.get('published', 0)}\n"
        f"分发广场：{stats.get('distributed', 0)}\n"
        f"过滤/跳过：{stats.get('filtered', 0)}\n"
        f"失败：{stats.get('failed', 0)}"
    )
    send_alert(content, "INFO")


async def flush_queue():
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            queue = json.load(f)
    except Exception:
        return
    remaining = []
    for item in queue:
        if not await _send_message(item.get("content", "")):
            remaining.append(item)
    if remaining:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(remaining, f, ensure_ascii=False)
    else:
        try:
            os.remove(QUEUE_FILE)
        except Exception:
            pass
