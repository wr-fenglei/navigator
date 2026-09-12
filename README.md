# Explore Discovery Loops

探索发现循环是一个用于日常任务的通用 Codex 技能, 从模糊想法中明确目标和验收标准, 通过苏格拉底式提问检查假设, 找到可行路径, 再拆成能实施和验证的循环, 由主 agent 按依赖安排执行并核验结果

探索, 拆解和实施放在同一个技能里, 目标不清楚就先探索, 已有方案就从拆解开始, 做法明确就直接实施, 也可以只完成其中一个阶段

## 安装

在 Codex 中请求安装这个仓库里的技能

```text
使用 $skill-installer 从 https://github.com/wr-fenglei/explore-discovery-loops 安装 explore-discovery-loops 技能
```

仓库根目录就是技能目录, 入口是 `SKILL.md`, 安装时保留 `references/` 和 `agents/` 的相对位置

自己维护这个技能时, 可以直接把仓库克隆到全局技能目录, 让安装位置和 Git 工作目录共用一份源码, 下面的命令适用于 `CODEX_HOME` 未设置且目标目录不存在的情况

```sh
git clone https://github.com/wr-fenglei/explore-discovery-loops.git ~/.codex/skills/explore-discovery-loops
```

如果设置了 `CODEX_HOME`, 使用它下面的 `skills/explore-discovery-loops` 目录, 安装后新建一个会话, 如果技能没有出现, 重启 Codex 后再检查

## 使用

在桌面端输入 `/`, 在命令列表中搜索 `explore-discovery-loops` 或 `探索发现循环` 并选择这个技能, 也可以输入 `$explore-discovery-loops` 后补充任务要求, 已启用技能会出现在斜杠命令列表中, 见 [官方说明](https://learn.chatgpt.com/docs/reference/slash-commands)

从想法开始推进

```text
$explore-discovery-loops 帮我明确这个想法的目标, 拆解方案并推进到验收
```

只讨论目标

```text
$explore-discovery-loops 帮我探索这个想法, 这次只讨论目标和验收标准, 先不实施
```

从已有计划继续

```text
$explore-discovery-loops 按这份计划继续执行, 核对已有产物和证据, 推进到约定的验收结果
```

需要之后自动继续或定时运行时, 还要使用宿主提供的调度能力, 只有调度实际建立后才会按约定恢复任务

## 界面与调用配置

`agents/openai.yaml` 是技能的 UI 元数据和调用策略配置, 斜杠菜单会使用技能的界面信息, 这里配置显示名称, 简介, 选择技能时的默认提示, 以及是否允许 Codex 根据任务自动选择这个技能, 见 [官方配置说明](https://learn.chatgpt.com/docs/build-skills#optional-metadata)

本技能设置 `allow_implicit_invocation: true`, 允许按任务自动选择, 这项设置不保证每次任务都会调用, 需要明确使用时输入 `$explore-discovery-loops` 或从 `/` 菜单选择

## 文件说明

- [SKILL.md](SKILL.md): 阶段选择, 表达方式, 决策条件和任务记录
- [探索](references/explore.md): 澄清目标, 检查假设, 取得选择路径所需的证据
- [拆解](references/plan.md): 安排实施循环, 依赖和验证方式
- [实施](references/execute.md): 调度执行, 核验结果, 处理中断和后续运行
- [agents/openai.yaml](agents/openai.yaml): 技能的 UI 元数据和调用策略

## 维护

在 `main` 分支维护技能源码, 用 Git 提交记录版本, 规则只写在对应文件里, README 保留安装和使用说明

修改阶段规则后, 检查相关场景是否仍按约定推进和停止, 提交前运行 `git diff --check`, 推送后核对远端提交和文件内容

单独维护这个技能时, Git 仓库可以直接放在全局的 `skills/explore-discovery-loops` 目录里, 不依赖按日期建立的会话目录, 修改后检查差异并提交, 具体任务的材料, 进度和证据放在任务目录里

如果使用独立的开发目录, 可以把全局技能入口链接到仓库, 复制安装的版本需要另行同步
