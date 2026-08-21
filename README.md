# LLM Bench — 专业 LLM API 压测客户端

> 独立 Windows 桌面应用，对 OpenAI 兼容 API 进行专业级压力测试与性能基准。
> 无需 Python 环境，双击即用，可复制到任意 Windows 机器运行。

---

## ✨ 核心特性

### 🎯 全部指标精确测量（无估算、无 N/A）

所有请求统一走 **SSE 流式协议**，精确采集每个 token 的时间戳，确保以下指标全部为实测值：

| 指标 | 说明 |
|---|---|
| 测试耗时 (s) | 单场景总耗时 |
| 并发数 / 总请求数 / 成功 / 失败 | 基础计数 |
| 请求吞吐 (req/s) | 成功请求 / 耗时 |
| 输出 token 吞吐 (tok/s) | 总输出 token / 耗时 |
| 总 token 吞吐 (tok/s) | (输入 + 输出) / 耗时 |
| 平均端到端延迟 (s) | 请求发出到完成 |
| P50 / P90 / P99 延迟 (s) | 延迟分位数 |
| **平均首 token 延迟 TTFT (s)** | 首个 token 返回时间 |
| TTFT P50 / P90 (s) | TTFT 分位数 |
| **平均每 token 生成时间 (s)** | 生成阶段每 token 耗时 |
| **平均 token 间延迟 ITL (s)** | 相邻 token 间隔 |
| ITL P50 / P90 / P99 (s) | ITL 分位数 |
| 每请求平均输入 / 输出 token | Token 统计 |

### 🔌 协议自动检测

默认 `auto` 模式，自动探测目标 API 支持的协议：

```
auto → 探测顺序: chat/completions → completions → responses
```

也支持手动指定：
- **chat** — `/v1/chat/completions`（OpenAI Chat Completions）
- **completions** — `/v1/completions`（OpenAI Legacy Completions）
- **responses** — `/v1/responses`（OpenAI Responses API）

### 📋 多场景矩阵测试

内置测试矩阵，一键加载，覆盖不同并发 × 输入输出组合：

**默认矩阵（6 场景）：**

| 场景 | 并发 | 总数 | 输入 | MaxTok |
|---|---|---|---|---|
| 低并发-短文本 | 1 | 10 | 你好 | 50 |
| 低并发-长文本 | 1 | 10 | 详细介绍AI... | 500 |
| 中并发-短文本 | 5 | 30 | 你好 | 50 |
| 中并发-长文本 | 5 | 30 | 气候变化短文 | 200 |
| 高并发-短文本 | 20 | 100 | 你好 | 50 |
| 高并发-中文本 | 20 | 50 | 解释机器学习 | 100 |

**流式矩阵（6 场景）：** 专为流式压测设计，不同并发 × 长短文本

### 🤖 推理模型兼容

自动解析 `reasoning_content` 字段，兼容 GLM、o1、DeepSeek-R1 等推理模型的流式输出。

### 📦 可移植 exe

17MB 独立程序，不依赖 Python 环境，不依赖固定路径。直接复制到其他 Windows 机器即可运行。

---

## 🚀 快速开始

### GUI 模式（推荐）

双击 `LLM_Bench.exe` 即可打开图形界面：

1. 填写 **Base URL**（如 `http://localhost:8000/v1`）
2. 填写 **API Key**（可选）
3. 点击 **「获取模型」** 自动拉取模型列表并下拉选择
4. 协议保持 **auto**（自动检测）
5. 选择/编辑测试场景
6. 点击 **「开始压测」**
7. 结果实时填入表格，可导出 CSV/JSON

### 命令行模式

```bash
# 基本压测
LLM_Bench.exe --headless --url http://localhost:8000/v1 --model gpt-4o -c 10 -n 100

# 流式压测（默认即流式）
LLM_Bench.exe --headless --url http://localhost:8000/v1 --model gpt-4o -c 20 -n 200 --max-tokens 200

# 仅获取模型列表
LLM_Bench.exe --fetch-models --url http://localhost:8000/v1 --key sk-xxx

# 指定协议
LLM_Bench.exe --headless --url http://localhost:8000/v1 --api chat -c 5 -n 50
```

### 参数说明

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--headless` | 无 GUI 命令行模式 | False |
| `--url` | API Base URL | `http://localhost:8000/v1` |
| `--key` | API Key | 空 |
| `--model` | 模型名称 | gpt-3.5-turbo |
| `--api` | 协议: auto/chat/completions/responses | auto |
| `-c` | 并发数 | 10 |
| `-n` | 总请求数 | 50 |
| `--max-tokens` | 最大输出 token | 100 |
| `--stream` | 流式模式（默认已开启） | True |
| `--input` | 输入文本 | 你好 |
| `--timeout` | 超时秒数 | 120 |
| `--fetch-models` | 仅获取模型列表 | False |

---

## 📊 输出示例

### 控制台表格

```
场景                 并发 总数 成功 失败 耗时s  req/s outT/s totT/s 延迟avg   P50     P90     P99    TTFT   ITL_avg ITL_P50 ITL_P90 tok/gen inT/req outT/req
------------------------------------------------------------------------------------------------------------------------------------------------------------
单场景测试              1   2   2   0  9.46  0.21   3.8    3.8   4.609  6.343  6.343  6.343  3.310  0.0764 0.0002 0.2056 0.0767    0.0    18.0
```

### 导出文件

文件名格式：`{模型名}_bench_{日期}.csv`

示例：`GLM-5.2-FP8_bench_20260821.csv`

**CSV 列（25 列）：**

```
场景, 并发数, 总请求数, 成功请求数, 失败请求数,
测试耗时(s), 请求吞吐(req/s), 输出token吞吐(tok/s), 总token吞吐(tok/s),
平均端到端延迟(s), P50延迟(s), P90延迟(s), P99延迟(s),
平均首token延迟TTFT(s), TTFT_P50(s), TTFT_P90(s),
平均每token生成时间(s), 平均token间延迟ITL(s), ITL_P50(s), ITL_P90(s), ITL_P99(s),
每请求平均输入token, 每请求平均输出token, 总输入token, 总输出token
```

---

## 🖥️ GUI 界面

```
┌─────────────────────────────────────────────────────────┐
│  连接配置                                                │
│  Base URL: [http://localhost:8000/v1]  API Key: [*****] │
│  模型: [gpt-4o ▼] [获取模型]  协议: [auto ▼]  Timeout:  │
├─────────────────────────────────────────────────────────┤
│  测试场景                              [+添加][编辑][删除] │
│  场景名称    并发  总数  输入文本    MaxTok  流式          │
│  低并发-短文本  1   10   你好        50      否           │
│  中并发-短文本  5   30   你好        50      否           │
│  高并发-短文本  20  100  你好        50      否           │
│  ...                                                     │
├─────────────────────────────────────────────────────────┤
│  [开始压测] [停止] [导出CSV] [导出JSON]    ▓▓▓▓░░ 3/6   │
├─────────────────────────────────────────────────────────┤
│  专业测试结果 (无N/A, 全指标输出)                         │
│  场景 | 并发 | 总数 | 成功 | 失败 | 耗时 | req/s | ...   │
│  低并发-短文本 | 1 | 10 | 10 | 0 | 12.3 | 0.81 | ...    │
│  中并发-短文本 | 5 | 30 | 30 | 0 | 18.5 | 1.62 | ...    │
├─────────────────────────────────────────────────────────┤
│  执行日志                                                │
│  压测开始 | 2026-08-21 16:28:45                          │
│  场景 1/6: 低并发-短文本                                 │
│    [TTFT] avg=1.614s p50=1.816s (精确)                  │
│    [生成] 每token=0.075s ITL avg=0.075s                 │
│  ...                                                     │
└─────────────────────────────────────────────────────────┘
```

---

## 🏗️ 从源码构建

### 环境要求

- Python 3.10+
- 依赖：`httpx`, `tkinter`（Python 自带）

### 构建 exe

```bash
# 安装依赖
pip install httpx pyinstaller

# 打包成独立 exe
pyinstaller --onefile --windowed --name "LLM_Bench" --collect-all httpx llm_bench.py

# 生成文件
dist/LLM_Bench.exe
```

### 从源码运行

```bash
# GUI 模式
python llm_bench.py

# 命令行模式
python llm_bench.py --headless --url http://localhost:8000/v1 -c 10 -n 100
```

---

## 📐 指标计算说明

| 指标 | 计算方式 |
|---|---|
| TTFT (首 token 延迟) | 从请求发出到第一个 token 返回的时间 |
| ITL (token 间延迟) | 相邻两个 token 之间的时间间隔 |
| 每 token 生成时间 | (端到端延迟 - TTFT) / (输出 token 数 - 1) |
| 请求吞吐 | 成功请求数 / 总耗时 |
| 输出 token 吞吐 | 总输出 token / 总耗时 |
| 总 token 吞吐 | (总输入 + 总输出) / 总耗时 |
| P50/P90/P99 | 对应百分位的延迟值 |

> **所有指标通过 SSE 流式精确测量**，不使用估算或均匀分布假设。

---

## 🔧 兼容性

### 已测试的 API 服务

| 服务 | 协议 | 状态 |
|---|---|---|
| OpenAI API | chat / completions / responses | ✅ |
| vLLM | chat | ✅ |
| Ollama | chat | ✅ |
| LM Studio | chat | ✅ |
| GLM (智谱) | chat (含 reasoning_content) | ✅ |
| DeepSeek | chat | ✅ |
| 任意 OpenAI 兼容 API | auto 自动检测 | ✅ |

### 运行环境

- **OS**: Windows 10/11 (x64)
- **无需安装**: 不依赖 Python、Node.js 或其他运行时
- **可移植**: 直接复制 exe 到其他 Windows 机器即可运行

---

## 📄 License

MIT

---

## 🙏 致谢

- [httpx](https://github.com/encode/httpx) — HTTP 客户端
- [PyInstaller](https://pyinstaller.org/) — Python 打包
- [tkinter](https://docs.python.org/3/library/tkinter.html) — GUI 框架
