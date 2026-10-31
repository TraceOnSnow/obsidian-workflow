# Markdown 内容库与 Obsidian 工作流

把普通 Markdown 笔记、作品记录和网站发表串成一条可检查的工作流。日常在 Obsidian 写作，Python CLI 管理稳定身份、来源和历史；需要发表时创建独立 Post，再通过标准 API 与 Payload 网站同步。

这是个人使用项目的公开源码快照。仓库不包含真实笔记、聊天档案、账号、凭据、运行状态或第三方插件 bundle。

## 做了什么

- **内容库 CLI**：新建、原位纳管、读取、修改、历史恢复和可恢复删除，输出 JSON，供人和 Agent 共用。
- **作品总表**：在 Markdown Super Table 中编辑评分、进度、短评和体验日期；有实质感想才保存关联正文。评分为 0–100，0 有效，空值未知。
- **独立发表稿**：私人 note／Work 与 Post 分开保存，保留来源和双向入口。修改原稿不会自动覆盖 Post，作品卡片也不会带出私人正文。
- **同步守卫**：使用当前文件哈希、共同基线和远端版本检查并发修改；双端冲突保留待处理，未知写入先对账，成功回读后才推进基线。扫描缺失不会自动删除网站记录。
- **MCP 与技能复用**：CLI、stdio MCP 和 Skill 使用同一内容库实现；HTTP MCP 另有 OAuth／PKCE 接入代码。运行服务和连接模型由使用者单独配置。

## 本地试用

需要 Python 3.11+。以下操作只创建临时示例库，不访问网站，也不调用模型。

```sh
git clone https://github.com/TraceOnSnow/obsidian-workflow.git
cd obsidian-workflow
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
markdown-library --help

demo_vault="$(mktemp -d)"
markdown-library --vault "$demo_vault" init
printf '%s\n' '# 一条示例笔记' '' '这是临时库中的合成内容。' > "$demo_vault/input.md"
markdown-library --vault "$demo_vault" create "$demo_vault/input.md" --title 示例笔记
markdown-library --vault "$demo_vault" list
```

`create` 返回稳定 ID 与哈希；可用 `get ID` 读取完整当前稿。修改、恢复和发布选择需要各命令要求的守卫值，参数以 `markdown-library --vault "$demo_vault" COMMAND --help` 为准。已有真实库应先备份并阅读对应命令，不套用临时库初始化流程。

可选 stdio MCP：

```sh
python -m pip install -e '.[mcp]'
markdown-library-mcp --help
markdown-library-mcp --vault "$demo_vault" --read-only
```

最后一条启动 stdio 服务，供兼容 MCP 的客户端连接。去掉 `--read-only` 会开放当前实现提供的写入工具，应按客户端权限配置使用。

## 发表与同步

普通保存只更新当前稿。`publish` 选择指定快照，之后的 `sync` 才负责与网站交互；`sync` 默认预览，实际应用需要 `--apply`。首次公开与 Post 发表应有明确授权。需要同步的目标必须实现本项目使用的 Payload 字段、草稿、版本和生命周期约定。

```sh
markdown-library --vault /absolute/path/to/vault sync --help
markdown-library --vault /absolute/path/to/vault sync \
  --url https://example.com --api-key-env HOMEPAGE_API_KEY
```

凭据由使用者在私密环境中配置到命名环境变量，不写入笔记、Skill 或 Git。预览会读取远端；不要把示例 URL 换成未获授权的服务。

`scripts/` 和 `deploy/` 中部分脚本是作者部署示例，保留了特定目录、URL、服务和凭据文件约定。使用前应逐项检查，并显式选择自己的库路径、目标 URL、凭据来源及备份位置；部分常量需要在本地副本中调整。克隆仓库不会自动完成 Obsidian Sync 登录、安装插件、启动服务或接通生产网站。

## 代码入口

| 目录 | 内容 |
| --- | --- |
| `src/markdown_library/` | 内容模型、事务、同步、关系、生命周期及 MCP |
| `tests/` | 临时库、合成数据和假传输的回归测试 |
| `skills/markdown-library/` | CLI 工具规约与包装入口 |
| `assets/` | Copilot 技能、记录模板和 Super Table 安装元数据 |
| `scripts/`、`deploy/` | 安装、维护、验证和部署示例 |

```sh
PYTHONPATH=src python -m unittest discover -s tests -q
```

部分集成测试或工具需要额外运行环境，具体要求以测试和脚本为准；源码测试通过也不代表 Obsidian 本机 UI 或外部服务已经验收。

相关项目：[历史材料整理管线](https://github.com/TraceOnSnow/chatgpt-pipeline)、[Payload 网站](https://github.com/TraceOnSnow/homepage)。本项目负责入库后的内容维护与同步；原始聊天档案、私人库和网站数据库分别保存。
