# AIHOT 资讯同步

## 已接入的数据链路

AIHOT 官方 `api/v1/selected` → 后端同步任务 → 飞书 AIHOT 表 → 共享 Redis 快照 → 鉴权接口 → Vue。

表地址：https://guanghe.feishu.cn/wiki/Rg2Dw2MLii1fpNkma0EcFSIWn4e?table=tblqz0b1wl9l4Kl0&view=vewapFusom

只同步 AIHOT 的精选集合，保留原始来源和 AIHOT 署名，不抓取文章全文。首轮导入时保留了表中原有的 5 条空行；空行不展示。接口文档：https://aihot.news/agent?tab=api 。

## 飞书字段

| 用途 | 字段 | 维护方式 |
|---|---|---|
| 内容 | 标题、原标题、摘要、原始来源 | AIHOT 自动同步 |
| 分类 | 分类、分类代码 | AIHOT 自动同步；分类转为中文，未知分类归入其他 |
| 链接与署名 | 原文链接、AIHOT链接、来源署名、来源署名链接 | AIHOT 自动同步 |
| 时间与评分 | 原文发布时间、发现时间、来源评分 | AIHOT 自动同步；原文时间和评分允许空 |
| 同步标识 | AIHOT ID、同步指纹、上游精选、同步状态、同步时间 | 系统管理，请勿手动更改 |
| 人工运营 | 封面、人工标题、人工摘要、隐藏、首页推荐、排序权重 | 在飞书中维护，自动同步不覆盖 |

- 人工标题/人工摘要非空时优先展示，清空后恢复上游内容。
- 勾选隐藏后从网站隐藏；未勾选隐藏且仍为上游精选的记录才能展示。
- 首页最多 7 条，优先首页推荐，再取其余资讯。列表按排序权重降序、原文发布时间（为空取发现时间）降序。
- 上游撤选只标记为已撤选，不删除飞书记录，不继续在网站展示。
- 封面为飞书附件。无封面时保留文字列表；有封面时经后端鉴权下载、转 WebP、按文件 token 哈希缓存到磁盘。前端不访问飞书图片临时链接。更换附件后生成新的图片地址。
- 24 个字段的创建定义见 `news-fields.json`（不含重命名为标题的原主字段）。

## 增量、重试和调度

1. 无游标时分页读取完整精选快照（每页 100 条），保留第一页返回的增量游标；全量读取完成后写表并核对撤选。
2. 此后从 `/changes` 处理 upsert/remove，每页写入成功后才原子保存游标。AIHOT ID 唯一匹配、指纹跳过未变内容。网络结果不明确时不盲重试写入，下轮先读表，避免重复创建。
3. HTTP 409 自动重建快照；304 无上游变化时也重新读取飞书，以更新人工运营字段。
4. 每轮成功后再次从飞书读取，原子替换 Redis 数据、时效标记及图片索引。完整成功后休眠至下一轮计划时间（默认 600 秒），不再每 15 秒轮询。无上游变更时只读一次飞书刷新缓存；有变更时先读表比对写入，再读表发布完整快照。
5. 网络读取最多重试 3 次；429/503/566/567 不立即重试，遵守 Retry-After（未给出则 600 秒）。普通失败等待 600 秒；不改变 UA 绕过限制。
6. Redis 分布式同步锁防止多 worker 重复同步，并与缓存读取锁互斥。单轮最多 840 秒，锁 900 秒。中途写表失败会留下 pending 标记，只展示上次完整快照，待下一轮修复后再发布。

服务运行时任务才会执行；笔记本休眠/关机期间不会同步。远程部署请用 systemd 或容器重启策略托管后端进程。

## Redis 与鉴权

`news:v1:<来源哈希>` 统一缓存，不按用户复制，实际还带项目配置的 Redis prefix。

- `:fresh`：600 秒，固定 TTL，不因访问续期。
- `:snapshot`、`:media:<hash>`：86400 秒，供上游故障时返回上一版（`stale=true`）。超过保留期且恢复失败返回统一错误，不伪造空列表。
- `:sync:next`：下轮计划时间；`:sync:status`：最近同步状态和新增/更新/可见数量。
- `:sync:pending`：写表尚未完整发布，成功后清除；`:sync:lock` 和 `:lock` 为租约。

`GET /api/v1/news`、`GET /api/v1/news/media/{id}` 均要求本站身份，配置来源租户时也校验租户。无登录返回 401。前端首页、资讯列表、详情页都只请求本站后端。

## 配置与部署

代码默认值、`.env.example` 及本机 `.env` 均默认开启任务。修改项目根目录 `.env` 中 `OWS_COMMUNITY_NEWS_SYNC_ENABLED=false` 可关闭，改回 `true` 可开启；修改后重启后端。启用时必须配置 Redis 和飞书目标表，否则启动会提示配置错误。任务使用 FastAPI 生命周期中的单个异步协程，复用 HTTP 连接池，不引入 Celery、APScheduler 或额外进程；服务退出时取消任务并释放锁。

配置示例：

```dotenv
OWS_COMMUNITY_STORE_BACKEND=redis
OWS_COMMUNITY_NEWS_TABLE_ID=tblqz0b1wl9l4Kl0
OWS_COMMUNITY_NEWS_VIEW_ID=vewapFusom
OWS_COMMUNITY_NEWS_SYNC_ENABLED=true
OWS_COMMUNITY_NEWS_SYNC_INTERVAL_SECONDS=600
OWS_COMMUNITY_NEWS_CACHE_SECONDS=600
OWS_COMMUNITY_NEWS_STALE_SECONDS=86400
OWS_COMMUNITY_NEWS_SYNC_STATE_ROOT=var/news-sync
OWS_COMMUNITY_MEDIA_ROOT=var/media
```

复用既有 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE_APP_TOKEN，运行时使用应用身份 `bitable:app` 写入，不使用个人 CLI 身份。应用需具有目标表的读写访问权。`OWS_COMMUNITY_NEWS_SOURCE_TENANT` 配置为本组织租户。

服务器保留 `var/news-sync`（游标）和 `var/media`（图片），Redis 持久化卷也需保留。单服务器多个 worker 共享同一游标目录和 Redis。若部署多个服务器副本，只在一个副本开启同步；其余副本关闭同步并共用 Redis，图片目录使用共享持久卷。不要让多个不同游标目录的实例同时同步。

常规一键启动：

```sh
uv run python -m app
```

立即执行一轮同步（仍有分布式互斥锁）：

```sh
uv run python -m scripts.sync_news
```

该命令强制忽略计划执行时间，仅用于人工维护；自动任务始终遵守失败退避。更新 `.env` 后需要重启后端。
