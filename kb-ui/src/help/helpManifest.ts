// ⚠️ 自动生成：node scripts/sync-help.mjs（predev/prebuild/pretest 自动执行）——不要手改。
// 真相源：docs/平台指南/
export interface HelpChapter {
  id: string
  file: string
  title: string
}

export interface HelpPart {
  key: string
  label: string
  chapters: HelpChapter[]
}

export const HELP_PARTS: HelpPart[] = [
  {
    "key": "intro",
    "label": "开篇",
    "chapters": [
      {
        "id": "01",
        "file": "01-平台是什么.md",
        "title": "平台是什么"
      },
      {
        "id": "02",
        "file": "02-核心概念词典.md",
        "title": "核心概念词典"
      },
      {
        "id": "03",
        "file": "03-三分钟上手.md",
        "title": "三分钟上手"
      }
    ]
  },
  {
    "key": "user",
    "label": "用户篇",
    "chapters": [
      {
        "id": "10",
        "file": "10-登录与概览.md",
        "title": "登录与概览"
      },
      {
        "id": "11",
        "file": "11-知识库管理.md",
        "title": "知识库管理"
      },
      {
        "id": "12",
        "file": "12-文档上传.md",
        "title": "文档上传"
      },
      {
        "id": "13",
        "file": "13-挖掘与审核.md",
        "title": "挖掘与审核"
      },
      {
        "id": "14",
        "file": "14-检索.md",
        "title": "检索"
      },
      {
        "id": "15",
        "file": "15-MCP接入.md",
        "title": "MCP 接入"
      },
      {
        "id": "16",
        "file": "16-一张网接入.md",
        "title": "一张网接入"
      },
      {
        "id": "17",
        "file": "17-挖掘范式.md",
        "title": "挖掘范式（挖掘 Workflow）"
      },
      {
        "id": "18",
        "file": "18-检索范式管理.md",
        "title": "检索范式管理"
      },
      {
        "id": "19",
        "file": "19-LLM任务中心.md",
        "title": "LLM 任务中心"
      },
      {
        "id": "20",
        "file": "20-账号与权限.md",
        "title": "账号与权限"
      },
      {
        "id": "21",
        "file": "21-常见问题与错误码.md",
        "title": "常见问题与错误码"
      }
    ]
  },
  {
    "key": "dev",
    "label": "开发篇",
    "chapters": [
      {
        "id": "30",
        "file": "30-架构总览.md",
        "title": "架构总览：六服务一容器"
      },
      {
        "id": "31",
        "file": "31-模块地图.md",
        "title": "模块地图：每个服务干什么"
      },
      {
        "id": "32",
        "file": "32-数据库与迁移.md",
        "title": "数据库与迁移"
      },
      {
        "id": "33",
        "file": "33-开发流程.md",
        "title": "开发流程"
      },
      {
        "id": "34",
        "file": "34-设计决策档案索引.md",
        "title": "设计决策档案索引"
      }
    ]
  },
  {
    "key": "ops",
    "label": "运维篇",
    "chapters": [
      {
        "id": "40",
        "file": "40-部署与发版.md",
        "title": "部署与发版"
      },
      {
        "id": "41",
        "file": "41-日常运维.md",
        "title": "日常运维"
      },
      {
        "id": "42",
        "file": "42-故障排查历史坑汇编.md",
        "title": "故障排查：历史坑汇编"
      }
    ]
  }
]

export const HELP_CHAPTER_COUNT = 23
