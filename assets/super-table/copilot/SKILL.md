---
name: super-table
description: 编辑当前笔记中的 Super Table 表格，保留其稳定身份与未请求修改的数据。
---

只按用户当前要求修改指定表格。表格保存在 Markdown 的 `super-table` 围栏内，以严格 JSON 表示；先读取当前文件，保存前重新读取并比较，遇到并发修改先合并，不覆盖新内容。保留其他段落与其他表格。

表格结构为 `schemaVersion: 1`、`tableId`、`fields`、`records`、`views`、`defaultViewId`。行使用唯一 `id`，单元格在 `values` 中按字段 `id` 定位，不能按行号或显示名称猜身份。保留所有现有 ID、未知属性、空值、视图、列宽和选项。新增对象需要新的唯一 ID。

字段使用 `text`、`link`、`number`、`date`、`checkbox`、`select`、`multiselect`、`attachment`。单选值使用选项的 `id`，选项对象包含 `id/label/color`；多选值为选项 ID 数组。数字使用有限 JSON number；日期存 `YYYY-MM-DD` 或 `YYYY-MM-DDTHH:mm`，不改未涉及的时间部分。文本支持 Markdown 和 `[[库内笔记]]`。附件仅为相对路径引用，不因删除引用而删文件。

作品评分为0–100：85是85分，0有效，null或空字符串为未评分，5就是5分。作品正文、发表稿、网站信息与发布选择按 personal-library 技能及既有工具维护。表格保存不会自动授予发表或首次公开权限。批量改动不得新增作品身份或改变范围外关系。

视图筛选为 `{"mode":"and","conditions":[]}` 或 `mode:"or"`，条件使用唯一 `id`、`fieldId` 和 `operator`；单选相等条件用 `value:["option-id"]`；`isEmpty` 不把0视为空。排序/分组各使用一个 `{fieldId,direction}`；`visibleFieldIds` 只控制显示。切换视图不修改默认视图。

保存前验证完整 JSON、字段与记录等 ID 唯一、引用有效，确认差异仅限请求范围。保留已有未知/不兼容内容并报告，不能为消除报错静默删数据。表格在其他位置发生变化时重新打开再编辑。

格式依据固定版本 Super Table 1.1.2 作者公开数据规范：
https://github.com/kingbeyond/obsidian-super-table/blob/18f1930b0ab0a8374d4271df70bf22af51e31b4b/SKILL.md
