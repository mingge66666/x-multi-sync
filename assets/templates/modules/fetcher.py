import json
import asyncio
from urllib.parse import unquote
from playwright.async_api import async_playwright
from loguru import logger


def load_cookies(cookies_path):
    """将 Cookie-Editor 导出的 JSON 转为 Playwright 格式。"""
    with open(cookies_path, "r", encoding="utf-8") as f:
        raw_cookies = json.load(f)

    pw_cookies = []
    for c in raw_cookies:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
        }
        if c.get("expirationDate"):
            cookie["expires"] = c["expirationDate"]
        if c.get("httpOnly"):
            cookie["httpOnly"] = True
        if c.get("secure"):
            cookie["secure"] = True
        ss = c.get("sameSite", "")
        if ss == "no_restriction":
            cookie["sameSite"] = "None"
        elif ss == "lax":
            cookie["sameSite"] = "Lax"
        elif ss == "strict":
            cookie["sameSite"] = "Strict"
        pw_cookies.append(cookie)

    return pw_cookies


def extract_browsing_user_id(cookies_path):
    """从 twid Cookie 解析当前浏览账号用户 ID。"""
    with open(cookies_path, "r", encoding="utf-8") as f:
        raw_cookies = json.load(f)

    for c in raw_cookies:
        if c.get("name") == "twid":
            decoded = unquote(c["value"])
            if decoded.startswith("u="):
                user_id = decoded[2:]
                logger.info(f"从 twid 解析到浏览账号 ID: {user_id}")
                return user_id

    logger.warning("未找到 twid Cookie，无法解析浏览账号 ID")
    return None


def parse_tweets_from_response(data):
    """从 GraphQL 时间线响应中解析推文列表。"""
    tweets = []

    try:
        user_result = data.get("data", {}).get("user", {}).get("result", {})
        timeline_obj = user_result.get("timeline_v2", user_result.get("timeline", {}))
        inner_timeline = timeline_obj.get("timeline", timeline_obj)
        instructions = inner_timeline.get("instructions", [])
    except (AttributeError, TypeError):
        logger.error("解析 GraphQL 响应结构失败")
        return tweets, None

    bottom_cursor = None

    for instruction in instructions:
        entries = instruction.get("entries", [])
        if not entries and instruction.get("type") == "TimelineAddToModule":
            entries = instruction.get("moduleItems", [])

        for entry in entries:
            entry_id = entry.get("entryId", "")

            if "cursor-bottom" in entry_id:
                content = entry.get("content", {})
                bottom_cursor = content.get("value", "")
                continue

            if "cursor-top" in entry_id:
                continue

            items_to_process = []
            content = entry.get("content", {})
            entry_type = content.get("entryType", content.get("__typename", ""))

            if entry_type == "TimelineTimelineItem":
                items_to_process.append(content.get("itemContent", {}))
            elif entry_type == "TimelineTimelineModule":
                module_items = content.get("items", [])
                for mi in module_items:
                    item = mi.get("item", {})
                    items_to_process.append(item.get("itemContent", {}))

            for item_content in items_to_process:
                tweet_results = item_content.get("tweet_results", {})
                result = tweet_results.get("result", {})

                if not result:
                    continue

                if result.get("__typename") == "TweetWithVisibilityResults":
                    result = result.get("tweet", {})

                if result.get("__typename") == "TweetTombstone":
                    continue

                rest_id = result.get("rest_id", "")
                if not rest_id:
                    continue

                tweets.append(result)

    return tweets, bottom_cursor


def check_auth_error(data):
    """检查是否为登录/鉴权错误。"""
    errors = data.get("errors", [])
    for err in errors:
        code = err.get("code", 0)
        if code in (32, 326, 64):
            return True, f"Twitter 鉴权错误码 {code}: {err.get('message', '')}"
    return False, ""


async def fetch_tweets(config, username=None, cookies_path=None, profile_url=None):
    """
    使用 Playwright 拦截 GraphQL，抓取指定用户主页推文。

    参数:
      config: 完整配置字典
      username: 可选，监控账号用户名（不含 @）
      cookies_path: 可选，覆盖 Cookie 路径
      profile_url: 可选，覆盖主页 URL

    返回:
      (推文列表, 错误信息或 None)
    """
    cookies_path = cookies_path or config.get("twitter", {}).get(
        "cookies_path", "./cookies/twitter.json"
    )

    if profile_url:
        pass
    elif username:
        profile_url = f"https://x.com/{str(username).lstrip('@')}"
    else:
        profile_url = config.get("twitter", {}).get("profile_url") or config.get(
            "my_x", {}
        ).get("profile_url")

    if not profile_url:
        return [], "未配置 profile_url 或 username，无法打开用户主页"

    captured_responses = []
    auth_error = None

    async def handle_response(response):
        nonlocal auth_error
        url = response.url
        if "/graphql/" in url:
            logger.info(f"GraphQL: {url.split('/graphql/')[-1][:80]}")
            if any(
                x in url
                for x in [
                    "UserTweets",
                    "UserTweetsAndReplies",
                    "UserOriginalsTimeline",
                    "UserPosts",
                ]
            ):
                try:
                    data = await response.json()
                    has_err, err_msg = check_auth_error(data)
                    if has_err:
                        auth_error = err_msg
                        return
                    captured_responses.append(data)
                    logger.info("已捕获包含推文数据的 GraphQL 响应")
                except Exception as e:
                    logger.warning(f"解析 XHR 响应失败: {e}")

    all_tweets = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            java_script_enabled=True,
        )

        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        )

        try:
            pw_cookies = load_cookies(cookies_path)
            await context.add_cookies(pw_cookies)
        except FileNotFoundError as e:
            await browser.close()
            msg = f"未找到 Cookie 文件: {e}。请将 Cookie-Editor 导出放到 {cookies_path}"
            logger.error(msg)
            return [], msg

        page = await context.new_page()
        page.on("response", handle_response)

        try:
            logger.info(f"正在打开主页: {profile_url}")
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(8)

            for _ in range(4):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(2)

            if page.url and "/login" in page.url:
                auth_error = "已跳转到登录页，Cookie 可能已失效，请重新导出"
                await browser.close()
                return [], auth_error

            for _ in range(15):
                if captured_responses or auth_error:
                    break
                await asyncio.sleep(1)

            logger.info(f"当前页面地址: {page.url}")

            if not captured_responses:
                logger.warning("未捕获到推文接口，尝试继续滚动…")
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(3)
                    if captured_responses:
                        break

            cursor = None
            for resp_data in captured_responses:
                tweets, cursor = parse_tweets_from_response(resp_data)
                all_tweets.extend(tweets)

            logger.info(f"第一页解析到 {len(all_tweets)} 条推文对象")

            for page_num in range(3):
                if not cursor:
                    break

                captured_responses.clear()
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)

                for _ in range(10):
                    if captured_responses:
                        break
                    await asyncio.sleep(1)

                if not captured_responses:
                    break

                for resp_data in captured_responses:
                    tweets, cursor = parse_tweets_from_response(resp_data)
                    all_tweets.extend(tweets)

                logger.info(f"第 {page_num + 2} 页累计 {len(all_tweets)} 条推文对象")

        except FileNotFoundError as e:
            logger.error(f"未找到 Cookie 文件: {e}")
            auth_error = f"未找到 Cookie 文件: {e}。请导出到 {cookies_path}"

        except Exception as e:
            err_str = str(e)
            logger.error(f"抓取异常: {e}")
            if "net::" in err_str:
                auth_error = f"网络错误，无法访问 X: {err_str}。请检查 VPS 是否能打开 x.com"
            else:
                auth_error = f"抓取异常: {err_str}"

        finally:
            await browser.close()

    logger.info(f"抓取结束，共 {len(all_tweets)} 条推文对象")
    return all_tweets, auth_error
