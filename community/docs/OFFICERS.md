# AI 领航团目录

## 数据路径

浏览器只请求本站 `/api/v1/officers` 和 `/api/v1/officers/media/{id}`。后端先验证本站会话和配置的来源租户，再访问 Redis。飞书应用密钥、应用 access token 和附件 file_token 不返回前端。

目录接口默认将新结果保鲜 300 秒。新鲜标记失效后，一个请求获得 Redis 锁，分页读取配置的飞书视图、完成校验，再通过 Redis 事务同时发布目录和图片索引。其他并发请求最多等待 10 秒；有旧数据则可返回旧数据，没有则返回 `SNAPSHOT_NOT_READY`。刷新总超时 45 秒，锁为 60 秒。

刷新失败保留上一次成功结果，响应 `stale=true`，并暂停回源 15 秒。旧快照从最后一次成功刷新起最多保留 7 天。这不是永久备份，也不是后台定时同步。Redis 本身不可用时返回统一 503，不绕过缓存请求飞书。

目录使用稳定分组：`title` 精确等于 `组织效能团队` 的记录全部在最前，其余记录保持源接口返回顺序。读取字段为「人员」「形象」「个性签名」「团队」「title」「tag」，使用指定视图，不额外发送覆盖视图筛选的 sort/filter。

前端保留按 title 筛选，title 不展示在卡片、详情或分组标题中；组织效能团队始终排在前面，全部成员使用连续网格。支持姓名、团队和标签搜索。浅色背景搭配拍立得卡片，桌面每行四人、手机每行两人；照片、姓名、团队、完整介绍和全部标签均直接展示在卡片上，点击仍可打开详情。当前目录规模为几十人，浏览器在这次已授权获取的列表中筛选，无需重复查询飞书。

## 联系成员

个人详情中的「联系我」使用人员字段的单个 open_id，后端生成固定域名的飞书 AppLink，随目录快照共同缓存。前端只负责打开链接，不查询通讯录、不发送消息。缺少有效标识或一条记录关联多个人时按钮禁用，避免联系错人。已有快照兼容缺少 chat_url 的情况，下一次成功刷新后补全。

链接格式：`https://applink.feishu.cn/client/chat/open?openId=<open_id>`。参见[飞书官方跳转说明](https://open.feishu.cn/community/articles/7271149634339520514)。浏览器可能询问是否打开飞书客户端。

## 图片

- Redis 只存附件索引；图片本体放在 `MEDIA_ROOT/officers/`。
- 首次访问经后端下载飞书附件，使用附件链接中的 extra 鉴权参数。图片统一转 WebP、去元数据、最长尺寸限制为 960 × 1200；最大下载 20 MiB，源图上限 6000 万像素，JPEG 在完整解码前先降采样。
- 文件名由来源配置和附件 token 的 SHA-256 生成。更换附件后产生新 URL，下一次目录刷新即可看到新照片；已有文件原子写入，不会向浏览器交付半张图。
- 多 worker 通过 Redis 限速（此数据源每秒一次新下载）、共享同一照片锁，避免并发重复下载。首次批量加载可能较慢，后续直接读磁盘。
- 图片请求每次验证登录。不要给此路径配置公开静态目录或 Nginx 共享缓存。
- 当前不会自动删除旧版本图片，便于旧快照继续显示。需根据磁盘容量安排后续清理，不能在上线更新时清空整个目录。
- 不支持任意外部图片 URL 或 SVG，来源仅为指定表格的附件。

## 本地开发

```bash
docker compose up -d redis
uv sync --locked
uv run python -m app
```

仓库根目录 `compose.yaml` 复用本机已有的 `redis:6-alpine` 镜像，独立容器 `ows-community-redis-1`，端口仅为 `127.0.0.1:6379`，命名卷 `ows-community_redis-data`。Redis 开启 AOF everysec 和 noeviction；不会改动 Dify 的容器或数据。数据卷需独立备份；不要使用 `docker compose down -v`，它会删除持久化数据。

在 `.env` 配置：

```dotenv
OWS_COMMUNITY_STORE_BACKEND=redis
OWS_COMMUNITY_REDIS_URL=redis://127.0.0.1:6379/0
OWS_COMMUNITY_REDIS_PREFIX=ows-community
OWS_COMMUNITY_FEISHU_BITABLE_APP_TOKEN=<Base app_token，不是 OAuth app_id>
OWS_COMMUNITY_OFFICERS_TABLE_ID=<table_id>
OWS_COMMUNITY_OFFICERS_VIEW_ID=<view_id>
OWS_COMMUNITY_OFFICERS_CACHE_SECONDS=300
OWS_COMMUNITY_OFFICERS_STALE_SECONDS=604800
OWS_COMMUNITY_OFFICERS_SOURCE_TENANT=<可选：来源企业 tenant_key>
OWS_COMMUNITY_MEDIA_ROOT=var/media
```

应用 App ID/Secret 沿用登录配置。需为应用开通 `bitable:app:readonly`，并单独授予源表资源访问权限。个人账号能读表不代表应用 token 能读表。开启高级权限的表需按飞书规则核实数据表权限；返回空数组也不一定代表表中确实无人。含其他敏感表的 Base 应由所有者限制应用范围，或将展示资料放入独立 Base。当前集成不自动修改飞书 ACL。

## 服务器

前端使用相对 API/图片地址，部署时不用替换页面中的主机名。设置站点域名 `SITE_ORIGIN`、`ALLOWED_HOSTS`、`CORS_ORIGINS` 与飞书回调地址，开启 HTTPS 和 Secure Cookie，关闭开发登录及公开文档。Nginx 将全部请求交给后端，参考 `deploy/nginx.conf.example`。

如果 Python 直接运行在服务器上，可继续通过 Docker 发布的本机 6379 连接 Redis。如果 Python 也在同一 Compose 网络中，改用 `redis://redis:6379/0`，无需向公网开放 Redis 端口。远程跨主机 Redis 应使用私网、认证及适当 TLS；本地 compose 示例不用于公网暴露。

把 `MEDIA_ROOT` 设置为独立持久化路径，例如 `/srv/ows-community/media`，并授予后端进程读写权限。更新代码、替换容器时保留此目录和 Redis 数据卷。容器化后端需绑定挂载该目录；多进程可共用同机目录，多台服务器需共用文件系统或后续改为私有对象存储，不能各自使用互不共享的磁盘却共用图片锁。

部署升级不会主动清缓存。会话在 Redis 中保留到过期，目录继续按 TTL 刷新。改变来源配置会使用独立缓存命名空间；未来修改不兼容 schema 时需升级 `officers:v1` 键版本。

## 验证

`uv run pytest -q` 包含缓存命中、过期刷新、并发锁、失败旧数据、原子发布、图片复用和接口鉴权测试。测试使用可控时间与模拟飞书响应；它们不代替应用真实资源权限、照片下载和生产容量验证。

真实联调先确认 Redis 就绪，再通过飞书登录访问 `/officers.html`。没有授权时页面应明确显示读取失败，不使用静态演示资料掩盖错误。

2026-10-21 本机联调通过：应用身份读取 33 位成员（16 个团队），title 为「组织效能团队」的 6 位优先；33 张照片均通过本站 HTTP 接口返回 200，匿名图片请求返回 401。真实 Redis 命中时未调用源接口，使新鲜标记失效后只刷新一次并恢复 300 秒 TTL。已检查 1440px 桌面四列拍立得布局、390px 手机两列、搜索、分组及个人介绍弹窗。自动化测试共 91 项通过。这是功能验收，尚未进行生产负载测试。
