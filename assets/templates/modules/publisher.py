import json
import asyncio
import tempfile
from pathlib import Path
import aiohttp
from playwright.async_api import async_playwright
from loguru import logger
from modules.x_rate_limit import acquire_x_post_slot


def load_cookies(path: str):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for c in raw:
        item = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
        }
        if c.get("expirationDate"):
            item["expires"] = c["expirationDate"]
        if c.get("httpOnly"):
            item["httpOnly"] = True
        if c.get("secure"):
            item["secure"] = True
        ss = (c.get("sameSite") or "").lower()
        if ss in ("no_restriction", "none"):
            item["sameSite"] = "None"
        elif ss == "lax":
            item["sameSite"] = "Lax"
        elif ss == "strict":
            item["sameSite"] = "Strict"
        out.append(item)
    return out


async def _download_images(urls, max_n=4):
    paths = []
    if not urls:
        return paths
    tmp = Path(tempfile.mkdtemp(prefix="ximg_"))
    async with aiohttp.ClientSession() as session:
        for i, url in enumerate(urls[:max_n]):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as r:
                    if r.status != 200:
                        continue
                    u = url.lower().split("?")[0]
                    ext = ".png" if u.endswith(".png") else ".jpg"
                    if ".webp" in u:
                        ext = ".webp"
                    p = tmp / f"img_{i}{ext}"
                    p.write_bytes(await r.read())
                    paths.append(str(p))
            except Exception as e:
                logger.warning(f"下载图片失败: {e}")
    return paths


async def post_to_x(text: str, cookies_path: str, image_urls=None, video_urls=None) -> bool:
    """
    发到自己的 X：正文 + 图片。
    video_urls 参数保留兼容，忽略不发。
    """
    image_urls = image_urls or []
    # 明确不发视频
    _ = video_urls

    if not (text and str(text).strip()) and not image_urls:
        return False
    if not Path(cookies_path).exists():
        logger.warning(f"未找到发帖 Cookie: {cookies_path}")
        return False

    ok, reason = acquire_x_post_slot()
    if not ok:
        logger.warning(f"跳过发 X: {reason}")
        return False

    t = (text or "").strip()
    if len(t) > 280:
        t = t[:277] + "..."

    local_files = await _download_images(image_urls, max_n=4)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        await context.add_cookies(load_cookies(cookies_path))
        page = await context.new_page()
        try:
            await page.goto(
                "https://x.com/compose/post",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await asyncio.sleep(4)

            if t:
                editor = page.locator(
                    '[data-testid="tweetTextarea_0"], div[role="textbox"]'
                ).first
                await editor.wait_for(state="visible", timeout=20000)
                await editor.click()
                await editor.fill(t)
                await asyncio.sleep(1.5)

            if local_files:
                fi = page.locator(
                    'input[data-testid="fileInput"], input[type="file"]'
                ).first
                if await fi.count() > 0:
                    await fi.set_input_files(local_files)
                    await asyncio.sleep(3 + len(local_files))

            btn = page.locator(
                '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
            ).first
            await btn.click()
            await asyncio.sleep(6)
            logger.info("已发到自己的 X" + ("（含图）" if local_files else ""))
            return True
        except Exception as e:
            logger.error(f"发到自己的 X 失败: {e}")
            return False
        finally:
            await browser.close()
            for pth in local_files:
                try:
                    Path(pth).unlink(missing_ok=True)
                except Exception:
                    pass
