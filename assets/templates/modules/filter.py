import re
from modules import db
from modules.processor import process_tweet
from loguru import logger

PERSONAL_LINK_PATTERNS = [
    r"(?i)t\.me/", r"(?i)telegram\.me/", r"(?i)discord\.gg/",
    r"(?i)youtube\.com/", r"(?i)youtu\.be/", r"(?i)instagram\.com/",
    r"(?i)tiktok\.com/", r"(?i)linktr\.ee/", r"(?i)bit\.ly/",
]
PROMO_KEYWORDS = [
    "follow me", "giveaway", "airdrop", "dm me", "join my",
    "link in bio", "subscribe",
]
OFFTOPIC_KEYWORDS = ["happy birthday", "celebrating my", "wedding", "vacation"]


def has_personal_link(text: str) -> bool:
    return any(re.search(p, text or "") for p in PERSONAL_LINK_PATTERNS)


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
    sources = []
    if legacy.get("extended_entities"):
        sources = [legacy["extended_entities"]]
    elif legacy.get("entities"):
        sources = [legacy["entities"]]
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
        return False, "dedup", tweet_data
    if "video" in media_types or "animated_gif" in media_types:
        return False, "drop_video", tweet_data
    if legacy.get("retweeted_status_result") or raw_text.startswith("RT @"):
        tweet_data["tweet_type"] = "retweet"
        return False, "drop_rt", tweet_data
    reply_to_user = legacy.get("in_reply_to_user_id_str", "")
    if reply_to_user and reply_to_user != target_user_id:
        return False, "drop_reply", tweet_data
    if reply_to_user == target_user_id:
        return False, "drop_thread", tweet_data

    clean_text = process_tweet(raw_text, max_length=max_content_length)
    if "photo" in media_types and not clean_text.strip():
        return False, "drop_image_only", tweet_data
    if not clean_text.strip():
        return False, "drop_empty", tweet_data

    if strict_promo:
        if has_personal_link(raw_text):
            return False, "drop_personal_link", tweet_data
        if has_promo_or_offtopic(raw_text) or has_promo_or_offtopic(clean_text):
            return False, "drop_offtopic", tweet_data

    tweet_data["content_clean"] = clean_text
    tweet_data["filter_result"] = "pass"
    tweet_data["status"] = "pending"
    return True, "pass", tweet_data
