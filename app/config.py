"""全局配置：全部通过环境变量注入，便于在 Render / Docker 上直接跑。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
DEFAULT_DATA_DIR = BASE_DIR / "data"              # 与项目根同级，容器与本地一致


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "每日英语听力 · 内容后台"
    api_prefix: str = "/api/v1"

    # ---- 存储 ----------------------------------------------------------
    # 本地开发用 SQLite；Render 上换 Postgres 只需覆盖 DATABASE_URL。
    # 注意 database_url 是本文件里的字符串，不支持变量引用，因此默认路径在此拼好。
    database_url: str = f"sqlite:///{DEFAULT_DATA_DIR / 'news.db'}"
    data_dir: str = str(DEFAULT_DATA_DIR)

    # ---- LLM -----------------------------------------------------------
    # llm_provider: deepseek | gemini | openrouter | offline
    llm_provider: str = "deepseek"
    # DeepSeek 官方（OpenAI 兼容）。deepseek-chat 即 Flash 档，性价比最高。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat-v3-0324:free"

    # ---- TTS -----------------------------------------------------------
    # edge-tts 免费无鉴权，原生返回逐词时间戳
    tts_enabled: bool = True
    tts_voice_en: str = "en-US-AriaNeural"
    tts_voice_ja: str = "ja-JP-NanamiNeural"

    # ---- 对象存储（Cloudflare R2 / 任意 S3 兼容）----
    # 留空则音频存本地磁盘；配全四项即自动切到 R2。
    # R2 免费额度：10GB 存储/月 + 出网免费
    r2_bucket: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_endpoint: str = ""            # https://<accountid>.r2.cloudflarestorage.com
    r2_public_base: str = ""         # 自定义公开域名，留空则用签名 URL

    # ---- 音频托管（音频不进 Git，避免仓库超 jsDelivr 的 50MB 上限）----
    # 显式指定音频对外基址；留空则依次回退 r2_public_base → b2_public_base
    # → Supabase。全部为空 = 未配置对象存储，发布校验会拦下（不产出坏链接）。
    audio_public_base: str = ""

    # ---- 存储后端选择 ----
    # r2    : Cloudflare R2（S3 兼容）
    # b2    : Backblaze B2（S3 兼容，用 keyId:applicationKey 两段式认证）
    # local : 本地磁盘
    # 注：部分网络环境到 Cloudflare S3 API 域名不可达，故优先 supabase。
    storage_backend: str = ""

    # Backblaze B2（Cloudflare R2 走不通时的替代，10GB 免费）
    b2_bucket: str = ""
    b2_access_key_id: str = ""           # B2 叫 Key ID
    b2_secret_access_key: str = ""       # B2 叫 Application Key
    b2_endpoint: str = ""                # https://s3.us-west-004.backblazeb2.com
    b2_public_base: str = ""             # https://f000.backblazeb2.com/file/<bucket>

    # ---- Supabase Storage（当前首选：Bearer 认证，与 Neon 同家）----
    supabase_project_url: str = ""       # https://xxxx.supabase.co
    supabase_service_key: str = ""       # service_role key（不是 anon key）
    supabase_bucket: str = "news-audio"

    # ---- GitHub 仓库 + jsDelivr CDN（读取 CDN 通、上传 API 不通时的方案）----
    # 内容仓库（需为 public）：owner/name
    github_content_repo: str = ""
    github_branch: str = "main"
    # 自定义 CDN 基址，留空则用 https://cdn.jsdelivr.net/gh/<repo>@<branch>
    github_cdn_base: str = ""
    # 保留天数（GitHub 仓库不宜过大，按此自动清理）
    github_keep_days: int = 14

    # 存储容量硬上限（GB）。免费额度留余量，超限拒绝写入。
    storage_limit_gb: float = 8.0

# ---- 静态 JSON 输出（给 CDN / App 直读）----
    # 开启后每次跑完流水线都会把当天内容导出成 JSON 写到存储里
    export_json: bool = True
    public_base_url: str = ""        # App 端访问 JSON 的基地址，如 https://cdn.example.com

    # ---- 配图（多级图源降级）----
    # 图源优先级，逗号分隔，命中即停；缺 key / 连不上 / 限流的源自动跳下一个。
    # unsplash（需 key）→ pexels（需 key）→ wikimedia（免费）→ openverse（免费）
    image_fetch_enabled: bool = True
    image_providers: str = "unsplash,pexels,wikimedia,openverse"
    # Unsplash：https://unsplash.com/developers 申请 Access Key
    # 走 API 必须 hotlink（不可转存 R2）+ 署名 + 回链带 utm
    unsplash_access_key: str = ""
    unsplash_app_name: str = "daily-english-news"   # utm_source，用英文小写连字符
    # Pexels：https://www.pexels.com/api/ 申请 API Key
    pexels_api_key: str = ""
    # 拿到图片 URL 后探活，过滤死链（Openverse 直链失效率高）
    image_verify_url: bool = True
    image_retries: int = 1
    image_timeout: float = 15.0
    # 是否允许用新闻原图兜底。默认 False —— 商用 App 应保持关闭。
    image_allow_source_fallback: bool = False

    # ---- 抓取 ----------------------------------------------------------
    topics: str = "tech,business,science,health,sports,culture,world"
    articles_per_topic: int = 1
    request_timeout: float = 25.0
    # 选题去重回看天数：库里最近这些天用过的标题不再复用。
    # 太短挡不住「隔天换个说法再写一遍」，太长会把正当的后续报道也挡掉。
    dedup_lookback_days: int = 60
    user_agent: str = (
        "Mozilla/5.0 (compatible; DailyEnglishBot/1.0; +https://example.com/bot)"
    )

    # ---- 业务 ----------------------------------------------------------
    max_words_en: int = 320
    max_words_ja: int = 420

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def audio_dir(self) -> Path:
        p = self.data_path / "audio"
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_path.mkdir(parents=True, exist_ok=True)
    s.audio_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()