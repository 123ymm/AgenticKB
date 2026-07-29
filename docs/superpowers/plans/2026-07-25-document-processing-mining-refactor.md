# 文档解析与知识挖掘改造实施计划

> **供执行本计划的 Agent 使用：** 实施时必须使用 `subagent-driven-development`（推荐）或 `executing-plans`，按照复选框逐项完成，并在每个阶段执行对应测试。

**目标：** 在分段之前持久化一份有版本、可溯源、可按行读取的文档解析结果，并让所有文档格式统一进入同一套分段与知识挖掘流水线。

**架构：** 在现有挖掘代码中新增独立的“文档处理”子系统。该子系统负责格式解析，并持久化不可变的解析结果和有序结构块；知识挖掘负责文档级分段集合、增强、检索单元、向量、构建和发布。现有 Snapshot 身份模型保持不变，但 Build 必须固定具体的源文件链接、解析结果和分段集合。

**技术栈：** Python 3.11、dataclass/Protocol、PostgreSQL JSONB/TEXT、SQLite 开发库、pytest。

---

## 一、已经确认的设计约束

### 1. 职责边界

```text
文档处理子系统
  输入：精确源文件内容、源文件元数据、Parser 配置
  输出：不可变 Parse Result、有序 Parsed Blocks
  不负责：按 token 合并/拆分、内容增强、Embedding、Build、Release

知识挖掘子系统
  输入：parse_result_id
  输出：Segment Set、Segments、Relations、Retrieval Units、Embeddings
  结束：Build/Release 发布
```

第一期把文档处理实现成仓库内独立包和内部服务，不立即拆成单独部署的 HTTP 微服务。其接口和持久化数据保持独立，后续需要进程化或服务化时不改变资产数据模型。

### 2. 行号与来源坐标规则

- 内部文本范围统一使用0基、左闭右开区间：`[start_line, end_line)`。
- Markdown/TXT 的规范正文只允许把 `CRLF` 和单独的 `CR` 统一成 `LF`。
- 不删除空行、不裁剪每行、不去除 Markdown 标记、不折叠空白。
- `total_lines` 根据 `canonical_text.split("\n")` 计算；正文以换行符结尾时，保留末尾空行。
- Block 和 Segment 的来源范围表示“可靠覆盖范围”：必须覆盖其使用的全部源内容，但在无法精确缩小范围时可以保守地引用整个来源 Block。
- 自动注入的面包屑和上下文属于生成内容，只写入生成内容元数据，不得计入来源范围。
- PDF 第一期使用页码和坐标，设置 `line_addressable=false`。

### 3. 身份和不可变规则

- `asset_documents` 继续表示逻辑文档身份。
- `asset_document_snapshots` 继续表示按归一化内容共享的 Snapshot，不承担 Parser 版本职责。
- `asset_document_snapshot_links` 表示某次具体来源和导入位置。
- Parse Result 不可变；只有原始内容哈希、Parser 身份和配置完全一致时才能复用。
- Segment Set 不可变，并且属于具体 Document。相同正文在不同 Document 下可能有不同标题、路径、Scope 和上下文。
- 已发布 Build 固定 `snapshot_link_id + parse_result_id + segment_set_id`。Parser 或 Segmenter 升级时产生新记录，不覆盖旧记录。

## 二、数据库设计

### 1. 新增 `asset_document_parse_results`

一行表示一次成功且不可变的解析结果。

```sql
CREATE TABLE asset_document_parse_results (
    id                       TEXT PRIMARY KEY,
    document_snapshot_id     TEXT NOT NULL
        REFERENCES asset_document_snapshots(id) ON DELETE CASCADE,
    source_raw_content_hash  TEXT NOT NULL,
    parser_name              TEXT NOT NULL,
    parser_version           TEXT NOT NULL,
    parser_config_hash       TEXT NOT NULL,
    content_kind             TEXT NOT NULL CHECK (
        content_kind IN ('source_text', 'extracted_text')
    ),
    readable_content         TEXT NOT NULL,
    readable_content_hash    TEXT NOT NULL,
    line_addressable         BOOLEAN NOT NULL,
    total_lines              INTEGER CHECK (total_lines IS NULL OR total_lines >= 0),
    coordinate_type          TEXT NOT NULL CHECK (
        coordinate_type IN ('line', 'page_bbox', 'mixed', 'none')
    ),
    metadata_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TEXT NOT NULL,
    CHECK (
        (line_addressable AND coordinate_type IN ('line', 'mixed') AND total_lines IS NOT NULL)
        OR
        (NOT line_addressable AND total_lines IS NULL)
    ),
    UNIQUE (
        document_snapshot_id,
        source_raw_content_hash,
        parser_name,
        parser_version,
        parser_config_hash
    )
);
```

字段说明：

- `source_raw_content_hash`：区分归一化内容相同、但空行或原始排版不同的源文件。
- `readable_content`：Read 接口唯一允许读取的完整正文。
- `content_kind`：`source_text` 表示文本源正文，`extracted_text` 表示 PDF 等格式的抽取正文。
- `line_addressable`：是否可以对外承诺精确、稳定的行读取。
- `parser_config_hash`：相同 Parser 版本使用不同配置时产生不同解析结果。

Read 接口不得通过拼接 Segment 重建 `readable_content`。

### 2. 新增 `asset_document_parsed_blocks`

```sql
CREATE TABLE asset_document_parsed_blocks (
    id                  TEXT PRIMARY KEY,
    parse_result_id     TEXT NOT NULL
        REFERENCES asset_document_parse_results(id) ON DELETE CASCADE,
    block_index         INTEGER NOT NULL CHECK (block_index >= 0),
    block_type          TEXT NOT NULL,
    text                TEXT NOT NULL,
    section_path_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_ranges_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
    structure_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (parse_result_id, block_index)
);
```

Markdown 来源范围示例：

```json
[{"coordinate_type":"line","start":10,"end":18}]
```

表示内部第10行至第17行，对外显示为第11行至第18行。

PDF 来源范围示例：

```json
[{"coordinate_type":"page_bbox","page":3,"bbox":[72,120,520,360]}]
```

### 3. 新增 `asset_segment_sets`

```sql
CREATE TABLE asset_segment_sets (
    id                     TEXT PRIMARY KEY,
    document_id            TEXT NOT NULL
        REFERENCES asset_documents(id) ON DELETE CASCADE,
    parse_result_id        TEXT NOT NULL
        REFERENCES asset_document_parse_results(id) ON DELETE CASCADE,
    segmenter_name         TEXT NOT NULL,
    segmenter_version      TEXT NOT NULL,
    segmenter_config_hash  TEXT NOT NULL,
    document_profile_hash  TEXT NOT NULL,
    segment_count          INTEGER NOT NULL CHECK (segment_count >= 0),
    metadata_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at             TEXT NOT NULL,
    UNIQUE (
        document_id,
        parse_result_id,
        segmenter_name,
        segmenter_version,
        segmenter_config_hash,
        document_profile_hash
    )
);
```

Segment Set 必须包含 `document_id`，因为相同解析正文用于不同逻辑文档时，标题、路径、Scope、文档类型和结构化上下文可能不同。

### 4. 修改现有表

```text
asset_document_snapshot_links
  + raw_content_hash TEXT NULL

asset_raw_segments
  + segment_set_id TEXT NULL FK asset_segment_sets(id)
  + source_ranges_json JSONB NOT NULL DEFAULT []

asset_raw_segment_relations
  + segment_set_id TEXT NULL FK asset_segment_sets(id)

asset_retrieval_units
  + segment_set_id TEXT NULL FK asset_segment_sets(id)

asset_build_document_snapshots
  + document_snapshot_link_id TEXT NULL FK asset_document_snapshot_links(id)
  + parse_result_id TEXT NULL FK asset_document_parse_results(id)
  + segment_set_id TEXT NULL FK asset_segment_sets(id)
```

迁移期新字段允许为空，以便旧 Release 继续工作；新挖掘任务写入的 Build Selection 必须全部填充。

原有 Snapshot 范围唯一约束改为兼容新旧数据的部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_asset_raw_segments_set_index
    ON asset_raw_segments(segment_set_id, segment_index)
    WHERE segment_set_id IS NOT NULL;

CREATE UNIQUE INDEX uq_asset_raw_segments_set_key
    ON asset_raw_segments(segment_set_id, segment_key)
    WHERE segment_set_id IS NOT NULL;

CREATE UNIQUE INDEX uq_asset_raw_segments_legacy_key
    ON asset_raw_segments(document_snapshot_id, segment_key)
    WHERE segment_set_id IS NULL;

CREATE UNIQUE INDEX uq_asset_retrieval_units_set_key
    ON asset_retrieval_units(segment_set_id, unit_key)
    WHERE segment_set_id IS NOT NULL;

CREATE UNIQUE INDEX uq_asset_retrieval_units_legacy_key
    ON asset_retrieval_units(document_snapshot_id, unit_key)
    WHERE segment_set_id IS NULL;
```

旧 `source_offsets_json` 暂时保留，只用于历史兼容和调试。新代码把权威来源写入 `source_ranges_json`，不得使用旧 Offset 伪造精确行号。

### 5. 迁移与历史数据策略

- 新增 `databases/asset_core/schemas/004_asset_core_parse_results.sql`。
- 同步修改 PostgreSQL、通用 SQL 和 SQLite 的新建库 Schema。
- 在 `pg_schema.py`、`reset_db.py` 注册004迁移。
- 在 `db_tables.py` 中按照外键依赖顺序加入三张新表，保证导入导出顺序正确。
- 历史 Link 的 `raw_content_hash` 保持 `NULL`，因为 Snapshot 中的哈希不一定代表该 Link 的原始文件。
- 历史 Build Selection 的三个新 ID 保持 `NULL`，由 Serving 按旧模式读取。
- 不允许通过旧 Segment 拼接正文进行回填。
- 只有重新读取源文件、完成解析和发布新 Build 后，文档才获得精确 Read 能力。

## 三、统一 Parser 输出协议

新增 `knowledge_mining/mining/document_processing/contracts.py`：

```python
@dataclass(frozen=True)
class SourceRange:
    coordinate_type: str
    start: int | None = None
    end: int | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None

@dataclass(frozen=True)
class ParsedBlock:
    block_index: int
    block_type: str
    text: str
    section_path: tuple[dict[str, object], ...] = ()
    source_ranges: tuple[SourceRange, ...] = ()
    structure: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

@dataclass(frozen=True)
class ParsedDocument:
    parser_name: str
    parser_version: str
    parser_config_hash: str
    content_kind: str
    readable_content: str
    readable_content_hash: str
    line_addressable: bool
    total_lines: int | None
    coordinate_type: str
    blocks: tuple[ParsedBlock, ...]
```

Parser 协议返回 `ParsedDocument`，不得感知 token 预算、Chunk 重叠、Embedding 配置、Build 或 Release。

为了让相同原始正文的 Parse Result 可以复用，Parser 输出不得依赖文件名生成正文内容；文件名、标题和路径由 Document/Link/Profile 层管理。

## 四、不同格式的处理方式

### 1. Markdown

- 规范正文为只统一换行符的 Markdown 源文。
- Markdown-it 的 Token Map 转换成0基左闭右开行范围。
- 标题仍作为有序 Block 保存，并在元数据中保留标题级别。
- Markdown 标记清理只发生在生成检索 Segment 文本时，不修改规范正文。

### 2. TXT

- 规范正文为只统一换行符的源文本。
- Parser 按空行结构产生段落 Block。
- 从 `PlainTextParser` 删除 `_split_long_text`、`chunk_size`、`chunk_overlap`。
- 长段落拆分统一由公共 Segmenter 执行。

### 3. PDF

- Parser 产生有序结构 Block、页码和坐标。
- `readable_content` 保存确定性的抽取文本，供统一挖掘使用。
- 设置 `content_kind=extracted_text`、`coordinate_type=page_bbox`、`line_addressable=false`、`total_lines=NULL`。
- PDF 与 MD/TXT 使用同一个 Segmenter。
- 在后续 Parser 能提供稳定行坐标前，PDF 的行读取请求明确返回不支持。

## 五、挖掘流水线改造

现有流程：

```text
内存 Parse Tree
  → Segment
  → Enrich
  → 最后统一创建 Snapshot 并写入全部结果
```

目标流程：

```text
准备 Document/Snapshot/Link
  → 解析并持久化 Parse Result/Blocks
  → 按 parse_result_id 重新读取解析结果
  → 公共 Segmenter
  → Enrich/Relations/Retrieval Units/Embeddings
  → 原子写入 Segment Set 及全部挖掘产物
  → Build Selection 固定精确资产版本
```

`DocumentContext` 增加并传递：

- `document_id`
- `snapshot_id`
- `snapshot_link_id`
- `parse_result_id`
- `segment_set_id`

Segment 阶段必须通过 Repository 按 `parse_result_id` 读取持久化 Block。仅在 `DocumentContext.tree` 中保留解析树，不算成功完成解析持久化。

解析结果使用独立事务写入。即使后续挖掘失败，已经成功的 Parse Result 仍然保留并可以在重试时复用。

Segment Set、Segments、Relations 和 Retrieval Units 在同一文档级事务中写入，避免出现不完整 Segment Set。

## 六、Segment 来源范围处理

- 从 Parsed Block 创建 Segment 时复制 Block 的全部 `source_ranges`。
- Segment 合并时合并并规范化双方来源范围。
- 小段吸收和引导段前向合并时，不得只继承 `prev` 或 `candidate` 的旧范围。
- Segment 拆分时，如果存在可靠映射则拆分来源范围；否则子 Segment 保守引用完整来源 Block。
- 面包屑注入只增加 `generated_context` 元数据，不改变来源范围。
- 最终统一重编号，保证一个 Segment Set 内 `segment_index=0..N-1`。
- `segment_index` 用于检索顺序和上下文扩展，`source_ranges_json` 用于返回原文位置，两者不能互相替代。

## 七、增量挖掘与发布

Build 分类不再只比较 `document_snapshot_id`，而是比较：

```text
(
  document_snapshot_id,
  document_snapshot_link_id,
  parse_result_id,
  segment_set_id
)
```

分类规则：

- Snapshot 相同但原始空行布局不同：`UPDATE`。
- 源文件相同但 Parser 版本变化：`UPDATE`。
- Parse Result 相同但 Segmenter 配置变化：`UPDATE`。
- 四个 ID 全部相同：`SKIP/retain`。

`validate_build` 对新模式的 Active Selection 检查：

1. 三个新增 ID 均不为空；
2. Link、Parse Result、Segment Set 与 Document/Snapshot 关系一致；
3. Segment Set 至少包含一个 Segment；
4. 实际 Segment 数等于 `asset_segment_sets.segment_count`；
5. 所有 Retrieval Unit 属于被选中的 Segment Set。

## 八、文件变动范围

### 新增文件

- `databases/asset_core/schemas/004_asset_core_parse_results.sql`
- `knowledge_mining/mining/document_processing/__init__.py`
- `knowledge_mining/mining/document_processing/contracts.py`
- `knowledge_mining/mining/document_processing/service.py`
- `knowledge_mining/mining/document_processing/repository.py`
- `knowledge_mining/mining/document_processing/parsers/markdown.py`
- `knowledge_mining/mining/document_processing/parsers/plain_text.py`
- `knowledge_mining/mining/document_processing/parsers/pdf.py`
- `knowledge_mining/tests/test_document_processing_contract.py`
- `knowledge_mining/tests/test_parse_result_repository.py`
- `knowledge_mining/tests/test_segment_source_ranges.py`
- `knowledge_mining/tests/test_segment_set_publication.py`

### 修改文件

- `databases/asset_core/schemas/002_asset_core_postgresql.sql`
- `databases/asset_core/schemas/001_asset_core.sqlite.sql`
- `databases/asset_core/schemas/001_asset_core.sql`
- `databases/asset_core/schemas/README.md`
- `knowledge_mining/mining/infra/pg_schema.py`
- `reset_db.py`
- `db_tables.py`
- `knowledge_mining/mining/contracts/models.py`
- `knowledge_mining/mining/contracts/protocols.py`
- `knowledge_mining/mining/stages/parse.py`
- `knowledge_mining/mining/stages/segment.py`
- `knowledge_mining/mining/pipeline.py`
- `knowledge_mining/mining/snapshot/__init__.py`
- `knowledge_mining/mining/infra/db.py`
- `knowledge_mining/mining/stages/publishing.py`
- `knowledge_mining/mining/jobs/run.py`
- `knowledge_mining/docs/stage-02-parse.md`
- `knowledge_mining/docs/stage-03-segment.md`
- `knowledge_mining/docs/stage-07-db-write-build-publish.md`

## 九、实施任务

### 任务1：增加数据库结构和迁移测试

- [ ] 先增加失败测试，验证新表、外键、部分唯一索引和兼容字段。
- [ ] 执行 `python -m pytest knowledge_mining/tests/test_asset_domain_migration.py -v`，确认新增断言失败。
- [ ] 新增004迁移并更新三份新建库 Schema。
- [ ] 在 `pg_schema.py`、`reset_db.py`、`db_tables.py` 注册新迁移和表顺序。
- [ ] 分别验证空库建库和历史 Schema 升级。
- [ ] 提交：`feat(asset-core): add parse result and segment set schema`。

### 任务2：定义文档处理协议

- [ ] 增加换行符、末尾空行、半开行范围和配置哈希测试。
- [ ] 执行 `python -m pytest knowledge_mining/tests/test_document_processing_contract.py -v`，确认失败。
- [ ] 实现协议类型和规范正文辅助函数，文档处理包不得反向依赖挖掘 Stage。
- [ ] 确认协议测试通过。
- [ ] 提交：`feat(processing): define persistent parser contract`。

### 任务3：实现 Markdown/TXT 适配器

- [ ] 增加保留空行测试，以及 TXT Parser 不再按 token 分块的测试。
- [ ] 确认测试在当前实现上失败。
- [ ] 实现两个适配器，并在 `stages/parse.py` 保留必要兼容导出。
- [ ] 执行文档处理和多格式回归测试。
- [ ] 提交：`refactor(processing): separate parsing from segmentation`。

### 任务4：实现 PDF 适配器

- [ ] 增加 Block 顺序、页码坐标和禁用行读取能力测试。
- [ ] 确认测试失败后，把现有 PDF Parser 适配到统一协议。
- [ ] 执行 `python -m pytest knowledge_mining/tests/test_multiformat_and_splitting.py -v`。
- [ ] 提交：`feat(processing): adapt pdf blocks to parser contract`。

### 任务5：持久化并重新读取解析结果

- [ ] 增加结果/Block 原子写入、精确哈希复用、Parser 版本隔离和失败回滚测试。
- [ ] 确认 Repository 测试失败。
- [ ] 实现 AssetCoreDB 方法、Repository 和 `DocumentProcessingService`。
- [ ] 确认 Repository 测试通过。
- [ ] 提交：`feat(processing): persist immutable parse results`。

### 任务6：调整 Pipeline 顺序

- [ ] 增加测试，证明 Snapshot/Link 在解析前建立，Parse Result 在 Segment 启动前持久化。
- [ ] 确认当前 `db_write_stage` 顺序无法通过测试。
- [ ] 新增准备阶段和解析持久化阶段，Stage 之间只传递稳定 ID。
- [ ] 执行 Pipeline 顺序和现有主流水线测试。
- [ ] 提交：`refactor(mining): persist parse artifacts before segmentation`。

### 任务7：修复合并/拆分过程中的来源范围

- [ ] 覆盖普通合并、孤儿吸收、引导段合并、长段拆分、面包屑和最终重编号。
- [ ] 断言结果范围覆盖全部来源 Block，且不包含生成上下文。
- [ ] 确认新增测试在旧继承逻辑上失败。
- [ ] 实现 `source_ranges_json` 的合并和拆分规则。
- [ ] 执行 Segment 和 Provenance 回归测试。
- [ ] 提交：`fix(mining): preserve source ranges through segmentation`。

### 任务8：原子写入 Segment Set

- [ ] 增加 Segment Set 复用身份和事务回滚测试。
- [ ] 实现 Segment Set 与全部下游记录的原子写入。
- [ ] 新数据改用 Segment Set 级数量、删除和复用方法，不再使用 Snapshot 级方法。
- [ ] 执行领域生命周期和增量挖掘测试。
- [ ] 提交：`feat(mining): version segment outputs by segment set`。

### 任务9：Build 固定全部资产版本

- [ ] 增加四 ID 比较、父 Build 继承和非法交叉引用测试。
- [ ] 扩展 Build Assembly 和 Validation。
- [ ] 执行发布、撤回、增量生命周期回归测试。
- [ ] 提交：`feat(publishing): pin source parse and segment artifacts`。

### 任务10：端到端验证和文档更新

- [ ] 增加包含空行的 Markdown Fixture 和 PDF Fixture。
- [ ] 验证 Markdown 可按行读取，PDF 有页码来源但不可按行读取。
- [ ] 验证所有格式调用同一个 Segmenter 实现。
- [ ] 执行 `python -m pytest knowledge_mining/tests -v` 并记录通过数量。
- [ ] 更新解析、分段、数据库写入和 Schema 文档。
- [ ] 提交：`docs(mining): document parser and segment-set lifecycle`。

## 十、验收标准

- 新发布的每个文档都固定 Source Link、Parse Result 和 Segment Set。
- 所有 Parser 都不再执行 token 预算分块。
- MD/TXT 的完整正文不依赖 Segment，能够独立读取和拼接。
- Segment 经过所有合并、吸收和拆分后，来源覆盖范围仍然可靠。
- PDF 通过统一 Segmenter 参与检索，同时明确声明不支持行读取。
- Parser 或 Segmenter 升级不改变旧 Release 已经使用的结果。
- 旧 Release 在重解析前仍可检索，但旧 Offset 不会被声明成精确 Read 坐标。

