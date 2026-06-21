# 内置示例「秘方」（starter definitions）

仓库**不含**任何真实的工作流 / 技能 / 规范 / QA 标准——你的真东西只躺在你自己的本地数据库里
（`foreman.db` 不进 git，DESIGN §11.2C / §765）。这里放的是一小撮**通用、脱敏**的示例，让开源用户
开箱就有一套能跑、能照着改的「积木」。

## 怎么载入

```bash
foreman seed-examples
```

它会把下面的示例写进你本地库（幂等：已存在同名启用版就跳过，可安全重复跑）。之后在手机/网页 UI 里
随便改——改完只存进你自己的库，不碰文件、不用重新部署。

## 里面有什么

| kind | name | 作用 |
|------|------|------|
| workflow | `add-feature` | 「加新功能」主线剧本：写测试 → 实现 → 自审+lint → push 前审批闸 |
| skill | `write-tests` | 「怎么写测试」做法手册 |
| skill | `implementation-notes` | 「实现要点」做法手册 |
| code_standard | `python-style` | Python 命名/结构/禁用项规矩 |
| code_standard | `test-naming` | 测试命名与放置规矩 |
| qa_rubric | `covers-happy-path` | 验收：测试是否覆盖主路径 |
| qa_rubric | `meets-standard-no-shortcuts` | 验收：实现是否合规范、有没有偷工 |
| qa_rubric | `lint-passes` | 验收：自审 + lint 全过 |

`add-feature` 工作流按名字引用上面的技能/规范/QA——把它们改成你团队的真东西后，剧本不用动也能照跑。

## 格式

- 技能 / 代码规范：Markdown（`*.md`）。
- 工作流 / QA 标准：YAML（`*.yaml`，引擎用 `yaml.safe_load` 解析，只读不执行）。
- `manifest.yaml` 列出每条的 kind / name / 对应文件 / scope（何时适用）。
