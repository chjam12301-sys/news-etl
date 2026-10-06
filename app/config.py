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

    # ---- 存储后端选择 ----
    # r2    : Cloudflare R2（S3 兼容）
    # b2    : Backblaze B2（S3 兼容，用 keyId:applicationKey 两段式认证）
    # local : 本地磁盘
    # 注：部分网络环境到 Cloudflare S3 API 域名不可达，此时用 b2。
    storage_backend: str = ""

    # Backblaze B2（Cloudflare R2 走不通时的替代，10GB 免费）
    b2_bucket: str = ""
    b2_access_key_id: str = ""           # B2 叫 Key ID
    b2_secret_access_key: str = ""       # B2 叫 Application Key
    b2_endpoint: str = ""                # https://s3.us-west-004.backblazeb2.com
    b2_public_base: str = ""             # https://f000.backblazeb2.com/file/<bucket>

    # 存储容量硬上限（GB）。免费额度留余量，超限拒绝写入。
    storage_limit_gb: float = 8.0

# ---- 静态 JSON 输出（给 CDN / App 直读）----
    # 开启后每次跑完流水线都会把当天内容导出成 JSON 写到存储里
    export_json: bool = True
    public_base_url: str = ""        # App 端访问 JSON 的基地址，如 https://cdn.example.com

    # ---- 配图 ----
    # 方案 B：统一走 Openverse（真 CC0 / Public Domain Mark），不用原图，授权最干净。
    # 找不到时退回渐变占位块（见 images.placeholder）。
    image_fetch_enabled: bool = True

    # ---- 抓取 ----------------------------------------------------------
    topics: str = "tech,business,science,health,sports,culture,world"
    articles_per_topic: int = 1
    request_timeout: float = 25.0
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