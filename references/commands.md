# 角色与命令

只需要 Python 3, 脚本使用标准库中的 SQLite, 下文 `SCRIPT` 指技能目录中的 `scripts/navigator.py` 的绝对路径

```sh
python3 "$SCRIPT" init --root /absolute/project
python3 "$SCRIPT" status --access /absolute/project/.navigator/main.json --data request.json
```

`init` 返回数据库和主 agent 访问文件的路径, 其余命令读取 `--data` 指定的 JSON 文件, 也可用 `--data -` 从标准输入读取, 无请求内容时省略 `--data`, 输出均为 JSON, 失败时 `ok:false` 并返回非零退出码

## 主 agent

使用 `init` 返回的 `main.json`, 不把它交给循环任务

| 命令 | 请求内容 | 作用 |
| --- | --- | --- |
| `create` | `blueprint` | 建立任务, 蓝图至少含 `goal` 和非空的 `acceptance` 列表 |
| `blueprint` | `task, revision, blueprint, reason` | 修改蓝图, 保留旧记录并使原有结果待重验 |
| `plan` | `task, revision, loops` | 保存或调整计划, 先处理仍在执行的旧循环 |
| `status` / `history` / `ready` | `task` | 查询当前情况 / 过程记录 / 可启动循环 |
| `claim` | `task, loop, request_id` | 领取循环, 生成派发包, 同一请求重试沿用原标识 |
| `bind` | `task, run, worker_id, host_id, workdir` | 保存创建任务工具返回的实际标识和工作目录 |
| `change` | `task, loop, reason` | 记录输入或环境变化, 使该循环及受影响的后续结果待重验 |
| `reconcile` | `task, run, stopped, note` | 查询实际任务后记录核对结果, 确认停止才释放未通过的执行记录 |
| `pause` / `resume` | `task, note` | 暂停派发 / 核对暂停期间的执行记录后恢复 |
| `finish` | `task` | 所有循环通过且包含整体验收循环后完成任务 |

`revision` 从 `create` 或 `status` 读取, 用来避免用旧计划覆盖新计划, `request_id` 由主 agent 为一次派发命名, 结果不明时沿用它核对, 不换一个标识重发

每个循环的数据格式

```json
{
  "id": "L1",
  "title": "完成目录样例",
  "phase": 1,
  "kind": "work",
  "goal": "整理一份可继续维护的目录",
  "acceptance": ["目录中的来源都能打开"],
  "deps": [],
  "scope": ["/absolute/project/catalog.md"],
  "inputs": {"source": "/absolute/project/source.md"}
}
```

`kind` 可用 `work`, `check`, `acceptance`, 最后一种用于整体验收, `deps` 填前置循环标识, `phase` 用于排序, 输入及环境变化时由主 agent 调用 `change`, 文件产物变化也会在读取状态和继续执行时被检查

整体验收必须通过依赖关系覆盖全部循环, 漏掉时 `plan` 会拒绝保存, 已完成任务修改计划后变为暂停, 核对后用 `resume` 继续

## 循环任务与验证 subagent

按派发提示保存访问文件, 文件名区分执行记录和角色, 只能调用对应角色的命令

| 角色 | 命令 | 请求内容 |
| --- | --- | --- |
| 循环任务 | `context` | 无, 读取蓝图, 本循环和前置结果 |
| 循环任务 | `submit` | `summary, artifacts, conditions`, 产物是本地文件的绝对路径列表 |
| 循环任务 | `block` | `reason`, 说明受阻, 等主 agent 核对后继续安排 |
| 循环任务 | `review-packet` | 无, 提交产物后取得独立验证包 |
| 验证 subagent | `context` | 无, 读取原定标准, 产物与运行条件 |
| 验证 subagent | `verify` | `passed, report, reviewer_id`, 检查报告为本地文件的绝对路径 |

`conditions` 只填影响复现的事实: `source`, `source_version`, `environment` 为字符串, `commands` 为命令字符串列表, 均可省略, 不放自查结论, 无需补充时填 `{}`

修复或重验使用新的执行记录, 旧记录保留, 改计划前先通过 `reconcile` 核对并释放未结束的记录, 暂停只改变派发状态, 主 agent 仍需用 Codex 工具通知正在执行的任务

访问文件用于避免角色和循环用错命令, 同机文件访问仍受宿主权限管理, 脚本不能证明验证者的真实身份或判断报告内容, 主 agent 要核对工具返回的身份与检查结果
