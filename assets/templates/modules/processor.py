import os
import re
import time
import requests
from loguru import logger

URL_PATTERN = re.compile(
    r"(https?://\S+)|(www\.\S+)|(t\.co/\S+)|(pic\.twitter\.com/\S+)",
    re.IGNORECASE,
)
CASHTAG_PATTERN = re.compile(r"[＄$]\s*([A-Za-z][A-Za-z0-9_]{0,30})")
PLACEHOLDER_FMT = "__CASHTAG{0}__"
PLACEHOLDER_FIND = re.compile(r"__CASHTAG(\d+)__", re.IGNORECASE)
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
def text_has_cashtag(text: str, name: str) -> bool:
    if not text or not name:
        return False
    return (
        re.search(rf"[＄$]\s*{re.escape(name)}\b", text, flags=re.IGNORECASE)
        is not None
    )
def extract_cashtag_names(text: str) -> list:
    if not text:
        return []
    return [m.group(1) for m in CASHTAG_PATTERN.finditer(text.replace("＄", "$"))]
def normalize_cashtags(text: str, uppercase: bool = False) -> str:
    if not text:
        return text
    text = text.replace("＄", "$")

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if uppercase:
            name = name.upper()
        return f"${name} "

    text = CASHTAG_PATTERN.sub(repl, text)
    text = re.sub(r"\$([A-Za-z][A-Za-z0-9_]{0,30})[ \t]+", r"$\1 ", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
def ensure_cashtags_present(text: str, names: list, uppercase: bool = False) -> str:
    """正文缺少 $TOKEN 时补到文首。names 可为 Metis / $Metis。"""
    text = text or ""
    missing = []
    for raw in names or []:
        name = (raw or "").strip().lstrip("$＄").strip()
        if not name:
            continue
        if text_has_cashtag(text, name):
            continue
        missing.append(name.upper() if uppercase else name)

    if not missing:
        return text

    seen = set()
    ordered = []
    for n in missing:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            ordered.append(n)

    prefix = " ".join(f"${n}" for n in ordered) + " "
    logger.warning(f"cashtag 缺失，已补回文首: {ordered}")
    return (prefix + text).strip()
def mask_cashtags(text: str):
    tags = []

    def repl(m):
        tag = "$" + m.group(1)
        tags.append(tag)
        return PLACEHOLDER_FMT.format(len(tags) - 1)

    return CASHTAG_PATTERN.sub(repl, text), tags
def unmask_cashtags(text: str, tags: list) -> str:
    if not text:
        text = ""
    for i, tag in enumerate(tags):
        ph = PLACEHOLDER_FMT.format(i)
        text = text.replace(ph, tag)
        text = re.sub(re.escape(ph), tag, text, flags=re.IGNORECASE)

    def ph_repl(m):
        i = int(m.group(1))
        if 0 <= i < len(tags):
            return tags[i]
        return m.group(0)

    return PLACEHOLDER_FIND.sub(ph_repl, text)
def protect_cashtags_for_square(text: str) -> str:
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
    """采集 → X：保留大小写 $Metis。max_length 仅作软截断；硬性 280 在 filter 里拒绝。"""
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)

    tags = []
    if translate and is_mostly_english(text):
        text, tags = mask_cashtags(text)
        text = translate_to_chinese(text)
        text = unmask_cashtags(text, tags)
        text = ensure_cashtags_present(text, tags, uppercase=False)
        text = strip_urls(text)
        text = strip_contract_addresses(text)
    else:
        tags = ["$" + n for n in extract_cashtag_names(text)]

    text = normalize_cashtags(text, uppercase=False)
    text = ensure_cashtags_present(text, tags, uppercase=False)

    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
        logger.warning("正文超长，已截断")
    return text
def clean_for_distribute(raw_text, max_length=900, for_square=True, extra_symbols=None):
    """
    主号 → 广场 / TG：
    - 统一 $METIS（大写）+ 空格
    - extra_symbols：entities.symbols，强制兜底
    """
    text = strip_urls(raw_text or "")
    text = strip_contract_addresses(text)

    names = list(extract_cashtag_names(text))
    for s in extra_symbols or []:
        s = (s or "").strip().lstrip("$＄")
        if s:
            names.append(s)

    text = normalize_cashtags(text, uppercase=True)
    text = ensure_cashtags_present(text, names, uppercase=True)

    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text
