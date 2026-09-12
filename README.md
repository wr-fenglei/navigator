# Navigator

导航者通过苏格拉底式提问明确目标, 把方案拆成多个相对独立又相互关联的实施循环, 每个循环都能验证结果, 由主 agent 协调推进直到验收, 简单任务直接做, 也可以只讨论或只做计划

## 安装与使用

可请求 `$skill-installer` 从本仓库安装, 自己维护时可直接克隆到全局技能目录, 以下命令适用于 `CODEX_HOME` 未设置且目标目录不存在的情况

```sh
git clone https://github.com/wr-fenglei/navigator.git ~/.codex/skills/navigator
```

设置了 `CODEX_HOME` 就使用它下面的 `skills/navigator`, 保留仓库目录结构, 若安装或修改后未显示, 重启 Codex

输入 `/` 搜索 "导航者", 或使用 `$navigator`, 直接说想做的事就行, 例如

```text
$navigator 我想减少每周整理资料花的时间
$navigator 我有个想法, 先聊聊, 暂时不做
$navigator 接着处理上次那件事
```

## 说明文件

- [SKILL.md](SKILL.md): 阶段选择, 协作和任务记录
- [探索](references/explore.md), [拆解](references/plan.md), [实施](references/execute.md): 各阶段的判断和动作
