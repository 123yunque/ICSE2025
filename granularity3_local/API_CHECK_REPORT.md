# 粒度三大模型 API 与调用条件检查

检查日期：2026-08-13

## 1. 检查结论

- 调用代码具备 OpenAI-compatible Chat Completions 接口适配能力。
- 推荐运行环境 `Npflower` 可正常导入 `openai 1.109.1`，同时具备同步与异步客户端。
- 已改为只读取当前会话的 `YUNWU_API_KEY`，不再读取旧项目 `pipeline_config.py`。
- 已在 `Npflower` 环境完成真实粒度三 probe 请求：模型 `gpt-5.4` 正常返回，耗时约 2.38 秒，共使用 664 tokens。
- 返回内容可成功解析为 JSON；`next=B002` 与本地 oracle 一致。`delta` 的语义一致，但模型用字符串 `"<unbound>"` 表示未定义值，而 oracle 使用 `{"$undefined": true}`，因此严格 exact match 为 false。
- 当前 `YUNWU_API_KEY` 与旧配置密钥不同；旧配置密钥的额度不足结果只作为历史诊断保留。
- 旧项目配置文件含明文默认密钥，存在泄露风险。该密钥应立即轮换，并删除所有代码内默认值；运行时只从环境变量读取。

最新机器可读结果见 `api_smoke_result_yunwu_env.json`；`api_smoke_result_npflower.json` 是旧密钥额度不足记录，`api_smoke_result.json` 是早期网络失败记录。

## 2. 已确认的接口配置

| 项目 | 当前情况 | 判定 |
|---|---|---|
| API 协议 | OpenAI-compatible Chat Completions | 可用 |
| Python SDK | `openai 1.109.1`（Npflower） | 可用 |
| 同步客户端 | `OpenAI` | 可用 |
| 异步客户端 | `AsyncOpenAI` | 可用 |
| Base URL | `https://yunwu.ai/v1` | 网络及 API 路由可达 |
| 当前环境密钥 | `YUNWU_API_KEY` | 可调用 |
| 模型名 | `gpt-5.4` | 已完成真实响应验证 |
| temperature | `0` | 适合确定性评测 |
| SDK 内部重试 | `0` | 合理，由实验层统一重试 |
| 外层最大尝试 | 旧流程默认 `4` | 可用 |
| 单请求超时 | 旧流程默认 `240 s` | 可用，冒烟建议 `60 s` |
| 并发数 | 旧流程默认 `1` | 首轮合理，连通后再压测 |

## 3. 模型调用的必要条件

调用前必须同时满足：

1. 在 `Npflower` 环境运行。
2. 通过环境变量设置 `YUNWU_API_KEY`，代码和命令行参数中不出现明文密钥。
3. 通过 `YUNWU_API_BASE_URL` 设置网关，并确保 DNS、TCP/443 和 TLS 均可用。
4. 通过 `YUNWU_MODEL` 设置模型名。
5. 网关模型列表中存在所配置模型；应先用一个最小请求验证模型名。
6. 本地 probe 目录至少包含 `model_case.json` 和 `model_inputs.jsonl`。
7. `answers.jsonl` 只留在本地判分端，严禁拼入模型 prompt。
8. 模型必须按约定返回单个 JSON 对象，不带 Markdown 和解释文本。

安全调用示例：

```powershell
$env:YUNWU_API_KEY = Read-Host 'API key'
$env:YUNWU_API_BASE_URL = 'https://yunwu.ai/v1'
$env:YUNWU_MODEL = 'gpt-5.4'
conda run -n Npflower python -m granularity3_local.api_smoke `
  --probe-dir 'granularity3_local\local_experiment\cases\task_109\original\input_1\probes' `
  --output 'granularity3_local\api_smoke_result.json' `
  --timeout 60
Remove-Item Env:YUNWU_API_KEY
```

## 4. 本次冒烟请求实际比较什么

模型可见内容：函数代码块、CFG 边、具体函数输入、当前 block、当前 block 入口状态、允许的直接后继以及字段定义。

模型需要返回：

```json
{
  "next": "B002",
  "delta": {
    "count": {
      "before": {"$undefined": true},
      "after": 0
    }
  }
}
```

本地判分器将模型结果与 `answers.jsonl` 中对应 `probe_id` 的 oracle 比较：

- `next`：直接进入的下一个 basic block ID；
- `delta`：当前 block 执行前后的规范化状态增量；
- `return`：仅 return 事件比较；
- 最终同时报告字段准确性与严格 exact match。

## 5. 恢复调用后的放量条件

建议按以下门槛逐级放量：

1. 单 probe 连续成功 3 次，JSON 解析成功率为 100%。
2. 10 个 probe 小样本成功，记录响应时间、token 数和 exact match。
3. 并发从 1 提升到 2，再到 4；只有无 429、超时和连接抖动时继续提升。
4. 固定 `temperature=0`，SDK 重试保持 0，外层对 429、5xx、超时和连接错误做有界指数退避。
5. 每条响应保存 `case_id/probe_id/model/latency/usage/raw_response/parsed_prediction`，但不保存密钥。
6. 全量运行前估算 probe 数、平均 prompt token、平均 completion token、成功率和预计成本。

当前已通过单 probe 冒烟测试。下一步应先运行 10 个 probe 的小规模实验，验证格式稳定性和成本后再扩大调用规模。
