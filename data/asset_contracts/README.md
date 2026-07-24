# Asset Contracts

本目录存放按共享 schema 直接构建出来的 SQLite 基线库。

当前文件：

- `asset_core_v1_1.sqlite`：基于 `databases/asset_core/schemas/001_asset_core.sqlite.sql` 生成的空库基线。

用途：

1. 在 SQLite 可视化工具中直接查看最新表结构。
2. 作为 Mining / Serving 后续联调前的数据库契约基线。
3. 用于快速校验共享 DDL 是否能成功落库。
