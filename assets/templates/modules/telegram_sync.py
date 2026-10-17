import os
import aiohttp
from loguru import logger

SYNC_TOKEN = os.getenv("TELEGRAM_SYNC_BOT_TOKEN", "").strip()
SYNC_CHAT = os.getenv("TELEGRAM_SYNC_CHAT_ID", "").strip()


async def send_sync_post(text: str, square_url: str = "") -> bool:
    if not SYNC_TOKEN or not SYNC_CHAT:
        logger.debug("未配置公开 TG，跳过")
        return False
    body = (text or "").strip()
    if square_url:
        body = f"{body}\n\n🔗 币安广场：{square_url}"
    if len(body) > 4000:
        body = body[:4000] + "..."
    url = f"https://api.telegram.org/bot{SYNC_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id": SYNC_CHAT,
                    "text": body,
                    "disable_web_page_preview": False,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    logger.info("已同步到 Telegram 公开频道")
                    return True
                logger.error(f"TG 公开频道失败: {data}")
                return False
    except Exception as e:
        logger.error(f"TG 公开频道异常: {e}")
        return False
