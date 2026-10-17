import json
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from playwright.async_api import async_playwright
from loguru import logger

UTC8 = timezone(timedelta(hours=8))


def load_cookies(cookies_path):
    """Load cookies from Cookie-Editor JSON format and convert to Playwright format."""
    with open(cookies_path, "r") as f:
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
        # Playwright sameSite mapping
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
    """Extract the browsing account's user ID from the twid cookie."""
    with open(cookies_path, "r") as f:
        raw_cookies = json.load(f)

    for c in raw_cookies:
        if c.get("name") == "twid":
            decoded = unquote(c["value"])  # u%3D1234567890 -> u=1234567890
            if decoded.startswith("u="):
                user_id = decoded[2:]
                logger.info(f"Extracted browsing user ID from twid cookie: {user_id}")
                return user_id

    logger.warning("Could not find twid cookie - browsing user ID unknown")
    return None


def parse_tweets_from_response(data):
    """Extract tweet objects from UserTweets GraphQL response."""
    tweets = []

    try:
        user_result = data.get("data", {}).get("user", {}).get("result", {})
        timeline_obj = user_result.get("timeline_v2", user_result.get("timeline", {}))
        inner_timeline = timeline_obj.get("timeline", timeline_obj)
        instructions = inner_timeline.get("instructions", [])
    except (AttributeError, TypeError):
        logger.error("Failed to parse GraphQL response structure")
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
    """Check if the response indicates an authentication error."""
    errors = data.get("errors", [])
    for err in errors:
        code = err.get("code", 0)
        if code in (32, 326, 64):
            return True, f"Twitter auth error code {code}: {err.get('message', '')}"
    return False, ""


async def fetch_tweets(config, username=None, cookies_path=None, profile_url=None):
    """
    username: 监控账号用户名（可选）
    cookies_path / profile_url: 可覆盖 config
    """
    cookies_path = cookies_path or config.get("twitter", {}).get("cookies_path", "./cookies/twitter.json")
    if profile_url:
        pass
    elif username:
        profile_url = f"https://x.com/{username.lstrip('@')}"
    else:
        profile_url = config.get("twitter", {}).get("profile_url") or config.get("my_x", {}).get("profile_url")
    if not profile_url:
        return [], "未配置 profile_url 或 username"

    captured_responses = []
    auth_error = None

    async def handle_response(response):
        nonlocal auth_error
        url = response.url
        # 打印所有 GraphQL 请求，方便调试
        if "/graphql/" in url:
            logger.info(f"GraphQL: {url.split('/graphql/')[-1][:80]}")
            if any(x in url for x in ["UserTweets", "UserTweetsAndReplies", "UserOriginalsTimeline", "UserPosts"]):
                try:
                    data = await response.json()
                    has_err, err_msg = check_auth_error(data)
                    if has_err:
                        auth_error = err_msg
                        return
                    captured_responses.append(data)
                    logger.info(f"Captured GraphQL response containing tweets data")
                except Exception as e:
                    logger.warning(f"Failed to parse XHR response: {e}")

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
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            java_script_enabled=True,
        )

        # 隐藏 webdriver 特征
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        # Load cookies
        pw_cookies = load_cookies(cookies_path)
        await context.add_cookies(pw_cookies)

        page = await context.new_page()
        page.on("response", handle_response)

        try:
            logger.info(f"Navigating to {profile_url}")
            # 改用 domcontentloaded，并加长等待
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(8)

            # 主动滚动，触发加载推文
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(2)

            if page.url and "/login" in page.url:
                auth_error = "Redirected to login page -- cookies expired"
                await browser.close()
                return [], auth_error

            # 多等一会儿
            for _ in range(15):
                if captured_responses or auth_error:
                    break
                await asyncio.sleep(1)

            logger.info(f"Current page URL: {page.url}")

            if not captured_responses:
                logger.warning("No UserTweets XHR captured, trying scroll...")
                for _ in range(3):  # 多滚几次
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(3)
                    if captured_responses:
                        break

            # Parse first batch
            cursor = None
            for resp_data in captured_responses:
                tweets, cursor = parse_tweets_from_response(resp_data)
                all_tweets.extend(tweets)

            logger.info(f"Fetched {len(all_tweets)} tweets from first page")

            # Scroll for more tweets if needed (max 3 additional pages)
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

                logger.info(f"Page {page_num + 2}: total {len(all_tweets)} tweets")

        except FileNotFoundError as e:
            logger.error(f"Cookie file not found: {e}")
            auth_error = f"Cookie file not found: {e}. Copy your Cookie-Editor export to cookies/twitter.json"

        except Exception as e:
            err_str = str(e)
            logger.error(f"Fetcher error: {e}")
            if "net::" in err_str:
                auth_error = f"Network error reaching Twitter: {err_str}. Check that your VPS can access x.com"
            else:
                auth_error = f"Fetcher error: {err_str}"

        finally:
            await browser.close()

    logger.info(f"Fetch complete: {len(all_tweets)} total tweet objects")
    return all_tweets, auth_error
