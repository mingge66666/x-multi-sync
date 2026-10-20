import re
from modules import db
from modules.processor import (
    process_tweet,
    clean_for_distribute,
    protect_cashtags_for_square,
    ensure_cashtags_present,
)
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
    "折扣", "优惠", "名额", "仅剩", "蓝V", "私信", "详询",
    "免费", "白嫖", "只需要", "仅需", "注册",
    "报名", "加微信", "加v", "代理", "招生", "课程", "社群收费",
]

OFFTOPIC_KEYWORDS = [
    "happy birthday", "celebrating my", "enjoying life",
    "wedding", "vacation", "holiday",
    "流量", "运营x", "我的 x", "我的x", "粉丝", "x上", "互动", "创作者",
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


def get_symbol_names(tweet) -> list:
    """entities.symbols → ['Metis', 'Axl']，分发兜底用。"""
    legacy = tweet.get("legacy", {}) or {}
    symbols = (legacy.get("entities") or {}).get("symbols") or []
    names = []
    for s in symbols:
        t = (s.get("text") or "").strip()
        if t:
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
    text = _remove_entity_urls(text, tweet)
    return text.strip()


def filter_tweet(tweet, target_user_id, max_content_length=900, strict_promo=True):
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
        if not has_photo:
            logger.info(f"F-05 跳过纯文字: {tweet_id}")
            return False, "drop_text_only", tweet_data

        clean_text = process_tweet(
            raw_text,
            max_length=max_content_length,
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

        tweet_data["content_clean"] = clean_text
        tweet_data["content_square"] = protect_cashtags_for_square(clean_text)
    else:
        # 分发：正文 + entities.symbols 兜底 → $METIS
        symbols = get_symbol_names(tweet)
        body = clean_for_distribute(
            raw_text,
            max_length=max_content_length,
            extra_symbols=symbols,
        )
        body = ensure_cashtags_present(body, symbols, uppercase=True)

        if not body.strip() and not has_photo:
            logger.debug(f"F-06 跳过空内容: {tweet_id}")
            return False, "drop_empty", tweet_data

        tweet_data["content_clean"] = body
        tweet_data["content_square"] = body
        logger.info(f"分发正文预览: {body[:120]}...")

    tweet_data["filter_result"] = "pass"
    tweet_data["status"] = "pending"
    logger.info(
        f"推文 {tweet_id} 通过过滤: {(tweet_data.get('content_clean') or '')[:80]}..."
    )
    return True, "pass", tweet_data
