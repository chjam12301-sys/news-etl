"""LLM 改写层：Gemini / OpenRouter / 离线降级，三种provider 同一接口。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from .config import settings
from .levels import LevelSpec

log = logging.getLogger(__name__)

# LLM 单次改写的最大尝试次数（JSON 格式错误属偶发，重试即可）
_MAX_ATTEMPTS = 3


@dataclass(slots=True)
class RewriteResult:
    level_code: str
    lang: str
    level: int
    level_label: str
    title: str
    paragraphs: list[str]
    vocab: list[dict[str, Any]] = field(default_factory=list)
    lead: str = ""
    # ---- 中文翻译（App 端要的）----
    title_zh: str = ""
    lead_zh: str = ""
    paragraphs_zh: list[str] = field(default_factory=list)
    # True 表示这一份是**离线降级产物**：正文是原文裁剪，没有中文译文。
    # 调用方应据此决定丢弃或重试，避免把「无译文」的坏数据当正常内容发布。
    degraded: bool = False

    @property
    def body(self) -> str:
        return "\n\n".join(p for p in self.paragraphs if p)


SYSTEM_PROMPT = (
    "You are a professional language-learning content editor. You rewrite real "
    "news articles into graded reading material for language learners. "
    "You always reply with a single valid JSON object and nothing else. "
    "No markdown fences, no commentary."
)


def _user_prompt(topic: str, title: str, source_text: str, spec: LevelSpec) -> str:
    lang_name = "English" if spec.lang == "en" else "Japanese (日本語)"
    lang_rule = (
        "Write ENTIRELY in English."
        if spec.lang == "en"
        else "Write ENTIRELY in Japanese (日本語). Do not mix in English except for "
        "proper nouns or when quoting a source term."
    )
    length_unit = (
        "words" if spec.lang == "en"
        else "Japanese characters (including punctuation, excluding whitespace)"
    )
    return f"""SOURCE TOPIC: {topic}
SOURCE LANGUAGE: the original report is most likely 简体中文 (Chinese). If so, read it,
understand what happened, then write your own {lang_name} version — never translate
sentence by sentence, and never keep Chinese phrasing in the output.
TARGET LANGUAGE: {lang_name}
ORIGINAL HEADLINE: {title}

SOURCE TEXT (may be truncated or noisy — infer the facts, never invent new ones):
---
{source_text}
---

TASK
Rewrite the news above for level `{spec.code}` ({spec.label}).

LEVEL RULES
{spec.instruction}
Target length: about {spec.target_words} {length_unit}. Sentence style: {spec.sentence_hint}.

LANGUAGE RULES
{lang_rule}
Keep all names, numbers, dates and places accurate to the source.
Simplify the language, never distort the facts.
For English output, do not use Han characters in `title` / `lead` / `paragraphs`.
For Japanese output, use natural Japanese kanji and kana; do not copy source Chinese sentences.
China-based brands
and products must use their official Latin name — 微信=WeChat, 抖音=Douyin, 小米=Xiaomi,
米家=Mijia, 华为=Huawei, 荣耀=HONOR, 比亚迪=BYD, 大疆=DJI, 美团=Meituan, 拼多多=Pinduoduo,
支付宝=Alipay, 淘宝=Taobao, 京东=JD.com, 哔哩哔哩=Bilibili, 小红书=Xiaohongshu (RED),
蔚来=NIO, 理想=Li Auto, 小鹏=XPeng, 网易=NetEase, 腾讯=Tencent, 阿里巴巴=Alibaba,
爱奇艺=iQIYI, 微博=Weibo. Anything without an official Latin name: use pinyin.

{HEADLINE_RULES}

OUTPUT FORMAT — return exactly this JSON shape:
{{
  "title": "headline rewritten at this level, in {lang_name} (under 90 characters)",
  "lead": "1-3 sentences in {lang_name} that prepare the reader for the article",
  "paragraphs": ["paragraph 1 in {lang_name}", "paragraph 2", "..."],
  "vocab": [
    {{"word": "...", "pos": "noun|verb|adj|adv|phrase", "zh": "Chinese gloss", "note": "short usage note in Chinese, may be empty"}}
  ]
}}

CONSTRAINTS
- paragraphs: {spec.para_min} to {spec.para_max} items, each {spec.para_words[0]}-{spec.para_words[1]} {length_unit}.
- TOTAL length: about {spec.target_words} {length_unit} and NEVER more than {spec.max_words}.
  This is a hard limit — if you have more to say, cut details, do not exceed it.
- vocab: exactly {spec.vocab_count} items, ordered by how useful they are for this level.
- Output raw JSON only."""

HEADLINE_RULES = """\
HEADLINE RULES (journalistic, not academic)
- Lead with the concrete thing that happened and WHO did it: "Nvidia puts a safety
  brain inside robots", not "A study on robotic safety systems".
- Prefer a strong verb in simple past or present: wins, cuts, bans, launches, dies,
  arrests, strikes, opens, breaks, agrees.
- Put the human or well-known name first when there is one.
- Say what CHANGES for the reader. No "How X are made", no "A look at", no
  "Researchers explore", no vague nouns like "study / index / framework".
- Under 90 characters, sentence case, no clickbait, no question marks."""


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
class BaseLLM:
    name = "base"

    async def complete(self, system: str, user: str, *, max_tokens: int = 2400) -> str:
        raise NotImplementedError


class GeminiLLM(BaseLLM):
    name = "gemini"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self) -> None:
        self.key = settings.gemini_api_key
        self.model = settings.gemini_model

    async def complete(self, system: str, user: str, *, max_tokens: int = 2400) -> str:
        url = self.endpoint.format(model=self.model)
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.7,
                "topP": 0.95,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
                for c in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                )
            ],
        }
        headers = {"x-goog-api-key": self.key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=120) as client:
            for attempt in range(4):
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 429 or r.status_code >= 500:
                    wait = 3 * (attempt + 1)
                    log.warning("[llm] %s %s，%ds 后重试", self.name, r.status_code, wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        raise RuntimeError("Gemini 重试耗尽")


class OpenRouterLLM(BaseLLM):
    name = "openrouter"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self) -> None:
        self.key = settings.openrouter_api_key
        self.model = settings.openrouter_model

    async def complete(self, system: str, user: str, *, max_tokens: int = 2400) -> str:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            for attempt in range(4):
                r = await client.post(self.endpoint, json=payload, headers=headers)
                if r.status_code == 429 or r.status_code >= 500:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        raise RuntimeError("OpenRouter 重试耗尽")


class OpenAICompatLLM(BaseLLM):
    """任意 OpenAI 兼容端点（DeepSeek 官方、硅基流动、通义、Kimi 等）。

    相比 OpenRouterLLM 的区别：base_url 与模型名均可配置，便于同一份代码
    切换不同厂商。DeepSeek 官方为 https://api.deepseek.com/v1。
    """

    def __init__(self, name: str, base_url: str, api_key: str, model: str) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key = api_key
        self.model = model

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if not base.endswith("/chat/completions"):
            base = f"{base}/chat/completions"
        return base

    async def complete(self, system: str, user: str, *, max_tokens: int = 2400) -> str:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last_err: str | None = None
        async with httpx.AsyncClient(timeout=150) as client:
            for attempt in range(4):
                try:
                    r = await client.post(self.endpoint, json=payload, headers=headers)
                except Exception as exc:  # 网络类错误也重试
                    last_err = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(3 * (attempt + 1))
                    continue

                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    wait = 4 * (attempt + 1)
                    log.warning("[llm] %s %s，%ds 后重试", self.name, r.status_code, wait)
                    await asyncio.sleep(wait)
                    continue

                if r.status_code == 401:
                    raise RuntimeError(f"[{self.name}] API Key 无效(401)，请检查 DEEPSEEK_API_KEY")

                if r.status_code != 200:
                    raise RuntimeError(f"[{self.name}] HTTP {r.status_code}: {r.text[:300]}")

                data = r.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError(f"[{self.name}] 响应结构异常: {str(data)[:300]}") from exc
                if not content or not content.strip():
                    raise RuntimeError(f"[{self.name}] 返回空内容")
                return content

        raise RuntimeError(f"[{self.name}] 重试耗尽: {last_err}")


class DeepSeekLLM(OpenAICompatLLM):
    """DeepSeek 专用。默认模型 deepseek-chat（Flash 档）。"""

    name = "deepseek"

    def __init__(self) -> None:
        super().__init__(
            name="deepseek",
            base_url=settings.deepseek_base_url or "https://api.deepseek.com/v1",
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model or "deepseek-chat",
        )


def get_llm() -> BaseLLM | None:
    """按配置返回可用 provider；无 key 时返回 None 走离线降级。"""
    provider = (settings.llm_provider or "gemini").lower()

    if provider == "deepseek" and settings.deepseek_api_key:
        return DeepSeekLLM()
    if provider == "gemini" and settings.gemini_api_key:
        return GeminiLLM()
    if provider == "openrouter" and settings.openrouter_api_key:
        return OpenRouterLLM()

    # 自动兜底：配置写错时按「有key 优先」选一个，保证不会静默降级
    if settings.deepseek_api_key:
        return DeepSeekLLM()
    if settings.gemini_api_key:
        return GeminiLLM()
    if settings.openrouter_api_key:
        return OpenRouterLLM()
    return None


# --------------------------------------------------------------------------- #
# JSON 解析
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def parse_json_loose(raw: str) -> dict[str, Any]:
    text = _FENCE.sub("", raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 截取最外层大括号
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    # 出现第二个对象时，取最后一个
    starts = [m.start() for m in re.finditer(r"\{", text)]
    if len(starts) > 1:
        last = starts[-1]
        return json.loads(text[last:])
    raise ValueError("无法解析模型输出为 JSON")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [p.strip() for p in re.split(r"\n\s*\n|\n", value) if p.strip()]
    return []


def _as_vocab(value: Any, lang: str, limit: int) -> list[dict[str, Any]]:
    """规范化 LLM 返回的词表。

    `phonetic` / `en` 一般不来自 LLM（prompt 里没有这两项），留出槽位是为了
    万一模型自发给到时不被丢掉；真正的来源是构建期查 ECDICT（app/dict.py）。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value[:limit]:
        if isinstance(item, str):
            out.append({"word": item, "pos": "", "zh": "", "note": "",
                        "phonetic": "", "en": ""})
            continue
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or item.get("term") or "").strip()
        if not word:
            continue
        out.append(
            {
                "word": word,
                "pos": str(item.get("pos") or "").strip(),
                "zh": str(item.get("zh") or item.get("chinese") or "").strip(),
                "note": str(item.get("note") or "").strip(),
                "phonetic": str(item.get("phonetic") or item.get("ipa") or "").strip(),
                "en": str(item.get("en") or "").strip(),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 离线降级：不等 LLM 也能出结构完整的数据
# --------------------------------------------------------------------------- #
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def _degraded(topic: str, title: str, text: str, spec: LevelSpec) -> RewriteResult:
    """离线降级的统一出口：打上 degraded 标记后返回。"""
    r = offline_rewrite(topic, title, text, spec)
    r.degraded = True
    log.warning("[llm] %s 使用降级内容（无译文）", spec.code)
    return r


def offline_rewrite(topic: str, title: str, text: str, spec: LevelSpec) -> RewriteResult:
    """无 LLM 时的启发式降级：分句 → 按等级截断/裁剪，保证 schema 一致。

    注意：降级产出的正文是**原文裁剪**，没有中文译文（LLM 才产出译文）。
    因此调用方需读RewriteResult.degraded，据此决定是否入库/重试。
    """
    flat = re.sub(r"\s*\n\s*\n\s*", "\n\n", text).strip()
    paras = [p.strip() for p in flat.split("\n\n") if p.strip()] or [title]

    # 原文常整段无空行（尤其抓来的正文），按\n\n 切不开会退化成 1 段
    # 1000+ 字的大段 —— 那既不像该等级的长度，也没有译文，App 侧无法处理。
    # 这里再按单换行/句号二次切分，确保段落粒度可用。
    refined: list[str] = []
    for p in paras:
        if len(p) > 400 and len(_SENT_SPLIT.split(p)) >= 3:
            sents = [s.strip() for s in _SENT_SPLIT.split(p) if s.strip()]
            buf: list[str] = []
            size = 0
            for sent in sents:
                buf.append(sent)
                size += len(sent.split())
                # 每段3~5 句、约 60~110 词，接近各等级目标段长
                if len(buf) >= 4 or size >= 90:
                    refined.append(" ".join(buf))
                    buf, size = [], 0
            if buf:
                refined.append(" ".join(buf))
        else:
            refined.append(p)
    paras = refined or paras

    # 低等级取前 2 段并逐句裁剪；高等级保留更多
    keep_paras = {1: 2, 2: 2, 3: 3, 4: 4, 5: 5}[spec.level]
    paras = paras[:keep_paras]

    out_paras: list[str] = []
    for p in paras:
        sentences = [s.strip() for s in _SENT_SPLIT.split(p) if s.strip()]
        max_sents = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7}[spec.level]
        picked = sentences[:max_sents]
        if not picked:
            continue
        joined = " ".join(picked)
        if spec.level <= 2 and len(joined.split()) > spec.target_words:
            words = joined.split()
            joined = " ".join(words[: spec.target_words])
            # 尽量断在句号
            m = re.search(r"^(.*[.!?。！？])", joined)
            joined = m.group(1) if m else joined + "."
        out_paras.append(joined)

    if not out_paras:
        out_paras = [title]

    vocab = [
        {"word": w, "pos": "", "zh": "", "note": "offline 模式未生成释义",
         "phonetic": "", "en": ""}
        for w in _pick_vocab_words(" ".join(out_paras), spec.lang, spec.vocab_count)
    ]
    lead = out_paras[0].split(".")[0][:120] + "."

    return RewriteResult(
        level_code=spec.code,
        lang=spec.lang,
        level=spec.level,
        level_label=spec.label,
        title=title[:90],
        paragraphs=out_paras,
        vocab=vocab,
        lead=lead,
    )


_TOKEN_RE = re.compile(r"[A-Za-z']{3,}" if False else r"[A-Za-z']{3,}|[぀-ヿ]{2,}|[一-鿿]{2,}")


def _word_count(text: str, lang: str) -> int:
    """英语按空白词计；日语按字符计（没有空格）。"""
    if lang == "ja":
        return len([c for c in text if not c.isspace()])
    return len(re.findall(r"[A-Za-z0-9']+", text))


def _valid_length(paragraphs: list[str], spec: LevelSpec) -> bool:
    """校验最终正文，不机械修改文本。"""
    return (
        spec.para_min <= len(paragraphs) <= spec.para_max
        and sum(_word_count(p, spec.lang) for p in paragraphs) <= spec.max_words
    )


def _japanese_revision_prompt(
    topic: str, title: str, source: str, spec: LevelSpec,
    paragraphs: list[str], attempt: int,
) -> str:
    # Rebuild the prompt with one length budget and leave more room on the final try.
    budget_percent = 80 if attempt == 1 else 65
    budget = min(spec.target_words, spec.max_words * budget_percent // 100)
    average_para = sum(spec.para_words) / 2
    count = max(spec.para_min, min(spec.para_max, round(budget / average_para)))
    retry_spec = replace(
        spec, target_words=budget, hard_max_words=budget,
        para_min=count, para_max=count, para_words=(1, budget // count),
    )
    actual = sum(_word_count(p, spec.lang) for p in paragraphs)
    return _user_prompt(topic, title, source, retry_spec) + (
        f"\n\nREVISION REQUIRED. The prior draft had {actual} Japanese characters. "
        f"Write a new complete article in EXACTLY {count} paragraphs with no more than "
        f"{budget} Japanese characters in total. Count each kanji, kana, Latin letter, "
        "digit and punctuation mark as one character. Keep the core news event and a "
        "complete ending. Omit secondary facts and background. Return valid JSON."
    )


def _revision_prompt(original: str, paragraphs: list[str], spec: LevelSpec) -> str:
    """把实测超限量反馈给 LLM，用更保守的预算重写整篇文章。"""
    unit = "Japanese characters (including punctuation, excluding whitespace)" if spec.lang == "ja" else "English words"
    actual = sum(_word_count(p, spec.lang) for p in paragraphs)
    # 模型尤其难以准确计算日语字数；故留足余量，不要求卡着硬上限。
    safe_total = min(spec.target_words, int(spec.max_words * 0.80))
    average_para = (spec.para_words[0] + spec.para_words[1]) / 2
    num_paras = max(spec.para_min, min(spec.para_max, round(safe_total / average_para)))
    per_para = safe_total // num_paras
    return (
        original
        + "\n\nREVISION REQUIRED. The prior draft has "
        + f"{actual} {unit} in {len(paragraphs)} paragraphs; "
        + f"the hard limit is {spec.max_words} {unit} and the allowed paragraph "
        + f"count is {spec.para_min}-{spec.para_max}. "
        + "Do NOT just remove the last sentence. Write a NEW shorter, "
        + "coherent news article with an ending and the most important facts. "
        + f"Aim for EXACTLY {num_paras} paragraphs, each under {per_para} {unit}, "
        + f"and no more than {safe_total} {unit} TOTAL across all paragraphs. "
        + "This is a deliberately conservative working budget; it takes priority "
        + "over the initial approximate length target. "
        + "For Japanese count every kanji, kana, Latin letter, digit and "
        + "punctuation mark as one character. "
        + "Output the full requested JSON with source-language paragraphs only; "
        + "do not include Chinese translations. Return valid JSON. "
        + "Previous draft: "
        + json.dumps(paragraphs, ensure_ascii=False)
    )


def _pick_vocab_words(text: str, lang: str, limit: int) -> list[str]:
    """按词频挑候选词，供 LLM 或离线模式填 vocab。"""
    from collections import Counter

    if lang == "ja":
        cands = re.findall(r"[぀-ヿ]{2,}|[一-鿿]{2,}", text)
    else:
        cands = [w.lower() for w in re.findall(r"[A-Za-z']{3,}", text)]
    stop = {
        "the", "and", "that", "have", "for", "not", "with", "you", "this", "but",
        "his", "from", "they", "which", "will", "was", "were", "are", "been", "has",
        "had", "said", "says", "after", "more", "about", "their", "there", "what",
        "said", "its", "our", "out", "year", "years", "over", "into", "than", "who",
        "would", "could", "also", "new", "one", "two", "first", "last", "we", "us",
    }
    cands = [c for c in cands if c not in stop]
    return [w for w, _ in Counter(cands).most_common(limit)]


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
async def rewrite_article(
    topic: str, title: str, text: str, spec: LevelSpec, llm: BaseLLM | None = None
) -> RewriteResult:
    """把一篇原文改写成指定等级。失败自动降级到离线模式。"""
    source = text[:7000]
    llm = llm or get_llm()
    if llm is None:
        log.info("[llm] 无可用 key，离线降级: %s", spec.code)
        return _degraded(topic, title, source, spec)

    user = _user_prompt(topic, title, source, spec)
    prompt = user
    data: dict[str, Any] | None = None
    rejected_paragraphs: list[str] | None = None

    # 第一阶段：让模型生成合格正文；失败时让模型完整重写。
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if spec.lang == "ja" and rejected_paragraphs is not None:
            prompt = _japanese_revision_prompt(
                topic, title, source, spec, rejected_paragraphs, attempt - 1,
            )
        try:
            candidate = parse_json_loose(await llm.complete(SYSTEM_PROMPT, prompt))
            paragraphs = _as_list(candidate.get("paragraphs"))
            if _valid_length(paragraphs, spec):
                data = candidate
                break
            total = sum(_word_count(p, spec.lang) for p in paragraphs)
            log.warning(
                "[llm] %s 第 %d/%d 次不合格：正文 %d/%d，段落 %d（要求 %d-%d）",
                spec.code, attempt, _MAX_ATTEMPTS, total, spec.max_words,
                len(paragraphs), spec.para_min, spec.para_max,
            )
            rejected_paragraphs = paragraphs
            if spec.lang != "ja" and attempt < _MAX_ATTEMPTS:
                prompt = _revision_prompt(user, paragraphs, spec)
        except Exception as exc:  # noqa: BLE001
            log.warning("[llm] %s 第 %d/%d 次生成失败: %s",
                        spec.code, attempt, _MAX_ATTEMPTS, exc)

    if data is None:
        return _degraded(topic, title, source, spec)

    paragraphs = _as_list(data["paragraphs"])
    title_out = str(data.get("title") or title).strip()[:120]
    lead = str(data.get("lead") or "").strip()[:400]

    # 第二阶段：仅根据定稿正文生成中文，防止译文保留已删除的信息。
    translation_prompt = (
        "Translate ONLY the following final graded article into natural Simplified Chinese. "
        "Do not add any facts, numbers, explanations, or sentences. "
        "Return a JSON object containing title_zh, lead_zh and paragraphs_zh. "
        "Each Chinese paragraph must correspond to exactly one source paragraph. "
        "Do not rewrite the source-language article.\n"
        + json.dumps({"title": title_out, "lead": lead, "paragraphs": paragraphs},
                     ensure_ascii=False)
    )
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            translated = parse_json_loose(
                await llm.complete(SYSTEM_PROMPT, translation_prompt)
            )
            zh_paras = _as_list(translated.get("paragraphs_zh"))
            title_zh = str(translated.get("title_zh") or "").strip()
            lead_zh = str(translated.get("lead_zh") or "").strip()
            if (len(zh_paras) == len(paragraphs) and title_zh
                    and (not lead or lead_zh)):
                return RewriteResult(
                    level_code=spec.code, lang=spec.lang, level=spec.level,
                    level_label=spec.label, title=title_out, paragraphs=paragraphs,
                    vocab=_as_vocab(data.get("vocab"), spec.lang, spec.vocab_count),
                    lead=lead, title_zh=title_zh[:120], lead_zh=lead_zh[:400],
                    paragraphs_zh=zh_paras,
                )
            raise ValueError("中文译文缺失或段落数量不匹配")
        except Exception as exc:  # noqa: BLE001
            log.warning("[llm] %s 第 %d/%d 次翻译失败: %s",
                        spec.code, attempt, _MAX_ATTEMPTS, exc)

    return _degraded(topic, title, source, spec)


async def rewrite_all(
    topic: str, title: str, text: str, specs: list[LevelSpec], llm: BaseLLM | None = None
) -> list[RewriteResult]:
    """一次文章 → 10 个等级。并发执行，llm=None 时逐个降级。"""
    llm = llm or get_llm()

    async def run(spec: LevelSpec) -> RewriteResult:
        # 离线模式不并发，避免无意义占用；LLM 模式并发 3 路以避开免费额度限流
        if llm is None:
            return _degraded(topic, title, text, spec)
        sem = asyncio.Semaphore(3)
        async with sem:
            return await rewrite_article(topic, title, text, spec, llm)

    results = await asyncio.gather(*(run(s) for s in specs), return_exceptions=True)
    out: list[RewriteResult] = []
    for spec, res in zip(specs, results):
        if isinstance(res, BaseException):
            log.warning("[llm] %s 异常: %s", spec.code, res)
            out.append(_degraded(topic, title, text, spec))
        else:
            out.append(res)
    return out