# Navigator

导航者通过苏格拉底式提问明确目标, 把方案拆成可验证的循环, 由主 agent 协调实施直到验收, 简单任务直接做, 也可以只探索或只拆解

## 安装与使用

可请求 `$skill-installer` 从本仓库安装, 自己维护时可直接克隆到全局技能目录, 以下命令适用于 `CODEX_HOME` 未设置且目标目录不存在的情况

```sh
git clone https://github.com/wr-fenglei/navigator.git ~/.codex/skills/navigator
```

设置了 `CODEX_HOME` 就使用它下面的 `skills/navigator`, 保留仓库目录结构, 若安装或修改后未显示, 重启 Codex

输入 `/` 搜索 "导航者", 或使用 `$navigator`, 例如

```text
$navigator 明确这个想法的目标, 拆解并推进到验收
$navigator 这次只讨论目标和验收标准
$navigator 核对已有产物和证据, 按计划继续
```

## 文件与维护

- [SKILL.md](SKILL.md): 阶段选择, 协作和任务记录
- [探索](references/explore.md), [拆解](references/plan.md), [实施](references/execute.md): 各阶段的判断和动作
- [agents/openai.yaml](agents/openai.yaml): UI 元数据和调用策略, 包含名称, 简介和默认提示

允许按任务自动选择不保证每次调用, 之后自动继续需要实际建立调度, 见 [配置说明](https://learn.chatgpt.com/docs/build-skills#optional-metadata) 和 [斜杠菜单说明](https://learn.chatgpt.com/docs/reference/slash-commands)

源码用 Git 管理, 安装位置与工作目录共用一份, 任务材料放在任务目录, 修改规则后检查相关场景, 提交前运行 `git diff --check`, 推送后核对远端提交和文件
