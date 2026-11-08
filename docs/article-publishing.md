# 本地一键发表工具

`markdown-library publish-article` 接收固定 JSON 请求，完成私人 note 到独立 Post 的准备，或直接修改已有 Post，再执行选版、限定同步、API 正式版核对、归档与双向入口维护。程序不调用模型，不轮询前台、不等待静态构建；原 `publish`、`sync` 等底层命令继续保留。当前 MCP 内容连接不开放此命令，不需要为本地使用接入或重启 MCP。

## 调用

```sh
markdown-library --vault /absolute/path/to/vault publish-article --schema

# 只读预览：不读取凭据，不访问网络，不修改库
markdown-library --vault /absolute/path/to/vault publish-article \
  --request request.json

# 用户已明确授权发表后，一次执行
markdown-library --vault /absolute/path/to/vault publish-article \
  --request request.json --apply \
  --client-env-file /absolute/private/client.env
```

请求也可由 stdin 传入：`--request -`。JSON 是工具参数，不是逐次编写的脚本。Python 调用方可直接使用 `article_publish.publish_article(library, request, client=client, apply=True)`；JSON Schema 由 `--schema` 或模块的 `REQUEST_SCHEMA` 提供，两者使用同一运行时校验。

`--client-env-file` 显式指定已有私密配置，其中包含 `HOMEPAGE_URL` 与 `HOMEPAGE_API_KEY`。文件必须仅本人可读写；内容作为 dotenv 数据解析，不执行 shell、变量展开或命令替换。不传文件时，沿用已配置的同名环境变量，也可用 `--url` 与 `--api-key-env` 显式选定目标和凭据。凭据从不放进请求、笔记或回执。

## 请求示例

```json
{
  "source_note_id": "原稿稳定ID",
  "expected_source_hash": "刚读取的原稿SHA256",
  "title": "2026-03-17 某公司面试复盘：任务调度",
  "published_at": "2026-03-17T12:00:00+08:00",
  "link": "interview-task-scheduler-2026-03-17",
  "categories": ["求职"],
  "replacements": {"示例公司名称": "某公司"},
  "forbidden_terms": ["示例公司名称"]
}
```

- 从私人稿准备文章时，`source_note_id`、`expected_source_hash`、`title`、`published_at` 必填。日期必须带时区；工具不猜日期或把当前时间填为历史日期。调用者依据内容库的显示日期规则确定时区和时刻。
- 未提供 `public_markdown` 时，取原稿正文，移除程序维护的发表导航，用公开标题替换开头的一级标题，并应用 `replacements`。原稿保持原文；不会把原稿的公司、角色或其他私人元数据复制到 Post。
- `replacements` 是同时、区分大小写的字面替换，仅作用于正文；标题和其他公开字段由请求明确指定。若需要语义匿名化，Agent 先完成内容判断，然后传入 `public_markdown`，程序无需再调用模型。该字段仅包含正文，不包含 YAML frontmatter。
- `forbidden_terms` 对最终公开投影进行不区分大小写的检查，覆盖正文、标题、描述、URL、分类、标签及沿用的公开属性。它是指定词检查，不承诺自动发现所有敏感信息。
- `link` 缺省时沿用已有 Post URL；新 Post 使用基于原稿 ID 的稳定中性 URL。`locale` 缺省沿用已有值，新稿默认 `zh`。可选 `description`、`tags`、`categories`；已有 Post 的未指定属性保留。
- 工具查找稳定来源，包括归档稿和历史来源关系。已有 Post 需要被修改时，必须提供它的 `expected_post_hash`；预览会返回该值及 `requires_post_hash`，Agent 读取比较后填写，用户不管理这些指纹。相同请求重复执行会复用 Post，不重复新建；仅导航变化允许复用原指纹，正文变化会拒绝。
- 直接修改已有发表稿时，改用 `post_id`、`expected_post_hash`、`title`，与 `source_note_id` 二选一。不传 `public_markdown` 则保留该 Post 的正文，只替换开头的一级标题，不从私人原稿重新复制；不传 `published_at`、`link` 等字段则保留原值。可用于只改标题、公开属性或已批准正文。即使没有私人来源关系也能更新既有 Post；有明确来源时仍维护入口。相同请求因网络异常重试时，仅当本地稿恰好等于该请求的预期结果才接受旧指纹，不覆盖独立新增修改。
- 私人来源入口接受 `note`；Work 正文继续使用 `prepare-work-review`，作品卡片继续走原独立流程。

只改已有文章标题的请求：

```json
{
  "post_id": "已有发表稿ID",
  "expected_post_hash": "刚读取的发表稿SHA256",
  "title": "【暑期实习】某公司算法一面复盘-树的直径与梯度下降"
}
```

## 返回与失败处理

```json
{
  "status": "published",
  "api_verified": true,
  "post_id": "独立发表稿ID",
  "remote_id": 1,
  "url": "https://example.com/post/interview-task-scheduler-2026-03-17",
  "frontend": "asynchronous",
  "local_maintenance": "complete"
}
```

`published` 表示标准 API 的后台最新版本和正式版均回读匹配批准稿，包括正文、代码和元数据；不表示前台已验收。网站异步更新由已有发布器处理，不影响命令返回。索引的全局状态页继续由现有同步定时任务维护，本命令只更新本篇及其原稿的导航。

冲突或未知写入结果返回 `needs_review`，CLI 退出码为 2，保留现有同步账本和同一个 Post 身份；后续通过原同步核对机制处理，不盲目新建、不清账本。异常同样返回非零，不能据此推断远端未发生写入。API 已核对成功但本地收尾失败时，返回 `published` 和 `local_maintenance: needs_review`，避免把已发布的文章误报为未发布；修复后复用原请求和身份完成收尾。

发布过程以当前文件哈希和既有同步守卫保护并发修改，只同步目标 Post，完整远端清单仅用于身份和冲突判断。发布请求的业务授权由调用者负责，预览本身不构成发表授权。
