# 检索预热：Java JAR 与数据库独立配置

2026-10-21 范围更正：应用继续沿用现有 JAR 构建、同步和服务重启流程。本次不修改 deploy-sync.sh、deploy-server.sh、docker-compose.yml、Supervisor、Docker 命令、端口或挂载。此前 2026-10-20 的混合安装包已撤回，不应继续使用。

## 1. 保留的代码与发布方式

保留 Java 内部的数据库预热、PG 重启检测和检索就绪判断，以及对应 Java 测试。编译结果仍是原来的 agent-serving-0.1.0.jar，由你原有的同步流程放入 runtime/agent_serving.jar；不用安装其他应用组件或更换部署脚本。

Java 在启动后自行观察数据库，每个数据源同一时间只执行一次预热。只在目标数据库的 `cmkb.retrieval_prewarm` 等于 `on` 时启用：先将两主表派生的 TOAST 表及有效索引读入 OS 页缓存，再将四个主目标加载到 PG shared buffers，HNSW 最后。完成后核对 PG 启动时间、数据库和物理关系身份。

未设置该开关的其他部署默认不预热、不阻挡检索，不要求为本次功能安装 pg_prewarm 或改内存配置；仍有只读元数据检查。不要将下述目标数据库配置复制到所有部署。

已开启的目标库预热期间，检索返回 HTTP503 和 Retry-After；完成后放行。新增 `/actuator/health/readiness` 仅作观察接口，原 `/actuator/health` 保持存活语义，现有同步脚本无须接入新接口。现有部署报告“服务已启动”时，Java 仍可能处于预热阶段，检索应等准备完成。

## 2. 上次 2GB 改 8GB 实际做了什么

用户已执行的 apply_retrieval_cache_fix.sh：

- `ALTER SYSTEM SET shared_buffers='8GB'` 写入 PGDATA/postgresql.auto.conf，覆盖原 postgresql.conf 中的 2GB，重启保留。
- 安装或复用目标数据库的 pg_prewarm 扩展。
- 重启一次 PG，并一次性预热 embeddings 主表、units 主表、units 主键、HNSW 索引，共 5344419840 字节。
- 没有修改业务数据、检索 SQL、模型或应用 JAR，没有配置下一次重启的自动预热。

本次不再次改容量。8GB 是这个内网实例的选择，其他实例需根据自身内存和索引大小决定。

## 3. 数据库由 DBA 单独配置一次

适用已确认的 PostgreSQL15 / pgvector0.8.0 环境。目标容器 `pgvector-postgres`，目标库 `coremasterkb_converged`。在维护时间，用该库的管理员连接执行。下面是数据库操作说明，不是应用部署脚本。

### 3.1 保留现有预载模块，追加 pg_prewarm

先查看现有模块：

```sql
SHOW shared_preload_libraries;
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
```

将 `pg_prewarm` 加入原列表，保留原有每个模块。例如，**仅当原列表为 `pg_stat_statements` 时**，执行：

```sql
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements', 'pg_prewarm';
```

若原来还有 `auto_explain` 等模块，必须一并保留。若原来列表为空，则为：

```sql
ALTER SYSTEM SET shared_preload_libraries = 'pg_prewarm';
```

不要把 SQL 列表写成一个 `'pg_stat_statements,pg_prewarm'` 字符串；ALTER SYSTEM 的列表值应分别给出。

设置记录间隔：

```sql
ALTER SYSTEM SET pg_prewarm.autoprewarm_interval = '300s';
```

按原来的方式重启 PG 一次。若使用现有容器，仍是：

```bash
docker restart pgvector-postgres
```

### 3.2 预载完成后持久开启，指定 Java 目标数据库

重新连接目标数据库，执行：

```sql
SHOW pg_prewarm.autoprewarm;
ALTER SYSTEM SET pg_prewarm.autoprewarm = 'on';
ALTER DATABASE coremasterkb_converged SET cmkb.retrieval_prewarm = 'on';
```

PG15 首次预载后才注册 `pg_prewarm.autoprewarm`，因此不能将这里的 ALTER SYSTEM 提前到首次预载重启之前。首次默认值是 on；若 SHOW 显示原来为 off，设置后还需按原方式再重启一次 PG。

然后通过你原有的同步流程发布 Java JAR、重启 serving。数据库级设置对新会话生效，这次正常应用重启会创建新连接，不需要增加部署步骤。

当前内网 kb_user 已是管理员。预热需要目标关系和 pg_prewarm 函数的相应权限；对于其他低权限部署，不要直接提权，应继续保持开关关闭或由 DBA 按需授权。

### 3.3 保存初始缓存清单

Java 日志出现 `Retrieval cache prewarm ready` 后，在同一目标库执行一次：

```sql
SELECT autoprewarm_dump_now();
```

清单保存为 PGDATA/autoprewarm.blocks，以后后台每300秒更新，正常停止也会保存。原生后台自动恢复是异步的；Java 就绪判断负责在目标库预热完成前返回503。

上述参数由 ALTER SYSTEM 写入 postgresql.auto.conf，数据库级开关保存在目标库设置中。原 postgresql.conf 没有同步变更是正常现象。

## 4. 以后重启与重新部署

- 继续使用原来的 JAR 同步和服务重启命令；没有额外应用安装器、启动标记文件或 Supervisor 改动。
- PG 真正重启后，原生模块依据清单恢复 PG 缓存；Java 自动检测 PG 启动时间变化并重新预热，完成后接受检索。
- 仅重启应用，Java 会自行检查并确认目标库预热；不要求重启 PG。
- 重启 Docker 服务或宿主机后，若原机制启动了 PG 和应用，且保留同一 PGDATA，预热机制照常工作。本功能不改变原有容器自启动策略。
- 保留原 PGDATA（本次 `/opt/pgdata`），包括 postgresql.auto.conf、autoprewarm.blocks 和数据库设置；原来的持久化方式不变。
- 新建库或恢复到另一个库时，DBA 在新目标单独配置扩展及数据库级开关。其他现有部署不需要一起调整。

四个 PG buffer 目标总大小上限是 shared_buffers 的75%；TOAST OS read 目标总大小另限 shared_buffers。规模超限或预热失败会保持503并重试，不自动增加内存。预热不锁页，后续业务仍可能淘汰缓存。

## 5. 关闭与验证范围

要关闭某个目标库的 Java 预热：

```sql
ALTER DATABASE coremasterkb_converged RESET cmkb.retrieval_prewarm;
```

随后按现有方式重启 serving，使新会话不再继承开关。原生 pg_prewarm 配置独立保留；关闭 Java 开关不回退8GB、不修改数据。

Java 54项测试已通过（含3项真实PG测试），新增代码行覆盖243/243，分支160/174。原生自动恢复在本地 PostgreSQL15.14 / pgvector0.8.0 小样本上已验证重启恢复156/156页；这不等于内网40万向量的端到端性能复现。

数据库预热在启动阶段消耗I/O时间；embedding/模型冷启动及后续缓存淘汰仍可能影响检索，不能承诺整个业务固定毫秒级。

参考：[PostgreSQL15 pg_prewarm](https://www.postgresql.org/docs/15/pgprewarm.html)、[ALTER SYSTEM](https://www.postgresql.org/docs/15/sql-altersystem.html)。
