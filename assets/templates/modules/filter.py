import re
from modules import db
from modules.processor import process_tweet
from loguru import logger

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
]

OFFTOPIC_KEYWORDS = [
    "happy birthday", "celebrating my", "enjoying life",
    "wedding", "vacation", "holiday",
]


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


def get_tweet_text(tweet):
    legacy = tweet.get("legacy", {})
    return legacy.get("full_text", "") or tweet.get("text", "")


def filter_tweet(tweet, target_user_id, max_content_length=900, strict_promo=True):
    """
    F-00 去重 | F-01 视频 | F-02 转推 | F-03 回复 | F-04 串帖
    F-05 纯图 | F-06 空内容 | F-07 引流 | F-08 非行情
    strict_promo=False：主号分发，正文保持与 X 一致，不做引流过滤、不翻译。
    """
    tweet_id = tweet.get("rest_id", "") or tweet.get("id_str", "")
    legacy = tweet.get("legacy", {})
    raw_text = get_tweet_text(tweet)
    media_types = get_media_types(tweet)

    tweet_data = {
        "tweet_id": tweet_id,
        "content_raw": raw_text,
        "has_images": 1 if "photo" in media_types else 0,
        "has_video": 1 if ("video" in media_types or "animated_gif" in media_types) else 0,
        "tweet_type": "original",
        "image_urls": get_image_urls(tweet),
    }

    if db.tweet_exists(tweet_id):
        logger.debug(f"F-00 去重跳过: {tweet_id}")
        return False, "dedup", tweet_data

    if "video" in media_types or "animated_gif" in media_types:
        logger.debug(f"F-01 跳过视频/GIF: {tweet_id}")
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
        # 采集阶段：翻译+清理，并过滤引流/非行情
        clean_text = process_tweet(raw_text, max_length=max_content_length)
        if "photo" in media_types and not clean_text.strip():
            logger.debug(f"F-05 跳过纯图无文字: {tweet_id}")
            return False, "drop_image_only", tweet_data
        if not clean_text.strip():
            logger.debug(f"F-06 跳过空内容: {tweet_id}")
            return False, "drop_empty", tweet_data
        if has_personal_link(raw_text):
            logger.info(f"F-07 跳过引流链接: {tweet_id}")
            return False, "drop_personal_link", tweet_data
        if has_promo_or_offtopic(raw_text) or has_promo_or_offtopic(clean_text):
            logger.info(f"F-08 跳过推广/非行情: {tweet_id}")
            return False, "drop_offtopic", tweet_data
else:
    # 主号分发：去掉推文末尾的 t.co 媒体链接，其它尽量与 X 一致
    clean_text = re.sub(r"https?://\S+", "", raw_text or "")
    clean_text = re.sub(r"[^\S\n]+", " ", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
    if len(clean_text) > max_content_length:
        clean_text = clean_text[: max_content_length - 3] + "..."
    if not clean_text.strip() and "photo" not in media_types:
        logger.debug(f"F-06 跳过空内容: {tweet_id}")
        return False, "drop_empty", tweet_data

    tweet_data["content_clean"] = clean_text
    tweet_data["filter_result"] = "pass"
    tweet_data["status"] = "pending"
    logger.info(f"推文 {tweet_id} 通过过滤: {clean_text[:80]}...")
    return True, "pass", tweet_data
