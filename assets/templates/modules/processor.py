import os
import re
import time
import requests
from loguru import logger

URL_PATTERN = re.compile(
    r"(https?://\S+)|(www\.\S+)|(t\.co/\S+)|(pic\.twitter\.com/\S+)",
    re.IGNORECASE,
)
# 半角或全角货币符 + 代币名
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


def normalize_cashtags(text: str) -> str:
    """
    统一为币安/X 可识别的 cashtag：
    - 全角 ＄ → 半角 $
    - 格式：$TokenName + 空格（后接中文/字母等非分隔符时）
    例：$Metis已触底 → $Metis 已触底
        ＄Axl表现 → $Axl 表现
    """
    if not text:
        return text

    # 先把孤立全角货币符也改成半角（非 cashtag 场景）
    text = text.replace("＄", "$")

    def repl(m: re.Match) -> str:
        token = m.group(1)
        return f"${token} "

    text = CASHTAG_PATTERN.sub(repl, text)
    # 压缩「$TOKEN  」多空格为单空格，保留换行
    text = re.sub(r"\$([A-Za-z][A-Za-z0-9_]{0,30})[ \t]+", r"$\1 ", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def mask_cashtags(text: str):
    """翻译前保护 cashtag。"""
    tags = []

    def repl(m):
        # 统一存成 $TOKEN
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
    """
    兼容旧调用名：不再转全角。
    改为 normalize_cashtags，保证 $TOKEN + 空格。
    """
    return normalize_cashtags(text or "")


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
    采集发自己的 X：保留半角 $TOKEN，并规范化空格。
    for_square 保留参数兼容；最终都走 normalize_cashtags。
    """
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)

    if translate and is_mostly_english(text):
        text, tags = mask_cashtags(text)
        text = translate_to_chinese(text)
        text = unmask_cashtags(text, tags)
        text = strip_urls(text)
        text = strip_contract_addresses(text)

    # 无论发 X / 广场，都用半角 $TOKEN + 空格
    text = normalize_cashtags(text)

    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
        logger.warning("正文超长，已截断")
    return text


def clean_for_distribute(raw_text, max_length=900, for_square=False):
    """
    主号分发：不翻译；去链接/合约串；统一 $TOKEN 格式。
    for_square 不再改成全角，TG 与广场正文一致。
    """
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)
    text = normalize_cashtags(text)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text
