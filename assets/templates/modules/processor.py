import os
import re
import time
import requests
from loguru import logger

URL_PATTERN = re.compile(
    r"(https?://\S+)|(www\.\S+)|(t\.co/\S+)|(pic\.twitter\.com/\S+)",
    re.IGNORECASE,
)
# 匹配 $BTC、$Metis、$Axl 等（半角 $ 紧跟字母）
CASHTAG_PATTERN = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{0,30})")
CONTRACT_PATTERN = re.compile(
    r"\b(?:ethereum|eth|bsc|arb|arbitrum|polygon|solana|base):0x[a-fA-F0-9]{40}\b",
    re.IGNORECASE,
)


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


def strip_contract_addresses(text: str) -> str:
    if not text:
        return text
    text = CONTRACT_PATTERN.sub("", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_cashtags(text: str) -> str:
    """
    确保 $TOKEN 后面有空格（如果后面紧跟中文/字母/数字）。
    例：$Metis已触底 → $Metis 已触底
    """
    if not text:
        return text

    def repl(m):
        tag = m.group(0)          # $Metis
        end = m.end()
        if end < len(text) and text[end] not in " \n\t.,;:!?，。；：！？":
            return tag + " "
        return tag

    return CASHTAG_PATTERN.sub(repl, text)


def mask_cashtags(text: str):
    tags = []

    def repl(m):
        tags.append(m.group(0))
        return f"⟦C{len(tags) - 1}⟧"

    return CASHTAG_PATTERN.sub(repl, text), tags


def unmask_cashtags(text: str, tags: list) -> str:
    if not text:
        return text
    for i, tag in enumerate(tags):
        text = text.replace(f"⟦C{i}⟧", tag)
        text = text.replace(f"[C{i}]", tag)
        text = text.replace(f"(C{i})", tag)
    return text


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


def process_tweet(raw_text, max_length=900, translate=True, for_square=False):
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)

    tags = []
    if translate and is_mostly_english(text):
        text, tags = mask_cashtags(text)
        text = translate_to_chinese(text)
        text = unmask_cashtags(text, tags)
        text = strip_urls(text)
        text = strip_contract_addresses(text)

    # 不再做全角转换，统一规范化 cashtag（补空格）
    text = normalize_cashtags(text)

    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
        logger.warning("正文超长，已截断")
    return text


def clean_for_distribute(raw_text, max_length=900, for_square=False):
    """主号分发：不翻译，不去全角，只做清理 + 规范化 cashtag。"""
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)
    text = normalize_cashtags(text)   # 关键：保留 $ 并保证后面有空格
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text
