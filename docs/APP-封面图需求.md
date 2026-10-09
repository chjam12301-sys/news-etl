# 封面图与图片署名 · App 端需求

> 配套接口文档见 [API.md](./API.md) 第五节 · 对应 API v2.7 · 2026-10-09

---

## 1. 一句话

封面图换成 Unsplash 了，**署名必须显示在图上并且可点**，否则违反 Unsplash API 条款、key 会被吊销。

---

## 2. 署名规范（唯一标准格式）

```
Photo by 作者名 on Unsplash
           └─ 链到作者主页      └─ 链到图片详情页
```

**两个词各自是下划线超链接：**

| 片段 | 取值 | 链接字段 |
|---|---|---|
| `Photo by ` | 固定文案 | — |
| 作者名 | `author` | `credit_url` → 摄影师主页 |
| ` on ` | 固定文案 | — |
| 图源名 | `source` | `source_url` → 图片详情页 |

- 整行用后端给的 `credit` 文本即可，App 只需把**作者名**和 **`Unsplash` 这两个词**做成链接；
- 链接 URL **原样使用**，不要去掉或改写 `utm_source` / `utm_medium`（后端已拼好，用于回链统计）；
- 不要翻译成中文、不要改写措辞。

示例渲染：

> Photo by <u>Annie Spratt</u> on <u>Unsplash</u>

---

## 3. 数据契约

`image` 对象，列表页与详情页返回**完全相同**的内容：

| 字段 | 说明 |
|---|---|
| `type` | `photo` = 有实图；`gradient` = 只有渐变块 |
| `url` | 图片直链（hotlink 加载） |
| `provider` | `unsplash` / `pexels` / `wikimedia` / `openverse` |
| `credit` | 署名全文，如 `Photo by Annie Spratt on Unsplash`（兜底用） |
| `author` | 作者名，如 `Annie Spratt` |
| `credit_url` | 摄影师主页（已带 utm） |
| `source` | 图源显示名，如 `Unsplash` |
| `source_url` | 图片详情页（已带 utm） |
| `fallback` | 渐变兜底：`from` / `to` / `label` / `seed` |

`type=gradient` 时没有上面这些，直接用顶层的 `from` / `to` / `label` 画渐变块。

> 旧字段 `image_url`（裸字符串）仍然返回，但**它不含署名**，新代码一律改用 `image`。

---

## 4. 必做的四件事

| # | 必做 | 不做会怎样 |
|---|---|---|
| 1 | **署名可见可点**：`credit` 在图上或紧邻图片，两个词各自带下划线链接 | key 被吊销，封面全部回退占位 |
| 2 | **加载失败切 `fallback`**：`onError` / 超时（建议 8s）后画渐变块 | Unsplash CDN 在部分网络取不到，会白屏 |
| 3 | **不下载转存**：只按 `url` 直接加载 | 违反 hotlink 条款 |
| 4 | **列表与详情用同一个 `image`** | 详情页容易丢署名 |

### 关于缓存

| ✅ 允许 | ❌ 禁止 |
|---|---|
| 系统图片库的标准 HTTP 缓存（含磁盘缓存），TTL ≤ 7 天 | 把图片另存到自建对象存储 / CDN 再引用 |
| 内存缓存（本次会话内复用位图） | 打包进 App 资源或离线包 |
| 列表缩略图追加 imgix 参数降尺寸（如 `w=1600` 改 `w=800`） | 换掉域名、删掉 `ixid` |

---

## 5. 渲染要点

- `type` 只有 `photo` / `gradient` 两值，都要实现；未知取值按 `gradient` 处理，不得白屏或崩溃；
- 封面容器**固定 16:9**，加载中先显示渐变块、图片就绪后淡入，不要先空白后撑开；
- 渐变为 `from → to` 的 135° 线性渐变，叠加 `label`（英文大写主题词），颜色由后端给定、**本地不要重算**；
- 署名超长时单行末尾省略，不换行撑开布局。

---

## 6. 上线顺序（强依赖）

```
App 改造上线  →  后端配置 UNSPLASH_ACCESS_KEY  →  封面切到 Unsplash
```

后端没配 key 时不会产出 Unsplash 图（自动降级到 Wikimedia / Openverse / 渐变块），
所以可以先发 App。反之若先开 key 而 App 还没署名，线上就是「用了图不署名」的违规状态。

---

## 7. 验收清单

- [ ] 列表页不滚动就能看到 `Photo by 作者名 on Unsplash`
- [ ] 点作者名 → 摄影师主页；点 `Unsplash` → 图片详情页；两个 URL 都带 utm
- [ ] 断网 / 弱网时封面变渐变块，不白屏、不无限 loading
- [ ] 抓包确认图片直连 `images.unsplash.com`，App 侧无自建域名副本
- [ ] 列表页与详情页署名完全一致
