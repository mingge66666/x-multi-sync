import os
import re
import time
import requests
from loguru import logger

URL_PATTERN = re.compile(r"https?://\S+")


def is_mostly_english(text: str) -> bool:
    if not text or not text.strip():
        return False
    letters = sum(1 for c in text if c.isalpha())
    if letters == 0:
        return False
    ascii_letters = sum(1 for c in text if "a" <= c.lower() <= "z")
    return (ascii_letters / letters) > 0.6


def translate_to_chinese(text: str, max_retries: int = 3) -> str:
    if not text or not text.strip():
        return text
    api_key = os.getenv("DEEPL_API_KEY", "").strip()
    if not api_key:
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
                logger.error(f"DeepL HTTP {resp.status_code}")
                return text
            resp.raise_for_status()
            data = resp.json()
            tr = (data.get("translations") or [{}])[0].get("text", "").strip()
            if tr and tr != text.strip():
                return tr
        except Exception as e:
            logger.warning(f"DeepL 第 {attempt} 次失败: {e}")
            time.sleep(2 ** attempt)
    return text


def process_tweet(raw_text, max_length=900, translate=True):
    text = URL_PATTERN.sub("", raw_text or "")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if translate and is_mostly_english(text):
        text = translate_to_chinese(text)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def process_tweet_as_is(raw_text, max_length=900):
    """分发阶段：不翻译、不去链接（主号内容原样）。"""
    text = (raw_text or "").strip()
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text
