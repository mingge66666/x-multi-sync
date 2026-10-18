import os
import re
import time
import requests
from loguru import logger

URL_PATTERN = re.compile(
    r"(https?://\S+)|(www\.\S+)|(t\.co/\S+)|(pic\.twitter\.com/\S+)",
    re.IGNORECASE,
)
CASHTAG_PATTERN = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{0,30})")


def is_mostly_english(text: str) -> bool:
    if not text or not text.strip():
        return False
    letters = sum(1 for c in text if c.isalpha())
    if letters == 0:
        return False
    ascii_letters = sum(1 for c in text if "a" <= c.lower() <= "z")
    return (ascii_letters / letters) > 0.6


def strip_urls(text: str) -> str:
    if not text:
        return ""
    text = URL_PATTERN.sub("", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def protect_cashtags(text: str) -> str:
    """$TOKEN → ＄TOKEN，降低币安展开成合约地址的概率。"""
    if not text:
        return text
    return CASHTAG_PATTERN.sub(r"＄\1", text)


def translate_to_chinese(text: str, max_retries: int = 3) -> str:
    if not text or not text.strip():
        return text
    api_key = os.getenv("DEEPL_API_KEY", "").strip()
    if not api_key:
        logger.warning("未配置 DEEPL_API_KEY，跳过翻译")
        return text
    base = (
        "https://api-free.deepl.com/v2/translate"
        if api_key.endswith(":fx")
        else "https://api.deepl.com/v2/translate"
    )
    headers = {
        "Authorization": f"DeepL-Auth-Key {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"text": [text[:4500]], "source_lang": "EN", "target_lang": "ZH"}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(base, headers=headers, json=payload, timeout=15)
            if resp.status_code in (403, 456):
                logger.error(f"DeepL 返回 HTTP {resp.status_code}")
                return text
            resp.raise_for_status()
            data = resp.json()
            tr = (data.get("translations") or [{}])[0].get("text", "").strip()
            if tr and tr != text.strip():
                tr = strip_urls(tr)
                logger.info(f"翻译完成: {tr[:60]}...")
                return tr
        except Exception as e:
            logger.warning(f"DeepL 第 {attempt} 次尝试失败: {e}")
            time.sleep(2 ** attempt)
    logger.warning("翻译失败，保留原文")
    return text


def process_tweet(raw_text, max_length=900, translate=True, for_square=True):
    """
    去链接、可选翻译、保护 cashtag、截断。
    for_square=True：发广场/需防合约展开时使用。
    """
    text = strip_urls(raw_text or "")
    if translate and is_mostly_english(text):
        text = translate_to_chinese(text)
    text = strip_urls(text)
    if for_square:
        text = protect_cashtags(text)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
        logger.warning("正文超长，已截断")
    return text


def clean_for_distribute(raw_text, max_length=900, for_square=False):
    """
    主号分发：不翻译，只去 t.co/链接；发广场时再保护 cashtag。
    """
    text = strip_urls(raw_text or "")
    if for_square:
        text = protect_cashtags(text)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text
