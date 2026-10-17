import os
import aiohttp
from loguru import logger

SYNC_TOKEN = os.getenv("TELEGRAM_SYNC_BOT_TOKEN", "").strip()
SYNC_CHAT = os.getenv("TELEGRAM_SYNC_CHAT_ID", "").strip()


async def send_sync_post(text: str, square_url: str = "", image_urls=None) -> bool:
    """
    同步到 Telegram 公开频道。
    - 正文与 X 一致（调用方传入）
    - 不附加币安广场链接
    - 有图则发图
    """
    if not SYNC_TOKEN or not SYNC_CHAT:
        logger.debug("未配置公开频道 Telegram，已跳过")
        return False

    body = (text or "").strip()
    # 明确不使用 square_url
    if len(body) > 4000:
        body = body[:4000] + "..."

    image_urls = [u for u in (image_urls or []) if u]
    timeout = aiohttp.ClientTimeout(total=60)

    try:
        async with aiohttp.ClientSession() as session:
            if image_urls:
                if len(image_urls) == 1:
                    api = f"https://api.telegram.org/bot{SYNC_TOKEN}/sendPhoto"
                    payload = {
                        "chat_id": SYNC_CHAT,
                        "photo": image_urls[0],
                        "caption": body[:1024] if body else "",
                    }
                    async with session.post(api, json=payload, timeout=timeout) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            logger.info("已同步到公开频道（单图）")
                            return True
                        logger.error(f"公开频道单图失败: {data}")
                else:
                    media = []
                    for i, u in enumerate(image_urls[:10]):
                        item = {"type": "photo", "media": u}
                        if i == 0 and body:
                            item["caption"] = body[:1024]
                        media.append(item)
                    api = f"https://api.telegram.org/bot{SYNC_TOKEN}/sendMediaGroup"
                    async with session.post(
                        api,
                        json={"chat_id": SYNC_CHAT, "media": media},
                        timeout=timeout,
                    ) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            logger.info("已同步到公开频道（多图）")
                            return True
                        logger.error(f"公开频道多图失败: {data}")

            # 无图或发图失败：纯文字
            api = f"https://api.telegram.org/bot{SYNC_TOKEN}/sendMessage"
            async with session.post(
                api,
                json={
                    "chat_id": SYNC_CHAT,
                    "text": body or "（无文字）",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    logger.info("已同步到公开频道（纯文字）")
                    return True
                logger.error(f"公开频道发文字失败: {data}")
                return False
    except Exception as e:
        logger.error(f"公开频道异常: {e}")
        return False
