import re
from modules import db
from modules.processor import (
    process_tweet,
    clean_for_distribute,
    protect_cashtags_for_square,
    ensure_cashtags_present,
)
from loguru import logger

# 普通 X 账号：加权长度严格 < 280 才走「发自己的 X」
X_FREE_MAX_CHARS = 280
# 币安广场短帖：实测约 2100 即提示超限（按字符数，汉字=3）
BINANCE_SQUARE_MAX_CHARS = 2080

PERSONAL_LINK_PATTERNS = [
    r"(?i)t\.me/",
    r"(?i)telegram\.me/",
    r"(?i)discord\.gg/",
    r"(?i)discord\.com/invite",
    r"(?i)youtube\.com/",
    r"(?i)youtu\.be/",
    r"(?i)instagram\.com/",
    r"(?i)tiktok\.com/",
    r"(?i)onlyfans\.com/",
    r"(?i)linktr\.ee/",
    r"(?i)bio\.link/",
    r"(?i)carrd\.co/",
    r"(?i)bit\.ly/",
    r"(?i)tinyurl\.com/",
]

PROMO_KEYWORDS = [
    "follow me", "follow for", "subscribe", "giveaway", "airdrop",
    "dm me", "join my", "my channel", "my group", "my community",
    "click the link", "link in bio", "check my", "retweet and",
    "like and rt", "tag your friends",
    "蓝V", "私信", "详询", "蓝 V",
    "传送门", "注册",
    "加微信", "加v", "社群收费",
]

OFFTOPIC_KEYWORDS = [
    "happy birthday", "celebrating my", "enjoying life",
    "wedding", "vacation", "holiday",
    "运营x", "我的 x", "我的x", "粉丝", "x上", "互动",
]


def x_weighted_length(text: str) -> int:
    """
    X 近似加权字数：
    - 基本拉丁 / 数字 / 常见标点：1
    - CJK、全角、emoji 等：2
    （链接未单独按 23 计，足够用于路由判断）
    """
    if not text:
        return 0
    n = 0
    for ch in text:
        o = ord(ch)
        if o <= 0x7F:
            n += 1
        else:
            n += 2
    return n


def has_personal_link(text: str) -> bool:
    if not text:
        return False
    return any(re.search(p, text) for p in PERSONAL_LINK_PATTERNS)


def has_promo_or_offtopic(text: str) -> bool:
    lower = (text or "").lower()
    return any(k in lower for k in PROMO_KEYWORDS + OFFTOPIC_KEYWORDS)


def get_media_types(tweet):
    media_list = []
    legacy = tweet.get("legacy", {})
    for source in [legacy.get("extended_entities", {}), legacy.get("entities", {})]:
        for m in source.get("media", []):
            media_list.append(m.get("type", ""))
    return media_list


def get_image_urls(tweet, max_images=4):
    urls, seen = [], set()
    legacy = tweet.get("legacy", {})
    if legacy.get("extended_entities"):
        sources = [legacy["extended_entities"]]
    elif legacy.get("entities"):
        sources = [legacy["entities"]]
    else:
        sources = []
    for source in sources:
        for m in source.get("media") or []:
            if m.get("type") != "photo":
                continue
            mid = str(m.get("id_str") or m.get("id") or "")
            u = m.get("media_url_https") or m.get("media_url")
            if not u:
                continue
            key = mid or u.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            if "pbs.twimg.com" in u and "name=" not in u:
                u = u + ("&" if "?" in u else "?") + "name=orig"
            urls.append(u)
            if len(urls) >= max_images:
                return urls
    return urls


def get_symbol_names(tweet) -> list:
    """entities.symbols + 正文正则，尽量收齐代币名。"""
    names = []
    seen = set()

    legacy = tweet.get("legacy", {}) or {}
    for s in (legacy.get("entities") or {}).get("symbols") or []:
        t = (s.get("text") or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            names.append(t)

    note = (tweet.get("note_tweet") or {}).get("note_tweet_results") or {}
    note_res = note.get("result") or {}
    for s in (note_res.get("entity_set") or {}).get("symbols") or []:
        t = (s.get("text") or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            names.append(t)

    raw = legacy.get("full_text") or ""
    if note_res.get("text"):
        raw = note_res.get("text") or raw
    for m in re.finditer(r"[＄$]\s*([A-Za-z][A-Za-z0-9_]{0,30})", raw or ""):
        t = m.group(1)
        if t.lower() not in seen:
            seen.add(t.lower())
            names.append(t)

    return names


def _remove_entity_urls(text: str, tweet) -> str:
    if not text:
        return ""
    legacy = tweet.get("legacy", {}) or {}
    entities = legacy.get("entities") or {}
    ext = legacy.get("extended_entities") or {}
    candidates = []
    for m in (ext.get("media") or entities.get("media") or []):
        for k in ("url", "display_url", "expanded_url", "media_url", "media_url_https"):
            v = m.get(k)
            if v:
                candidates.append(v)
    for u in entities.get("urls") or []:
        for k in ("url", "display_url", "expanded_url"):
            v = u.get(k)
            if v:
                candidates.append(v)
    for v in sorted(set(candidates), key=len, reverse=True):
        text = text.replace(v, "")
        if "://" not in v:
            text = text.replace("https://" + v, "").replace("http://" + v, "")
    return text


def get_tweet_text(tweet):
    legacy = tweet.get("legacy", {}) or {}
    text = legacy.get("full_text", "") or tweet.get("text", "") or ""

    note = (tweet.get("note_tweet") or {}).get("note_tweet_results") or {}
    note_res = note.get("result") or {}
    if note_res.get("text"):
        text = note_res.get("text") or text

    text = _remove_entity_urls(text, tweet)
    return text.strip()


def filter_tweet(tweet, target_user_id, max_content_length=699, strict_promo=True):
    """
    返回:
      (ok, result, tweet_data)

    strict_promo=True（采集监控号）时：
      tweet_data["route"] ∈ {"x", "square_tg"}
      - x: 发自己的 X，再由 distribute_from_my_x 同步广场+TG
      - square_tg: 不发 X，采集阶段直接发广场+TG

    strict_promo=False（从自己的 X 分发）时：
      仍只做正文清洗，供广场/TG 使用。
    """
    tweet_id = tweet.get("rest_id", "") or tweet.get("id_str", "")
    legacy = tweet.get("legacy", {})
    raw_text = get_tweet_text(tweet)
    media_types = get_media_types(tweet)
    has_photo = "photo" in media_types
    has_video = "video" in media_types or "animated_gif" in media_types

    tweet_data = {
        "tweet_id": tweet_id,
        "content_raw": raw_text,
        "has_images": 1 if has_photo else 0,
        "has_video": 1 if has_video else 0,
        "tweet_type": "original",
        "image_urls": get_image_urls(tweet),
        "video_urls": [],
        "route": "x",
    }

    if db.tweet_exists(tweet_id):
        logger.debug(f"F-00 去重跳过: {tweet_id}")
        return False, "dedup", tweet_data

    if has_video:
        logger.info(f"F-01 跳过视频/GIF: {tweet_id}")
        return False, "drop_video", tweet_data

    if legacy.get("retweeted_status_result") or raw_text.startswith("RT @"):
        tweet_data["tweet_type"] = "retweet"
        logger.debug(f"F-02 跳过转推: {tweet_id}")
        return False, "drop_rt", tweet_data

    reply_to_user = legacy.get("in_reply_to_user_id_str", "")
    if reply_to_user and reply_to_user != target_user_id:
        tweet_data["tweet_type"] = "reply"
        logger.debug(f"F-03 跳过回复他人: {tweet_id}")
        return False, "drop_reply", tweet_data

    if reply_to_user == target_user_id:
        tweet_data["tweet_type"] = "thread_continuation"
        logger.debug(f"F-04 跳过串帖续贴: {tweet_id}")
        return False, "drop_thread", tweet_data

    if strict_promo:
        # —— 采集 → 自己的 X / 或直接广场+TG ——
        if not has_photo:
            logger.info(f"F-05 跳过纯文字: {tweet_id}")
            return False, "drop_text_only", tweet_data

        # 先按广场上限清洗，避免 process 时被错误截断影响路由
        clean_text = process_tweet(
            raw_text,
            max_length=max(max_content_length, BINANCE_SQUARE_MAX_CHARS),
            translate=True,
            for_square=False,
        )
        if not clean_text.strip():
            logger.debug(f"F-06 跳过空正文: {tweet_id}")
            return False, "drop_empty", tweet_data

        if has_personal_link(raw_text):
            logger.info(f"F-07 跳过引流链接: {tweet_id}")
            return False, "drop_personal_link", tweet_data

        if has_promo_or_offtopic(raw_text) or has_promo_or_offtopic(clean_text):
            logger.info(f"F-08 跳过推广/个人情绪: {tweet_id}")
            return False, "drop_promo_or_personal", tweet_data

        x_len = x_weighted_length(clean_text)
        square_len = len(clean_text)

        # 超过广场短帖上限 → 跳过
        if square_len > BINANCE_SQUARE_MAX_CHARS:
            logger.info(
                f"F-09 跳过超广场上限({square_len}>{BINANCE_SQUARE_MAX_CHARS}): {tweet_id}"
            )
            return False, "drop_too_long", tweet_data

        # ≤ X 限制 → 发自己的 X
        if x_len < X_FREE_MAX_CHARS:
            route = "x"
            logger.info(
                f"路由=x id={tweet_id} x_len={x_len} square_len={square_len}"
            )
        else:
            # X 超限但广场可发 → 直接广场+TG
            route = "square_tg"
            logger.info(
                f"路由=square_tg id={tweet_id} x_len={x_len}>={X_FREE_MAX_CHARS} "
                f"square_len={square_len}<={BINANCE_SQUARE_MAX_CHARS}"
            )

        tweet_data["route"] = route
        tweet_data["content_clean"] = clean_text
        tweet_data["content_square"] = protect_cashtags_for_square(clean_text)
        tweet_data["x_weighted_len"] = x_len
        tweet_data["square_len"] = square_len
    else:
        # —— 分发 → 广场 / TG ——
        symbols = get_symbol_names(tweet)
        body = clean_for_distribute(
            raw_text,
            max_length=min(max_content_length, BINANCE_SQUARE_MAX_CHARS),
            extra_symbols=symbols,
        )
        body = ensure_cashtags_present(body, symbols, uppercase=True)

        if not body.strip() and not has_photo:
            logger.debug(f"F-06 跳过空内容: {tweet_id}")
            return False, "drop_empty", tweet_data

        if len(body) > BINANCE_SQUARE_MAX_CHARS:
            logger.info(
                f"F-09 分发超广场上限({len(body)}>{BINANCE_SQUARE_MAX_CHARS}): {tweet_id}"
            )
            return False, "drop_too_long", tweet_data

        tweet_data["content_clean"] = body
        tweet_data["content_square"] = body
        tweet_data["route"] = "square_tg"
        logger.info(
            f"分发正文预览 id={tweet_id} symbols={symbols} body={body[:150]!r}"
        )

    tweet_data["filter_result"] = "pass"
    tweet_data["status"] = "pending"
    logger.info(
        f"推文 {tweet_id} 通过过滤 route={tweet_data.get('route')}: "
        f"{(tweet_data.get('content_clean') or '')[:80]}..."
    )
    return True, "pass", tweet_data
