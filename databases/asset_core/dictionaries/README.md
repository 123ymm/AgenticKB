# Dictionaries

本目录存放挖掘态和运行态使用的规则配置文件。

## 文件说明

| 文件 | 用途 | 状态 |
|------|------|------|
| command_patterns.yaml | 命令识别规则（ADD/MOD/DEL/SET/SHOW 等） | 占位，M3 填充 |
| section_patterns.yaml | 段落类型识别规则 | 占位，M2 填充 |
| term_patterns.yaml | 术语识别规则 | 占位，M3 填充 |
| builtin_alias_hints.yaml | 内置别名提示（仅开发参考） | 占位，M3 填充 |

## 约束

系统不依赖预置本体或旧 alias 字典启动。用户运行时导入已解析 Markdown 产品文档后，系统基于 Markdown 标题、表格、代码块和弱规则自动生成可检索的 section、segment、命令候选、术语候选和上下文扩展边。

正式 alias_dictionary 不是 Phase 1A 的前置输入，而是从用户导入的产品文档中抽取候选、经人工确认后形成的知识资产。
