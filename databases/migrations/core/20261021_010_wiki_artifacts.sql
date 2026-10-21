-- 63号批次1：wiki 制品通用底座（online_expand，与启动链 017 改/018 同文）。
-- ①batches 通用化：channel 补 wiki_compile/sql_import（sql_import 为表格类 P2 预留位，
--   枚举现在建全，后续加通道只加代码不改表）；details jsonb 装通道专属细节，
--   file_name/sheet_name 保留（存量 upload 批次兼容）。
-- ②kb_artifact_sources：制品来源绑定（kind 无关——表格 kb_import 与 wiki 编译共用）。
-- ③kb_wiki_pages / kb_wiki_page_links / kb_wiki_page_units：wiki 专有三表（63号 §2.4）。
-- ④kb_wiki_page_units HNSW：009 同款守卫（pgvector>=0.8 + 列形态 fail-closed +
--   同名无效/定义漂移索引自愈 + 串行建索引——64MB /dev/shm 坑）。

-- ---------- ① batches 通用化 ----------
ALTER TABLE kb_artifact_batches
    ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 幂等换宽 CHECK（静态 DROP+ADD，同 012_semantic_role_v2 先例）。
ALTER TABLE kb_artifact_batches
    DROP CONSTRAINT IF EXISTS ck_kb_artifact_batches_channel;
ALTER TABLE kb_artifact_batches
    ADD CONSTRAINT ck_kb_artifact_batches_channel
    CHECK (channel IN ('upload','kb_import','wiki_compile','sql_import'));

-- ---------- ② 来源绑定（通用） ----------
CREATE TABLE IF NOT EXISTS kb_artifact_sources (
    id          TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    kb_id       TEXT NOT NULL,
    scope       JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'active',
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_kb_artifact_sources UNIQUE (artifact_id, kb_id)
);
-- artifact 侧前缀查询由 UNIQUE(artifact_id,kb_id) 约束索引覆盖；本索引供
-- kb 侧反查（哪些制品引用了此 KB——KB 清退/重挖场景）。
CREATE INDEX IF NOT EXISTS idx_kb_artifact_sources_kb
    ON kb_artifact_sources(kb_id);

-- ---------- ③ wiki 专有三表 ----------
CREATE TABLE IF NOT EXISTS kb_wiki_pages (
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    page_key    TEXT NOT NULL,
    title       TEXT NOT NULL,
    page_type   TEXT NOT NULL DEFAULT 'topic'
                CONSTRAINT ck_wiki_pages_type
                CHECK (page_type IN ('topic','glossary','contradiction','open_question')),
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    body_md     TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT 'machine'
                CONSTRAINT ck_wiki_pages_origin CHECK (origin IN ('machine','manual')),
    provenance  JSONB NOT NULL DEFAULT '[]'::jsonb,
    flags       JSONB NOT NULL DEFAULT '{}'::jsonb,
    ordinal     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (artifact_id, revision, page_key)
);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_type
    ON kb_wiki_pages(artifact_id, revision, page_type);

CREATE TABLE IF NOT EXISTS kb_wiki_page_links (
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    from_page   TEXT NOT NULL,
    to_page     TEXT NOT NULL,
    anchor_text TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'valid'
                CONSTRAINT ck_wiki_links_status CHECK (status IN ('valid','broken')),
    PRIMARY KEY (artifact_id, revision, from_page, to_page, anchor_text)
);

CREATE TABLE IF NOT EXISTS kb_wiki_page_units (
    artifact_id  TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    unit_key     TEXT NOT NULL,
    page_key     TEXT NOT NULL,
    content_text TEXT NOT NULL,
    embedding    VECTOR(1024),
    embed_status TEXT NOT NULL DEFAULT 'pending'
                 CONSTRAINT ck_wiki_units_embed
                 CHECK (embed_status IN ('pending','done','failed')),
    PRIMARY KEY (artifact_id, revision, unit_key)
);

-- ---------- ④ HNSW（守卫口径照 009：pgvector>=0.8 + 列形态 fail-closed +
--   同名无效/定义漂移索引自愈；串行建索引——容器 /dev/shm 默认 64MB，并行建
--   按 maintenance_work_mem 申请动态共享内存会 ENOSPC，串行走后端本地内存） ----------
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
       AND c.relname = 'kb_wiki_page_units'
       AND a.attname = 'embedding'
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF column_type IS DISTINCT FROM 'vector(1024)' THEN
        RAISE EXCEPTION 'kb_wiki_page_units.embedding 列类型为 %（应为 vector(1024)）：表结构漂移，请先人工核对库形态', COALESCE(column_type, '<列缺失>');
    END IF;

    SELECT pg_get_indexdef(i.indexrelid), i.indisvalid
      INTO existing_def, existing_valid
      FROM pg_index i
      JOIN pg_class ic ON ic.oid = i.indexrelid
      JOIN pg_namespace n ON n.oid = ic.relnamespace
     WHERE n.nspname = 'public' AND ic.relname = 'idx_wiki_units_hnsw';
    IF existing_def IS NOT NULL THEN
        IF NOT existing_valid
           OR position('using hnsw' in lower(existing_def)) = 0
           OR position('vector_cosine_ops' in lower(existing_def)) = 0
           OR position('embedding' in lower(existing_def)) = 0 THEN
            EXECUTE 'DROP INDEX public.idx_wiki_units_hnsw';
        END IF;
    END IF;
END $$;

-- 串行建索引（64MB /dev/shm 坑详见 009）；连接用后即弃，SET 不影响服务运行时配置。
SET max_parallel_maintenance_workers = 0;

SET maintenance_work_mem = '512MB';

CREATE INDEX IF NOT EXISTS idx_wiki_units_hnsw
    ON kb_wiki_page_units
    USING hnsw (embedding vector_cosine_ops);
