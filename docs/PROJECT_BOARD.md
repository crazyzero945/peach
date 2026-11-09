# 项目看板与交付关联

[Peach Requests & Roadmap](https://github.com/orgs/peach-mitao/projects/7)是唯一持续同步的公开看板，展示项目 Issue、计划范围与交付状态。看板的标题、说明、视图、字段与选项一律用英文，Issue 保留原标题；维护者提出的任务和使用者提交的反馈都是应用仓库的 Issue，按作者身份写进 `Source` 字段，再由视图区分。个人馆藏修正、账号登录与设备维护留在私有清单。

「Board」展示全部项目需求，「Maintainer tasks」和「Community feedback」按 `Source` 筛选，「Needs reply」列出等维护者回复的反馈，「Roadmap」按优先级展示开放事项，「Awaiting verification」列出已发布待验收的事项。README 顶部和仓库 Projects 提供入口。视图不改变公开范围，同一事项只维护一份。

全部 Issue 都在[应用仓库](https://github.com/peach-mitao/peach/issues)。原规划仓库 `peach-mitao/peach-roadmap` 已归档，开放事项已用 GitHub 原生转移搬回应用仓库，原 URL 自动跳转。

公开仓库的 Issue 始终按仓库权限可见，将它加入私有看板不会隐藏原 Issue。正式事项使用 GitHub Issue 编号；草稿保留已确认范围与优先级，相关事项使用可访问的链接说明关联。

## 状态与版本

| 字段 | 含义 |
| --- | --- |
| Status | `Needs triage` 待评估、`Backlog` 待安排、`In progress` 进行中、`Needs verification` 待验证、`Done` 已完成、`Canceled` 已取消；PR 另有 `Closed unmerged` 已关闭未合入，描述真实处理结果 |
| Source | Issue 作者是组织成员、所有者或协作者为 `Maintainer`，其他人为 `Community` |
| Reply | 开放 Issue 里外部用户最后发言、维护者还没接话为 `Needs reply`，维护者回复后为 `Replied`；机器人发言不计，只有维护者在说的 Issue 留空 |
| Priority | P0 紧急、P1 优先、P2 常规、P3 低优先级 |
| Implementation | 已关联的 PR 或经核对的实现提交 |
| Delivery | `No linked work` 未关联实现、`Not merged` 尚未合入、`Merged` 已合入待发布、`Released` 已发布 |
| Release、Release notes | 首次实际包含实现的可下载版本及其链接 |
| Milestone | 计划交付范围；与实际发布版本分别记录 |

PR 合入表示代码已集成。开放的需求进入 `Needs verification`；完成验收后关闭 Issue 才进入 `Done`。关闭原因是「不计划」时进入 `Canceled`。发布版本不会代替目标环境或报告者验收。

## 自动同步

应用仓库的 `.github/workflows/project-board.yml` 响应本仓库 Issue 开关和标签变化、PR 建立和合入、主线推送、Release 发布与变动，Issue 的新建、评论、标签与开闭由每小时一次的定时检查同步。使用主线代码，不检出或执行外部 PR 的代码。所有同步串行执行，每次读取当前事实，重复执行不会重复建卡或推进状态。

- 公开路线图自动收录应用仓库的开放 Issue。PR 用于计算实现关联，内部草稿不复制到公开 Project。
- `Source` 按 Issue 作者身份、`Reply` 按最后发言人同步；分类视图使用同一份 Issue。
- 重新打开的事项按实际实现恢复为 `Backlog` 或 `Needs verification`，已关联开放 PR 的事项为 `In progress`。
- 应用仓库 PR 的 GitHub 关联和独立关联行建立实现关联。使用 `Refs #编号` 或完整 Issue URL。只接受配置仓库和迁移映射里的旧仓库，正文中普通提及某个编号不会被当成交付。
- 仓库名与编号共同标识 Issue；`issue_aliases` 保存迁移映射，旧提交和 PR 里写的 `Refs peach-mitao/peach-roadmap#编号` 仍能指向转移后的 Issue。GitHub 原生转移保留历史讨论，原 Issue URL 自动跳转。
- 已合入 PR 对应的开放需求进入 `Needs verification`；`status:needs-info` 和 `status:needs-verification` 分别保留等待信息和验收的含义。
- 从有下载制品的已发布 Release 中检查标签提交祖先关系，填写首次包含实现的版本。只有标签、失败构建和发布草稿均不算已发布。
- 通过 Actions 的仓库令牌创建 Release 不会触发另一条 Release 工作流；发布工作流在上架后显式调用同一同步入口。
- 不关闭 Issue，不合入 PR，不修改优先级、排期或归档状态。已归档事项不自动恢复。

历史直接集成而没有 PR 的实现，在 `.github/project-board.json` 的 `implementation_commits` 中以 `组织/仓库#编号` 为键登记完整提交 SHA；同步同样验证其是否属于应用主线和发布标签，不凭文字宣称完成。

## 凭据与验证

组织 Project 不接受仓库默认的 `GITHUB_TOKEN`。同步用组织安装的 GitHub App「peach-board」现取令牌：只授予组织 Project 写权限与应用仓库内容、Issue、PR 的只读权限，令牌一小时过期。App 的 Client ID 存为仓库变量 `PROJECT_BOARD_APP_CLIENT_ID`，私钥存为 Actions Secret `PROJECT_BOARD_APP_KEY`，由官方 [create-github-app-token](https://github.com/actions/create-github-app-token) 换取令牌；私钥不得写入代码、日志或事件正文。

本地运行 `scripts/project_board.py` 默认只预览差异，显式传入 `--apply` 才写入。回归覆盖开关、合入、待验收、发布祖先关系和跨仓库引用；线上验收使用手动触发和实际看板读回。
