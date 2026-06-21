# 代码规范：Python 风格（python-style）

> 你家的「规矩」——命名、结构、禁用项。**通用脱敏示例**，请替换成你团队的真实规范。

## 命名
- 模块/函数/变量用 `snake_case`，类用 `CapWords`，常量用 `UPPER_SNAKE`。
- 名字说清意图：`active_users` 好过 `data`、`tmp`、`x`。

## 结构
- 函数短小、单一职责；嵌套别超过 3 层，深了就抽函数或提前 return。
- 公开函数写一句 docstring 说清「做什么 + 返回什么 + 失败时怎样」。
- 对外输入 fail-closed：拿不准就拒绝/报错，别静默放行。

## 禁用 / 慎用
- 禁 `shell=True` 拼字符串跑命令；用 argv 列表传参，杜绝命令注入。
- 禁 `eval` / `exec` / `pickle` 处理不可信数据。
- 禁把密钥、密码、token 写死进代码或打进日志。
- 慎用裸 `except:`；要兜底也写 `except Exception` 并说明为什么。

## 工具
- 用 ruff/格式化器统一风格；提交前 lint 必须全过、无新增告警。
