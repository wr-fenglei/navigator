# Navigator

导航者通过提问明确目标, 拆出多个有关联的实施循环, 由主 agent 协调, 独立 subagent 验证, SQLite 保存过程和当前状态

## 安装与使用

需要 Python 3, 可用 `$skill-installer` 从本仓库安装, 或在目标目录不存在时克隆

```sh
git clone https://github.com/wr-fenglei/navigator.git ~/.codex/skills/navigator
```

输入 `/` 搜索 "导航者", 或直接使用 `$navigator`, 例如

```text
$navigator 我想减少每周整理资料花的时间
$navigator 我有个想法, 先聊聊, 暂时不做
$navigator 接着处理上次那件事
```

[SKILL.md](SKILL.md) 说明设计和流程, [角色与命令](references/commands.md) 说明数据库操作, [交接格式](references/handoff.md) 说明任务怎样接收上下文和返回结果

这是本地同机版本, 脚本不自动创建 Codex 任务或建立后台服务, 实际派发和后续调度由主 agent 使用可用工具完成

运行检查

```sh
python3 -m unittest discover -s tests -v
```
