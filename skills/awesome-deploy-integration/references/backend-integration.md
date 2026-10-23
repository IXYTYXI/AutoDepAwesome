# 后端实施与验收

## 环境与安装

将整个 `awesome-deploy-integration` 文件夹复制到目标 AI 工具支持的技能目录；例如 Codex 的 `$CODEX_HOME/skills`（未设置时为 `~/.codex/skills`）。非 Codex 工具也可直接读取 `SKILL.md` 并导入 Python 助手。Python 3.9+ 标准库即可，无需安装第三方依赖。

客户端构造时从以下受控环境读取配置，无需每次填写占位符：

| 配置 | 默认与覆盖方式 |
| --- | --- |
| 适配服务地址 | `AWESOME_INTAKE_ORIGIN`，默认 `http://127.0.0.1:8099` |
| 内部凭证 | 优先 `AWESOME_INTAKE_TOKEN`；未设置则读取 `AWESOME_INTAKE_TOKEN_FILE` 指向的绝对路径；未设置路径则读 `/etc/awesome-team/intake-token` |
| 搜索 CLI 的共享会话 | `AWESOME_SHARED_SESSION_ID`，必须由可信后端注入，不让最终用户填写身份 |
| 网络超时 | Python 构造参数 `timeout`，默认 10 秒；CLI 为 `--timeout` |

环境变量和凭证文件由运维/密钥管理系统提供，不将真实值写进命令历史、代码、聊天或本 Skill。默认文件权限不足时，请运维给接入服务账号配置只读访问，或将独立凭证文件设为该账号可读（通常 0600）并设置 `AWESOME_INTAKE_TOKEN_FILE`；不 sudo 绕过读取、不打印 key。共享 session 仍由登录过程产生，不能因同机或已有内部凭证跳过登录。

已有授权会话后，只读工具示例（在本 Skill 目录执行）：

```sh
python3 -B scripts/awesome_deploy_client.py search --query demo
python3 -B scripts/awesome_deploy_client.py status example-request-001
python3 -B scripts/awesome_deploy_client.py wait example-request-001 --max-seconds 60
```

CLI 退出码 0 表示查询完成（包括 failed/rejected 任务，须读 status），1 表示接口/输入错误，2 表示等待到期且没有取消任务。CLI 不提供真实会话签发和部署命令，避免把后端认证省略成手填身份；Python 方法支持完整链路。

## 把助手接入真实后端

将 `scripts/awesome_deploy_client.py` 作为模块纳入自己的后端，或在受控 Python 模块路径中引用。下面是框架无关的接线示例；`auth`、`db`、`policy` 是 A 的现有认证、持久化和授权接口，必须由项目实现。助手不验证 OAuth、不代建 A 的数据库、不改变业务逻辑。

```python
import uuid
from awesome_deploy_client import AwesomeDeployClient, WaitTimeout

client = AwesomeDeployClient()

def on_verified_login(server_login):
    # server_login 来自已校验的 OAuth 回调或可信上游会话，不是浏览器 JSON。
    verified = auth.require_verified_feishu_identity(server_login)
    old_sid = db.shared_session_for(server_login.user_id)
    if old_sid:
        client.revoke_session(old_sid)
        db.clear_shared_session(server_login.user_id)
    ttl = min(28800, auth.remaining_lifetime_seconds(server_login))
    shared = client.issue_session({
        "appId": verified.app_id, "tenantKey": verified.tenant_key,
        "openId": verified.open_id, "name": verified.name,
    }, ttl=ttl)
    db.save_shared_session(server_login.user_id, shared["sessionId"], shared["expiresIn"])

def list_candidates(current_login, query, offset=0):
    user = auth.require_login(current_login)
    return client.search_apps(db.shared_session_for(user.id), query=query,
                              limit=20, offset=offset)

def submit_selected_update(current_login, business_request_id, selected_app_id,
                           query, selected_page_offset, ref):
    user = auth.require_login(current_login)
    policy.require_deployment_request(user)
    # selected_app_id 来自用户明确选择；重新搜索核实当前权限和绑定信息。
    page = list_candidates(current_login, query, selected_page_offset)
    selected = next((item for item in page["items"]
                     if item["appId"] == selected_app_id), None)
    if selected is None:
        raise ValueError("选择已失效，请刷新项目列表后重新选择。")
    if not selected["domainBound"]:
        raise ValueError("请先确定可用的新名称并使 awesome.yaml 匹配；不能复用曾用名称。")
    payload = {"appId": selected["appId"], "repository": selected["repository"],
               "subdomain": selected["subdomain"], "ref": ref}
    # get_or_create 必须事务去重并检查同业务申请用户/payload相同。
    # 持久化在网络 POST 前；重试读取原记录，不换 ID 或内容。
    record = db.get_or_create_request(
        business_request_id=business_request_id, user_id=user.id,
        request_id="a-" + uuid.uuid4().hex, deployment=payload)
    policy.require_same_user_and_payload(record, user, payload)
    return client.submit(record.request_id, record.deployment,
                         db.shared_session_for(user.id))

def read_progress(current_login, request_id):
    user = auth.require_login(current_login)
    record = db.require_request(request_id)
    # 当前申请人，或具有本项目部署详情权限的 admin/owner/maintainer；viewer 不够。
    policy.require_request_detail_access(user, record)
    return client.get_request(record.request_id)

def on_logout(current_login):
    user = auth.require_login(current_login)
    sid = db.shared_session_for(user.id)
    if sid:
        client.revoke_session(sid)
        db.clear_shared_session(user.id)
    auth.finish_logout(current_login)
```

新建时无需选既有 appId，但必须有明确新建指令和名称/仓库/ref；以同样事务规则保存记录，deployment 省略 appId。不要将“搜索没找到”直接变成新建。分页 UI 保留 query/limit，nextOffset 为数字才加载下一页；选择结果须按 appId 绑定，不能用数组下标。

可在后端工作线程调用 `wait_for_request(id, max_seconds=60)`；抛出 `WaitTimeout` 时保存其 `request_id` / `last_request`，显示仍在处理中，后续查询原 ID。不要占用网页请求线程无限等待。助手只发送一次 POST；网络错误须按 API 契约区分结果未知和明确拒绝。

## 验证范围

离线执行：

```sh
python3 -B scripts/test_awesome_deploy_client.py
```

测试使用虚构 token/session 和内存 HTTP 替身，不读取默认生产凭证、不连接服务器。覆盖会话→分页搜索→显式 appId 提交→进度→撤销，保持幂等键/内容，拒绝重定向和不安全 origin，保留 401/403/409 等错误，网络超时、不合法响应与有界等待。

真实联调在用户授权的测试环境完成，至少用两个普通用户和管理员核查：

- 甲的 owner 项目不会出现在无授权乙的搜索/total 中，乙猜 appId 更新仍被拒绝；viewer 不能部署；授予 maintainer 后乙可搜索和更新，owner 仍是甲。
- 同一 source 凭证不提升普通用户权限；已禁用或无 can_deploy 的管理员也被拒绝；新建应用归实际用户。
- 同 ID/同人/同内容只生成一个任务；同 ID 换内容/人被拒绝；网络响应丢失后按原 ID 恢复。
- 退出或 session 过期阻止新申请；已受理任务仍能继续；来源/本地账号/应用权限撤销遵循平台规则。
- A 的请求查询不泄露其他用户详情；浏览器/日志无凭证、sessionId、receipt；搜索 503 显示错误，不伪装空结果。
- pending/submitted 不显示已上线；failed/recovery_required/cancelled/rejected 正确结束；等待到期不创建新任务。

仅运行离线测试不能证明生产权限、OAuth 回调、Redis、网络路由或最终部署已经通过。交付时如实列出这些未实测项。
