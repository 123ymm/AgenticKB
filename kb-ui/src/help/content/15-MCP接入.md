# 15 · MCP 接入

页面：`/mcp`（MCP 接入）｜所有人可用

MCP 是外部 AI Agent（dify / 扣子 / Claude 等）连接平台知识库的入口。**每个 Agent 一把钥匙，一把钥匙一个知识域**；一个用户最多 10 把钥匙，各钥匙的工具、开放库、提示词互不影响。

## 前提（三样）

1. **平台账号**且已绑定知识域（未绑定域无法创建钥匙，页面会提示联系管理员）；
2. 目标库对你本人有权限（被拥有者加为成员或库是 public）——钥匙只能放大你已有的权限；
3. **网络可达**：Agent 所在机器能访问 `http://<服务器IP>:9000/mcp`（该端口供外部直连；连不通先查防火墙/安全组放行 9000）。

> **客户端要求**：平台的 MCP 只认 `Authorization: Bearer kbm_…` 请求头——你的 MCP 客户端必须支持自定义请求头（Claude Code/Claude Desktop、dify、扣子均支持；只让填 URL 不让加头的客户端接不通）。

## 接入四步

1. **创建密钥**：命名（如"客服机器人"）→ 创建 → **明文仅此一次显示，关闭后无法再查看，请立即复制**（`kbm_` 开头，上限 10 把）；
2. **勾选开放的知识库**：Agent 只能检索你勾选的、且你本人有权限的库（开放 ∩ 有权限）；
3. **工具开关**（可选）：逐个开/关三个工具，可自定义提示词与工具描述；
4. **配置连接**：页面给出连接地址（由当前部署地址推导）：

```json
{
  "mcpServers": {
    "kb": {
      "url": "http://<服务器IP>:9000/mcp",
      "headers": { "Authorization": "Bearer kbm_你的密钥" }
    }
  }
}
```

## 密钥的生命周期

| 操作 | 行为 |
|---|---|
| **轮换** | 旧钥匙**立即失效且不可恢复**（无宽限期），正在使用它的 Agent 会断连——确认后重新生成并更新客户端配置 |
| **吊销** | 同样立即失效；吊销后钥匙**留在列表**显示「已吊销」标签（可随时删除这条展示记录；旧密钥仍永久失效，删除记录不影响其他钥匙） |
| 验钥 | **每次调用实时验证**——开放了但本人权限被收窄的库，调用时自然被拒 |
| 域规则 | 不传 `domain` 参数 = 钥匙绑定的域；传了必须等于钥匙域，否则报"该 MCP 钥匙绑定知识域 X，不支持指定其他域"（要访问其他域就新建对应域的钥匙） |

无钥匙/错钥匙：`tools/list` 返回空清单，一切调用被拒——Agent 什么也看不到，也不会泄露库是否存在。

> 多 Agent 场景的最佳实践：**一 Agent 一钥匙 + 只勾最小开放库**；给自动化脚本专建服务账号加库成员（第 20 章），钥匙边界就是审计边界（检索日志不记录发起人，第 14 章）。

## 三个工具

职责一句话：**`search_knowledge` 模糊找、`get_knowledge` 深入读、`upload_document` 上传**。

Agent 的典型节奏：先 `get_knowledge`（不带参数）看自己有什么库 → `search_knowledge` 检索 → 之后一切深入读取都走 `get_knowledge`。返回即统一协议 `{query, evidence[], has_more}`（读取类返回带 `view` 字段自标识视图类型）。

通用规则：**身份 = 钥匙主人**；范围 = 开放的库；错误带稳定 code，按提示修参数重试。

### search_knowledge —— 一切检索的起点

| 参数 | 必填 | 说明 |
|---|---|---|
| `query` | ✅ | 用户原问题**原样传入**（不要改写） |
| `kb_names` | 可选 | 按库名缩小范围（大小写不敏感）；名字必须全部命中开放清单，否则报错并列出当前开放库；**不传 = 检索全部开放库** |
| `within` | 可选 | `{"document_refs": […], "section_refs": […]}`（HTTP 侧还支持 `section_scope`，见第 14 章白名单） |
| `filters` | 可选 | `{"asset_types": […], "evidence_types": […]}`（类型词 = `evidence[].type` 取值） |
| `expansion` | 可选 | `{"mode": "auto|exact|window|parent|whole_document"}` |
| `top_k` | 可选 | 1–200 |
| `paradigm` | 一般不传 | 自动跟随库绑定；传错域的范式报 `paradigm_domain_mismatch` |

典型错误：`no_paradigm_configured`（域没绑范式，响应附可用清单）/ `kb_not_found`（库不存在或无权限）/ `HTTP xxx`（检索服务暂不可用）。

### get_knowledge —— 一切读取行为

一个工具按**入口**自动分流，返回带 `view` 字段自标识：

| 你给的入口 | 行为 | 返回 view |
|---|---|---|
| 什么都不传 | 域→库顶层浏览 | `kb_tree` |
| 只传 `kb_name` | 该库文件清单（分页） | `documents` |
| `ref=ev_`（+`mode`） | 证据原文展开 | `evidence_content` |
| `ref=doc_`（+`limit`/`cursor`） | 整篇文档分页读 | `document_content` |
| 只传 `ref=st_` | **能力报告**：可导航关系 / 表格列（columns）/ 可过滤聚合字段 | `capabilities` |
| `ref=st_` + `query` | 表格精确查询（DSL） | `table_rows` / `aggregate` |
| `ref=st_` + `relation` | 结构关系导航 | `navigation` |

`relation` 取值：`parent / children / previous / next / ancestors / descendants / container / caption / footnotes / references`（能力报告会列出目标支持哪些）。

**表格查询 DSL**（先拿能力报告看列名，再查——字段名以 `assets[].columns[].name` 为准，**不是模糊搜索**）：

```
get_knowledge(ref="st_9Pzq…")                    # 先看能力报告
get_knowledge(ref="st_9Pzq…", query={
    "select": ["额定功率", "冗余配置"],
    "where": [{"field": "额定功率", "op": "lte", "value": 2000}],
    "order_by": [{"field": "额定功率", "direction": "asc"}],
    "limit": 20})
get_knowledge(ref="st_9Pzq…", query={"aggregate": {"op": "avg", "field": "额定功率"}})
get_knowledge(ref="st_9Pzq…", relation="children")
```

**互斥规则**（传错得到明确报错，不静默降级）：`ref` 与 `kb_name` 互斥；`ev_` 只认 `mode`；`doc_` 只认 `limit`/`cursor`；`st_` 上 `query` 与 `relation` 互斥。

### upload_document —— 上传文件

**两步直传**（大文件不走 base64）：

```
① upload_document(kb_name="网络手册库", filenames=["手册.pdf", "notes.md"])
   → 每个文件返回 upload_url（一次性凭证，10 分钟内单次有效，无需认证头）
② curl -X PUT --data-binary @手册.pdf "<upload_url>"
```

注意：

- **upload_url 本身即凭证**（无需认证头）——不要外传、不要落盘进日志；
- **上传自动排队挖掘**（库空闲立即增量挖掘；正在挖掘/审核则排队串行）；需要 owner/editor 权限；
- 类型与上限与网页一致（普通 50MB，zip/hdx/chm 500MB 自动解压）；
- 票据超时/PUT 失败就重调工具取新 URL（票据单次使用）。

## 典型工作流

```
① 不知道有什么 → get_knowledge()
② 检索          → search_knowledge(query="……")
③ 证据被截断    → get_knowledge(ref="ev_…", mode="parent")
④ 要整篇文档    → get_knowledge(ref="doc_…", limit=100)   # 翻页用 cursor
⑤ 碰到表格      → get_knowledge(ref="st_…")               # 能力报告
                → get_knowledge(ref="st_…", query={…})
⑥ 结构上下游    → get_knowledge(ref="st_…", relation="children")
```

---

## 本章素材来源

| 素材 | 用途 |
|---|---|
| `kb-ui/src/views/McpAccessView.vue` | 界面行为与文案：每 Agent 一钥/一域、明文一次性展示、10 把上限、轮换与吊销确认、吊销留列表标"已吊销"、删除记录、域未绑定提示、连接地址推导 |
| `mcp_server/identity.py`（validate_domain） | domain 校验语义（不传=钥匙域，传错报"不支持指定其他域"） |
| `docs/检索使用指南-2026-09-07.md` §3.2/§5/§9 | 三工具参数表、分流表、DSL 示例、互斥规则、错误表、两步直传（kbIds/503 相关行为已按代码修正） |
| `docker-compose.yml`（MCP 9000 端口注释） | 外部直连端口、"勿改回 127.0.0.1" |
