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
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        continue
                    ext = ".jpg"
                    u = url.lower()
                    if ".png" in u:
                        ext = ".png"
                    elif ".webp" in u:
                        ext = ".webp"
                    p = tmp / f"img_{i}{ext}"
                    p.write_bytes(await r.read())
                    paths.append(str(p))
            except Exception as e:
                logger.warning(f"下载配图失败: {e}")
    return paths


async def post_to_x(text: str, cookies_path: str, image_urls=None) -> bool:
    """发布到自己的 X（中文正文 + 可选图片）。"""
    if not text or not str(text).strip():
        return False
    if not Path(cookies_path).exists():
        logger.warning(f"未找到发帖 Cookie 文件: {cookies_path}")
        return False

    ok, reason = acquire_x_post_slot()
    if not ok:
        logger.warning(f"跳过发布到 X：{reason}")
        return False

    t = str(text).strip()
    if len(t) > 280:
        t = t[:277] + "..."

    local_images = await _download_images(image_urls or [])

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

            editor = page.locator(
                '[data-testid="tweetTextarea_0"], div[role="textbox"]'
            ).first
            await editor.wait_for(state="visible", timeout=20000)
            await editor.click()
            await editor.fill(t)
            await asyncio.sleep(1.5)

            if local_images:
                fi = page.locator(
                    'input[data-testid="fileInput"], input[type="file"]'
                ).first
                if await fi.count() > 0:
                    await fi.set_input_files(local_images)
                    await asyncio.sleep(3 + len(local_images))

            btn = page.locator(
                '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
            ).first
            await btn.click()
            await asyncio.sleep(6)
            logger.info("已成功发布到自己的 X 账号")
            return True
        except Exception as e:
            logger.error(f"发布到自己的 X 失败: {e}")
            return False
        finally:
            await browser.close()
            for pth in local_images:
                try:
                    Path(pth).unlink(missing_ok=True)
                except Exception:
                    pass
