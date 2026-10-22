"""Wiki 编译管线 prompt 常量与版本冻结（63号批次3 Task 3）。

独立模块的理由：服务主文件 800 行铁律 + prompt 文本的变更节奏与编排逻辑
不同（改 prompt 只动这里）。**版本冻结铁律**（批次3 决策4）：幂等键含
prompt_version——任何 prompt 文本变更必须同步升版本号，否则 llm_service
任务缓存会继续命中旧输出。
"""
from __future__ import annotations

#: 幂等键构成部分（决策4：升级 prompt 必须同步升版本，否则命中旧任务缓存）。
TOC_PROMPT_VERSION = "wiki-toc-1"
PAGE_PROMPT_VERSION = "wiki-page-1"
QUALITY_PROMPT_VERSION = "wiki-quality-1"

#: 全书页数硬上限（决策3：页 cap 前移到目录生成+confirm 校验+写作防御三层）。
MAX_PAGES = 200

TOC_SYSTEM = (
    "你是知识库 Wiki 编译的目录架构师。输入是语料聚类簇清单"
    "（簇 id / 启发式标签 / 代表句 / 目录分布）。请设计一本 wiki 全书的目录："
    "分节、每节页提示（页标题 + 来源簇 id）、术语表、开放问题。只输出 JSON 对象，"
    "schema：\n"
    '{"toc":[{"section_title":"节标题","page_hints":[{"title":"页标题",'
    '"cluster_ids":["簇id"],"page_type":"topic"}]}],'
    '"glossary":[{"term":"术语","definition_hint":"定义提示"}],'
    '"open_questions":[{"question":"问题？","why":"为何重要"}]}\n'
    "硬约束：全书总页数（各节 page_hints 数量之和）不得超过 " + str(MAX_PAGES)
    + " 页，相近主题合并为单页；cluster_ids 只准使用输入清单中出现的簇 id；"
    "open_questions 的 question 以？结尾；无术语/问题输出空数组。"
)

#: TOC 修复重试注记（拼进 user 消息——payload 含注记故幂等键不同，不命中
#: 旧缓存；文案对解析失败/页数超限两类重试通用）。
TOC_RETRY_NOTE = (
    "上一版页数超限或解析失败，请压缩合并至 " + str(MAX_PAGES)
    + " 页以内并重新输出完整合规 JSON。"
)

#: 页写作四节模板 + 无据不写规则（63号 §4.3/§4.4；system 头段——调用方再拼
#: 全局目录/可用页键/批间约束）。
PAGE_SYSTEM_HEAD = (
    "你是 Wiki 页面作者。严格按四节模板输出纯 Markdown 正文（不输出 frontmatter）：\n"
    "# 页标题\n> 一句话摘要（blockquote，必填）\n\n"
    "## 核心事实\n- 事实句 [[src:unit_id]]（每个事实句必须带引用标记）\n\n"
    "## 相关页面\n- [[其他页key]] -- 关系注释（父子/组成/参照）\n\n"
    "## 来源\n- [[src:unit_id]] -- 切片摘要一句话\n"
    "无据不写：每个事实句必须带 [[src:]]，引用只准用给定素材中出现的 unit id，"
    "禁止编造 id；相关页面双链只准用给定页键清单中的键；不要输出模板外的内容。"
)

#: 质量审校（矛盾/缺口发现，一次调用——contradiction/open_question 专页源）。
QUALITY_SYSTEM = (
    "你是 Wiki 全书质量审校员。输入是全部已写页清单（页键/标题/摘要）。"
    "找出跨页矛盾（同一主题两页给出不一致主张）与内容缺口（应写而未写）。"
    "只输出 JSON 对象：\n"
    '{"contradictions":[{"page_a":"页键","page_b":"页键","claim_a":"主张A",'
    '"claim_b":"主张B","severity":"low|medium|high"}],'
    '"gaps":[{"question":"问题？","context":"上下文"}]}\n'
    "page_a/page_b 必须取自输入清单；没有矛盾/缺口时输出空数组。"
)

__all__ = [
    "TOC_PROMPT_VERSION",
    "PAGE_PROMPT_VERSION",
    "QUALITY_PROMPT_VERSION",
    "MAX_PAGES",
    "TOC_SYSTEM",
    "TOC_RETRY_NOTE",
    "PAGE_SYSTEM_HEAD",
    "QUALITY_SYSTEM",
]
