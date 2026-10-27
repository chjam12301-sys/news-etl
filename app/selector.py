"""选题打分：从一堆候选里挑「读者愿意点开」的那条。

用户的原话：抓来的主题「都很无聊，或者都太专业，作为中国人读起来离我很远」——
北极冻土、自然指数表格、科学家改论文里的小错误。

这些其实不是翻译问题，是**选题问题**：Nature / Phys.org / Quanta / NIH / WHO
这类机构源写的是科研流程，不是新闻。解决办法是两层：

1. 源权重 —— 编辑部型大众媒体 1.0；机构/学术源 0.35。
2. 标题打分 —— 有具体的人/公司/作品 + 强动词 + 数字 = 好选题；
   "How X are made" / "study" / "paper" / 「一文读懂」= 扣到出局。

2026-10-27 源换成国内媒体后，上面两条判据不变，但正则不能只用英文：
中文标题命中不了英文强动词与专有名词正则，打分信号会整体失效，
所以中文单列一组 CJK_VERBS / CJK_REJECT_PATTERNS。

另加 Google News 热度：一条新闻被越多家媒体同时报道，越可能是当天的大事。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 避免与 fetcher 循环 import
    from .fetcher import RawArticle

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 源权重：大众新闻编辑部 vs 学术/机构源
# --------------------------------------------------------------------------- #
SOURCE_WEIGHT: dict[str, float] = {
    # ---- 国内源（2026-10-27 起）：消费电子 / 数字生活 / 游戏文化 ----
    # 编辑部型、写得像人话的给 1.0；厂商稿与快讯偏多的稍微压一点。
    "ifanr": 1.0, "ithome": 0.95, "sspai": 1.0, "toodaylab": 1.0,
    "tmtpost": 0.9, "gcores": 1.0, "yystv": 0.95, "chuapp": 0.95,
    # ---- 历史海外源：保留权重避免旧数据重算时口径突变 ----
    # 大众新闻（可读性强、受众广）
    "bbc_world": 1.0, "bbc_tech": 1.0, "bbc_business": 1.0, "bbc_science": 1.0,
    "bbc_health": 1.0, "bbc_culture": 1.0, "bbc_sport": 1.0,
    "guardian_world": 1.0, "guardian_tech": 0.95, "guardian_business": 0.95,
    "guardian_science": 0.95, "guardian_culture": 0.9, "guardian_sport": 0.9,
    "aljazeera": 1.0, "cnbc": 0.95, "marketwatch": 0.85, "ft": 0.85,
    "espn": 0.95, "skysports": 0.9,
    "theverge": 0.9, "arstechnica": 0.85, "npr_books": 0.8,
    # 学术 / 机构：内容本身没错，但读者门槛高、离日常远
    "hnrss": 0.55,          # 开发者向，标题常是技术名词
    "nature": 0.35, "phys_org": 0.4, "quanta": 0.4,
    "medical_x": 0.45, "nih": 0.35, "who": 0.4,
}
DEFAULT_WEIGHT = 0.7

# --------------------------------------------------------------------------- #
# 标题规则
# --------------------------------------------------------------------------- #
# 直接出局：解释型、勘误、评论、以及"科研流程本身"当新闻
REJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^\s*(how|why|what|when|where|who)\b", "解释型标题"),
    (r"^\s*(editorial|opinion|comment|analysis|viewpoint|letter|guest)\s*[:：\-]", "评论/社论"),
    (r"\b(correction|corrigendum|erratum|retraction)s?\b", "勘误"),
    (r"\b(a look at|an explainer|explained|what you need to know)\b", "科普/说明"),
    (r"\b(index tables?|nature index|impact factor|citation count)\b", "科研计量"),
    (r"\b(preprint|peer[- ]review|methodology|systematic review)\b", "科研流程"),
    (r"^\s*(the )?(future|promise|challenge)s? of\b", "空泛议题"),
)

# 扣分：学术腔 / 没有实际发生的事
PENALTY_PATTERNS: tuple[tuple[str, float, str], ...] = (
    (r"\b(study|studies|paper|research|researchers?|scientists?|academics?)\b", 0.20, "学术论文腔"),
    (r"\b(journal|preprint|findings?|data set|dataset)\b", 0.15, "科研产物"),
    (r"\b(framework|index|metric|metrics|model|approach|policy brief)\b", 0.12, "抽象名词"),
    (r"\b(needs?|should|must|calls? for|urges?|pledges? to)\b", 0.15, "倡议/表态，无事件"),
    (r"\b(small|slight|minor|tiny|modest|marginal)\b", 0.10, "影响微弱"),
    (r"\b(mistake|error|typo|miscalculation)\b", 0.15, "纠错类"),
    (r"\b(explores?|exploring|investigates?|examines?|assesses?)\b", 0.10, "研究动作而非事件"),
    (r"\b(consortium|institute|university|college)\b", 0.08, "机构事务"),
)

# 强动词：发生了什么，而不是"研究了什么"
STRONG_VERBS = frozenset(
    """
wins won beats beat loses lost cuts cut bans banned lifts launches launched dies died
arrests arrested jailed jails strikes struck sinks sank fires fired quits quit buys bought
sells sold agrees agreed rejects rejected orders ordered warns warned opens opened closes
closed breaks broke hits hit sets set raises raised slashed surges plunges unveils unveiled
sues sued fines fined convicts convicted rescues rescued survives survived marries married
retires retired signs signed announces announced shuts shuts_down stops stopped saves saved
""".split()
)

_PROPER_NOUN = re.compile(r"\b[A-Z][a-z]{2,}\b")
_NUMBER = re.compile(r"\b\d[\d,.]*\s*(%|percent|million|billion|trillion|kg|km|m)?\b")

# --------------------------------------------------------------------------- #
# 中文标题规则（源换成国内媒体后新增）
# --------------------------------------------------------------------------- #
# 英语那套强动词 / 专有名词正则在中文标题上一条都命中不了，等于把打分信号全废掉，
# 所以中文单列一组。判据不变：**具体的人/产品/数字 + 发生了什么**才算好选题。
_CJK = re.compile(r"[一-鿿]")

# 直接出局：多条目汇总（早报/资讯/盘点）当单篇写会散架，说明文与问答体没有事件
CJK_REJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"早报|晚报|晨报|周报|日报|资讯|速递|简报|快报|盘[点货]|汇总|合集|速览", "汇总型栏目"),
    (r"(本周|上周|本月|一周|年度)(行业)?(大事|要闻|新闻|回顾)", "汇总型栏目"),
    (r"(一文读懂|一文看懂|一文了解|一文说清|一图读懂)", "说明文"),
    (r"(如何看待|怎么看待|如何评价|该怎么看|到底是什么|究竟是什么)", "问答体"),
    (r"^(关于|谈谈|说说|聊聊|浅谈)", "随笔"),
)

# 强动词（中文）：标题里出现 = 真的发生了某件事
CJK_VERBS: tuple[str, ...] = (
    "发布", "开售", "首销", "发售", "上线", "下线", "下架", "停服", "关停", "开源",
    "裁员", "扩招", "涨价", "降价", "提价", "召回", "延期", "跳票", "停产", "断货",
    "收购", "并购", "融资", "上市", "退市", "起诉", "胜诉", "败诉", "罚款", "和解",
    "封禁", "解禁", "重制", "复刻", "联名", "签约", "夺冠", "破亿", "爆火", "刷屏",
    "翻车", "泄露", "回应", "道歉", "辟谣", "推出", "亮相", "开卖", "预售", "复出",
    "停更", "开馆", "开展", "撤档", "定档", "重映", "通关",
)

# 作品名（《…》）与拉丁品牌/型号：中文标题里最具体的锚点
_WORK = re.compile(r"《[^》]{1,30}》")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.+]{1,}")

BASE_SCORE = 0.5
ACCEPT_FLOOR = 0.35       # 低于此分认为不适合做听力材料
HEAT_BONUS = 0.05         # 每有 1 家外媒同报 +0.05，最多 +0.3
HEAT_CAP = 6


@dataclass(slots=True)
class Scored:
    raw: Any                # fetcher.RawArticle
    score: float
    reasons: list[str] = field(default_factory=list)
    heat: int = 0
    rejected: str = ""

    @property
    def ok(self) -> bool:
        return not self.rejected and self.score >= ACCEPT_FLOOR


def score_title(title: str, source: str, heat: int = 0) -> tuple[float, list[str], str]:
    """给一条标题打分。返回 (分数, 理由, 出局原因)。"""
    reasons: list[str] = []
    t = (title or "").strip()
    if not t:
        return 0.0, [], "空标题"

    for pat, why in REJECT_PATTERNS:
        if re.search(pat, t, re.I):
            return 0.0, [f"出局：{why}"], why

    if _CJK.search(t):
        for pat, why in CJK_REJECT_PATTERNS:
            if re.search(pat, t):
                return 0.0, [f"出局：{why}"], why

    score = BASE_SCORE
    low = t.lower()

    w = SOURCE_WEIGHT.get(source, DEFAULT_WEIGHT)
    score += (w - 0.7) * 0.5          # 1.0 → +0.15；0.35 → -0.175
    if w >= 1.0:
        reasons.append(f"主流源 +{w}")
    elif w <= 0.45:
        reasons.append(f"学术/机构源 {w}")

    if STRONG_VERBS & set(re.findall(r"[a-z']+", low)):
        score += 0.15
        reasons.append("有强动词 +0.15")

    # 专有名词：去掉首字母（标题首字母几乎总是大写）
    body = t[1:] if len(t) > 1 else ""
    if _PROPER_NOUN.search(body):
        score += 0.12
        reasons.append("有具体人名/地名 +0.12")

    if _CJK.search(t):
        verb = next((v for v in CJK_VERBS if v in t), "")
        if verb:
            score += 0.15
            reasons.append(f"有强动词「{verb}」+0.15")
        if _WORK.search(t):
            score += 0.12
            reasons.append("有具体作品名 +0.12")
        elif _LATIN_TOKEN.search(t):
            score += 0.10
            reasons.append("有品牌/型号名 +0.10")

    if _NUMBER.search(t):
        score += 0.10
        reasons.append("有数字/金额 +0.10")

    for pat, pen, why in PENALTY_PATTERNS:
        if re.search(pat, t, re.I):
            score -= pen
            reasons.append(f"{why} -{pen}")

    if heat:
        bonus = min(heat, HEAT_CAP) * HEAT_BONUS
        score += bonus
        reasons.append(f"外媒同报 {heat} 家 +{bonus:.2f}")

    return round(score, 3), reasons, ""


def score_article(raw: "RawArticle", heat: int = 0) -> Scored:
    score, reasons, rejected = score_title(raw.title, raw.source, heat)
    return Scored(raw=raw, score=score, reasons=reasons, heat=heat, rejected=rejected)


def rank(candidates: list["RawArticle"], heats: dict[str, int] | None = None) -> list[Scored]:
    """打分并排序：**先按「是否被判出局」分层，再按分数**。

    不能只按分数排 —— 出局的稿子分数恒为 0，会排在低分但可用的稿子前面，
    兜底时就把它捞回来了（实测挑中的正是"How Nature Index Tables Are Made"）。
    """
    heats = heats or {}
    out = [score_article(r, heats.get(r.title, 0)) for r in candidates]
    out.sort(key=lambda s: (not s.rejected, s.score), reverse=True)
    return out


def select(
    candidates: list["RawArticle"],
    per_topic: int,
    heats: dict[str, int] | None = None,
) -> tuple[list["RawArticle"], list[Scored]]:
    """挑出最多 per_topic 条。三级兜底：

      ① 合格（未出局且过线）
      ② 未出局但分数低 —— 平庸好过空着
      ③ 只剩出局稿 —— 空一天 App 就没内容，宁可出一条，但要打警告
    """
    scored = rank(candidates, heats)
    ok = [s for s in scored if s.ok]
    if ok:
        picked = ok[:per_topic]
    else:
        usable = [s for s in scored if not s.rejected]
        pool = usable or scored
        picked = pool[:per_topic]
        lvl = "分数未过线" if usable else "全部出局"
        for s in picked:
            log.warning("[select] %s，兜底取用(%.2f)：%s", lvl, s.score, s.raw.title)
            s.reasons.append(f"{lvl}，兜底取用")

    titles = {id(s.raw) for s in picked}
    for s in scored:
        line = "[select] %.2f %s | %s" % (s.score, s.raw.title[:60], "; ".join(s.reasons))
        (log.info if id(s.raw) in titles else log.debug)(line)
    return [s.raw for s in picked], scored
