# 交接格式

主 agent 领取循环后, 脚本返回 `run`, `new`, `packet` 和 `prompt`, `run` 是这次执行的标识, 循环重试时会有新的 `run`

## 派发循环

1. 调用 `claim`, 保存返回的包, `new:true` 表示刚领取, `new:false` 表示已有这次派发, 先查询已有任务, 不重复创建
2. 在用户已要求新建任务的范围内, 用 `prompt` 作为新任务的完整说明, 项目任务先确认项目和工作目录, 创建返回仍在准备时等待实际 `threadId`, 不把 `clientThreadId` 当成它
3. 取得实际标识后调用 `bind`, 再关注该任务返回的结果, 循环任务可先读上下文, 主 agent 绑定前不能提交产物

派发包由脚本生成, 不手填 token

```json
{
  "protocol": "navigator/v1",
  "db": "/absolute/project/.navigator/state.sqlite",
  "role": "worker",
  "token": "脚本生成的本循环访问值",
  "task": "任务标识",
  "loop": "L1",
  "run": "本次执行标识",
  "script": "/absolute/skill/scripts/navigator.py",
  "context": {"blueprint": {}, "loop": {}, "dependencies": {}, "run": {}}
}
```

新任务按提示保存包, 文件名为 `access-<run>-worker.json`, 验证者使用 `access-<run>-verifier.json`, 避免同目录覆盖, 通过 `context` 读取蓝图, 本循环要求和前置结果, 不附带主会话全文, 数据库沿用原绝对路径

如果创建任务成功但尚未保存标识就中断, 用 `run` 和已有任务核对, 找到后 `bind`, 确认没有任务在运行才 `reconcile` 释放记录, 不凭数据库未绑定就重发

## 实施与独立验证

循环任务提交下面这样的 JSON, 脚本计算文件校验值, 不接受把自查结论当成通过

```json
{"summary":"已完成目录样例","artifacts":["/absolute/project/catalog.md"],"conditions":{"source_version":"v1"}}
```

然后调用 `review-packet`, 用返回的 `prompt` 派发新上下文的独立 subagent, 把工具返回的实际标识告知它, 用作 `reviewer_id`, 验证包只给原定要求, 产物和运行条件, 不附带实施者的总结, 验证者不再派发验证者

验证者保存检查报告并提交

```json
{"passed":true,"report":"/absolute/project/check.md","reviewer_id":"实际验证 subagent 标识"}
```

任务结束时原样返回脚本回执, 可另加一句结果说明

```json
{"ok":true,"task":"任务标识","loop":"L1","run":"本次执行标识","state":"passed","report":{"path":"/absolute/project/check.md","sha256":"脚本计算的值"}}
```

无法取得独立验证工具就保留待验证, 把验证包交给主 agent 补派, 主 agent 通过 `status` 核对回执和当前产物, 再查询 `ready` 安排后续循环, `passed` 表示已提交匹配版本的独立检查记录, 不代表脚本替人完成了验收判断
