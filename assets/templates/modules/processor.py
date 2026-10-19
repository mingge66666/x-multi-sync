import os
import re
import time
import requests
from loguru import logger

URL_PATTERN = re.compile(
    r"(https?://\S+)|(www\.\S+)|(t\.co/\S+)|(pic\.twitter\.com/\S+)",
    re.IGNORECASE,
)
# 半角或全角 $ + 代币名
CASHTAG_PATTERN = re.compile(r"[＄$]([A-Za-z][A-Za-z0-9_]{0,30})")
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


def normalize_cashtags(text: str, uppercase: bool = False) -> str:
    """
    统一 cashtag：
    - 全角 ＄ → 半角 $
    - 格式：$Name + 空格（后接中文等时尤其需要）
    - uppercase=True（币安/TG）：$METIS 、$AXL
    - uppercase=False（自己的 X）：保留原大小写 $Metis
    """
    if not text:
        return text

    text = text.replace("＄", "$")

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if uppercase:
            name = name.upper()
        return f"${name} "

    text = CASHTAG_PATTERN.sub(repl, text)
    # $TOKEN 后多空格压成一个
    text = re.sub(r"\$([A-Za-z][A-Za-z0-9_]{0,30})[ \t]+", r"$\1 ", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def mask_cashtags(text: str):
    tags = []

    def repl(m):
        tag = "$" + m.group(1)
        tags.append(tag)
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


def protect_cashtags_for_square(text: str) -> str:
    """币安/TG：大写代币名 + 空格。"""
    return normalize_cashtags(text or "", uppercase=True)


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
    """
    采集 → 自己的 X：uppercase=False，保留 $Metis 等形式。
    """
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)

    if translate and is_mostly_english(text):
        text, tags = mask_cashtags(text)
        text = translate_to_chinese(text)
        text = unmask_cashtags(text, tags)
        text = strip_urls(text)
        text = strip_contract_addresses(text)

    text = normalize_cashtags(text, uppercase=False)

    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
        logger.warning("正文超长，已截断")
    return text


def clean_for_distribute(raw_text, max_length=900, for_square=True):
    """
    主号 → 币安 / TG：
    默认 uppercase=True → $METIS 、$AXL
    """
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)
    text = normalize_cashtags(text, uppercase=bool(for_square) if for_square is not None else True)
    # 分发场景统一大写（广场与 TG 相同）
    text = normalize_cashtags(text, uppercase=True)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text
