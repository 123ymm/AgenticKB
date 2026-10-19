-- 009：v2 向量表 HNSW 近邻索引（内网 1.1.14 检索性能缺陷修复）。
-- 缺陷定性：asset_retrieval_embeddings_v2 自 2026-08-30 上线（013）起从未建过
-- ANN 索引——旧 v1 表的 HNSW 定义随 001 退役删除，但 1.1.8 起检索早已查 v2，
-- "删旧表"删的不是在用的索引，v2 从出生就裸奔。40 万×1024 维向量存 TOAST
-- （~4KB/条，全量 ~1.6GB），无索引 = 每次 dense 检索全量读盘逐条算距离
-- （现场证据：wait_event=DataFileRead，单查询 100s+）。
-- 修复是两件套：本迁移建索引；配套 Java searchDenseV2 改写为
-- 「按距离 LIMIT 取候选 → DISTINCT ON 去重」的 ANN 可用形态（原 DISTINCT ON
-- 排序键以 representation_id 打头，走不了近邻路径）。serving 连接层另需
-- SET hnsw.iterative_scan（0.8+ 默认 off，带过滤查询不打开就用不上索引）。
--
-- 执行方式：迁移器（runner.apply_manifest）单事务执行；CREATE INDEX
-- CONCURRENTLY 不能进事务块，故用普通 CREATE INDEX——本文件只在部署停机窗口
-- 由 database_upgrade apply 运行（部署流程本就全停服务）。账本门控：已应用
-- 库自动跳过，重建容器/重复 apply 不触发、不重复建。
--
-- 只建正式表：staging（挖掘写入面）绝不能带 HNSW——LIKE INCLUDING ALL 克隆 +
-- 每批次 DELETE+INSERT 会反复付建图代价；正式表晋升是逐快照 DELETE+INSERT
-- （db.promote_snapshot_assets），索引增量维护，晋升后存活。
--
-- 前置守卫（fail-closed，给运维可读错误而不是建图中途的晦涩报错）：
--   * pgvector >= 0.8.0：带过滤（snapshot_id IN ...）的查询要走迭代索引扫描
--     才能用上 HNSW；0.8 以下建了也白建，直接拒绝并提示升级扩展/镜像。
--   * 列必须是 vector(1024)（013 原生形态）：若历史库漂移为无维度 vector，
--     混维度数据无法建索引，须先人工核对库形态再升级。
-- 自愈：同名索引若无效（失败 CONCURRENTLY 残留）或定义漂移（方法/操作符类/
-- 目标列不符）→ 先 DROP 再重建；正确且有效的同名索引原样保留不重建。
-- maintenance_work_mem 仅本迁移会话调大以加速建图（40 万×1024 维预计分钟级，
-- 视盘速），迁移连接用后即弃，不影响服务运行时配置。

DO $$
DECLARE
    ext_version TEXT;
    column_type TEXT;
    existing_def TEXT;
    existing_valid BOOLEAN;
BEGIN
    SELECT extversion INTO ext_version FROM pg_extension WHERE extname = 'vector';
    IF ext_version IS NULL THEN
        RAISE EXCEPTION 'pgvector 扩展未安装：请先安装/升级数据库镜像（pgvector/pgvector:pg16）再执行本迁移';
    END IF;
    IF string_to_array(ext_version, '.')::int[] < ARRAY[0, 8, 0] THEN
        RAISE EXCEPTION 'pgvector % 低于 0.8.0：带过滤的查询无法走 HNSW（迭代索引扫描 0.8.0 起支持），请升级 pgvector 扩展/镜像后重试', ext_version;
    END IF;

    SELECT format_type(a.atttypid, a.atttypmod) INTO column_type
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relname = 'asset_retrieval_embeddings_v2'
       AND a.attname = 'embedding_vector_vec'
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF column_type IS DISTINCT FROM 'vector(1024)' THEN
        RAISE EXCEPTION 'embedding_vector_vec 列类型为 %（应为 vector(1024)）：表结构漂移，请先人工核对库形态', COALESCE(column_type, '<列缺失>');
    END IF;

    SELECT pg_get_indexdef(i.indexrelid), i.indisvalid
      INTO existing_def, existing_valid
      FROM pg_index i
      JOIN pg_class ic ON ic.oid = i.indexrelid
      JOIN pg_namespace n ON n.oid = ic.relnamespace
     WHERE n.nspname = 'public' AND ic.relname = 'idx_are_v2_vec_hnsw';
    IF existing_def IS NOT NULL THEN
        IF NOT existing_valid
           OR position('using hnsw' in lower(existing_def)) = 0
           OR position('vector_cosine_ops' in lower(existing_def)) = 0
           OR position('embedding_vector_vec' in lower(existing_def)) = 0 THEN
            EXECUTE 'DROP INDEX public.idx_are_v2_vec_hnsw';
        END IF;
    END IF;
END $$;

SET maintenance_work_mem = '512MB';

CREATE INDEX IF NOT EXISTS idx_are_v2_vec_hnsw
    ON asset_retrieval_embeddings_v2
    USING hnsw (embedding_vector_vec vector_cosine_ops);
