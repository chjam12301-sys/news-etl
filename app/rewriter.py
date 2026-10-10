"""LLM 改写层：Gemini / OpenRouter / 离线降级，三种provider 同一接口。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import settings
from .levels import LevelSpec

log = logging.getLogger(__name__)

# LLM 单次改写的最大尝试次数（JSON 格式错误属偶发，重试即可）
_MAX_ATTEMPTS = 2


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
    return f"""SOURCE TOPIC: {topic}
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
Target length: about {spec.target_words} words. Sentence style: {spec.sentence_hint}.

LANGUAGE RULES
{lang_rule}
Keep all names, numbers, dates and places accurate to the source.
Simplify the language, never distort the facts.

{HEADLINE_RULES}

OUTPUT FORMAT — return exactly this JSON shape:
{{
  "title": "headline rewritten at this level, in {lang_name} (under 90 characters)",
  "title_zh": "同一标题的中文翻译（口语化，不要直译腔）",
  "lead": "1-3 sentences in {lang_name} that prepare the reader for the article",
  "lead_zh": "lead 的中文翻译",
  "paragraphs": ["paragraph 1 in {lang_name}", "paragraph 2", "..."],
  "paragraphs_zh": ["与 paragraphs 一一对应的中文翻译", "..."],
  "vocab": [
    {{"word": "...", "pos": "noun|verb|adj|adv|phrase", "zh": "Chinese gloss", "note": "short usage note in Chinese, may be empty"}}
  ]
}}

ZH REQUIREMENTS
- `title_zh` / `lead_zh` / `paragraphs_zh` 必须是**简体中文**。
- 段落数必须与 `paragraphs` 完全一致，一一对应。
- 翻译要自然、地道，像中文媒体写出来的，不要逐词硬译。

CONSTRAINTS
- paragraphs: {spec.para_min} to {spec.para_max} items, each {spec.para_words[0]}-{spec.para_words[1]} words.
- TOTAL length: about {spec.target_words} words and NEVER more than {spec.max_words}.
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


def _split_sentences(text: str, lang: str) -> list[str]:
    sep = r"(?<=[。！？])" if lang == "ja" else r"(?<=[.!?])\s+"
    return [s.strip() for s in re.split(sep, text) if s.strip()]


def _trim_to_limit(
    paragraphs: list[str], zh_paras: list[str], spec: LevelSpec
) -> tuple[list[str], list[str]]:
    """把正文逐句裁到 spec.max_words 以内，中译同步裁。

    为什么必须裁：只靠 prompt 写「about N words」没用 —— 实测 A1 产出
    116~230 词。裁的时候按整句删，避免留下半句话；至少保留 para_min 段。
    """
    joiner = "" if spec.lang == "ja" else " "
    limit = spec.max_words
    before = sum(_word_count(p, spec.lang) for p in paragraphs)
    if before <= limit:
        return paragraphs, zh_paras

    # 扁平化成带段落号的句子，从尾部逐句删 —— 只裁最后一段是裁不动的
    per = [_split_sentences(p, spec.lang) or [p] for p in paragraphs]
    flat = [(i, s) for i, ss in enumerate(per) for s in ss]
    min_paras = min(spec.para_min, len(per))

    # 优先「段落数够 + 不超长」；若做不到，则退而求其次「不超长 + 至少 1 段」
    # —— 长度是硬指标，段数只是版式偏好。
    best: list[str] | None = None
    loose: list[str] | None = None
    while flat:
        flat.pop()
        groups: dict[int, list[str]] = {}
        for i, s in flat:
            groups.setdefault(i, []).append(s)
        rebuilt = [joiner.join(groups[i]) for i in sorted(groups) if groups[i]]
        if not rebuilt:
            break
        if sum(_word_count(p, spec.lang) for p in rebuilt) > limit:
            continue
        if len(rebuilt) >= min_paras:
            best = rebuilt
            break
        if loose is None:
            loose = rebuilt

    best = best or loose
    if best is None:
        best = list(paragraphs)

    # 兜底：没有句读可依（整段一个长句）时按词/字硬截，保证不破硬上限
    over = sum(_word_count(p, spec.lang) for p in best) - limit
    if over > 0:
        rest = sum(_word_count(p, spec.lang) for p in best[:-1])
        room = max(limit - rest, 1)
        last = best[-1]
        if spec.lang == "ja":
            best[-1] = "".join([c for c in last if not c.isspace()][:room])
        else:
            best[-1] = " ".join(re.findall(r"[A-Za-z0-9']+", last)[:room])

    log.info("[llm] %s 超长已裁剪：%d → %d 词（段 %d → %d，上限 %d）",
             spec.code, before,
             sum(_word_count(p, spec.lang) for p in best),
             len(paragraphs), len(best), limit)
    return best, zh_paras[: len(best)] if zh_paras else []


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

    # LLM 返回的 JSON 偶发格式错误（未转义引号、截断），重试一次通常就好。
    # 之前是失败即降级 —— 会静默产出「无译文」的内容，App 侧无法感知。
    data: dict[str, Any] | None = None
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = await llm.complete(SYSTEM_PROMPT, user)
            data = parse_json_loose(raw)
            if _as_list(data.get("paragraphs")):
                break
            last_exc = ValueError("返回空正文")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("[llm] %s 第 %d/%d 次解析失败: %s",
                        spec.code, attempt, _MAX_ATTEMPTS, exc)
            data = None
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(1.5 * attempt)

    if data is None:
        log.warning("[llm] %s 改写最终失败(%s)，降级离线", llm.name, last_exc, spec.code)
        return _degraded(topic, title, source, spec)

    paragraphs = _as_list(data.get("paragraphs"))[:8]
    if not paragraphs:
        log.warning("[llm] %s 返回空正文，降级离线", spec.code)
        return _degraded(topic, title, source, spec)

    title_out = str(data.get("title") or title).strip()[:120]

    # 中文翻译：段落数必须与原文一致，否则丢弃（宁可没有也不要错位）
    zh_paras = _as_list(data.get("paragraphs_zh"))[:len(paragraphs)]
    if len(zh_paras) != len(paragraphs):
        log.warning("[llm] %s 中译段落数不符(%d vs %d)，丢弃中文字段",
                    spec.code, len(zh_paras), len(paragraphs))
        zh_paras = []
        # 再试一次：段数不符常因正文尾部被截断，重试能拿到完整的两份
        if _MAX_ATTEMPTS > 1:
            try:
                raw2 = await llm.complete(SYSTEM_PROMPT, user)
                d2 = parse_json_loose(raw2)
                p2 = _as_list(d2.get("paragraphs"))[:8]
                z2 = _as_list(d2.get("paragraphs_zh"))[:len(p2)]
                if p2 and len(z2) == len(p2) and d2.get("title_zh"):
                    log.info("[llm] %s 重试成功，译文已对齐", spec.code)
                    paragraphs, zh_paras = p2, z2
                    data = d2
            except Exception as exc:  # noqa: BLE001
                log.warning("[llm] %s 重试仍失败: %s", spec.code, exc)

    # 硬上限裁剪：模型几乎总超出目标（A1 实测 116~230 词，而目标只有 110）。
    # 逐句裁到上限内，中译同步裁，保证段落仍一一对应。
    paragraphs, zh_paras = _trim_to_limit(paragraphs, zh_paras, spec)

    return RewriteResult(
        level_code=spec.code,
        lang=spec.lang,
        level=spec.level,
        level_label=spec.label,
        title=title_out,
        paragraphs=paragraphs,
        vocab=_as_vocab(data.get("vocab"), spec.lang, spec.vocab_count),
        lead=str(data.get("lead") or "").strip()[:400],
        title_zh=str(data.get("title_zh") or "").strip()[:120],
        lead_zh=str(data.get("lead_zh") or "").strip()[:400],
        paragraphs_zh=zh_paras,
    )


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
            out.append(offline_rewrite(topic, title, text, spec))
        else:
            out.append(res)
    return out