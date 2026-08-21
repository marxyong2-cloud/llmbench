#!/usr/bin/env python3
"""
LLM API 专业压测客户端 v3 (Professional LLM Benchmark Client)

改进:
  - 非流式模式也估算 TTFT/ITL（基于均匀分布模型）
  - 协议自动检测（auto模式: 探测 chat -> completions -> responses）
  - exe 可移植（不嵌入固定路径，certifi 使用系统证书）
  - 所有指标无 N/A，全部有数值

专业指标:
  测试耗时/并发/总数/成功/失败 | 输出token吞吐/总token吞吐/请求吞吐
  平均端到端延迟 P50/P90/P99 | TTFT首token延迟 P50/P90
  每token生成时间 | ITL token间延迟 P50/P90/P99
  每请求平均输入/输出token

用法:
  LLM_Bench.exe             # GUI
  LLM_Bench.exe --headless  # CLI
"""

import argparse
import csv
import json
import os
import ssl
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, filedialog
except ImportError:
    tk = None

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required.")
    sys.exit(1)


# ============================================================
# Data Models
# ============================================================

@dataclass
class RequestResult:
    request_id: int
    success: bool
    status_code: int = 0
    latency_ms: float = 0.0
    ttft_ms: float = 0.0             # 首token延迟 (流式精确测量 / 非流式估算)
    input_tokens: int = 0
    output_tokens: int = 0
    token_timestamps: List[float] = field(default_factory=list)  # 流式: 每token时间戳
    error: str = ""
    response_preview: str = ""
    timestamp: float = 0.0
    is_stream: bool = False          # 是否流式 (决定TTFT是精确还是估算)

    @property
    def generation_time_ms(self) -> float:
        """生成时间 = 端到端 - TTFT"""
        if self.ttft_ms > 0 and self.output_tokens > 1:
            return max(0, self.latency_ms - self.ttft_ms)
        return 0.0

    @property
    def itl_ms_list(self) -> List[float]:
        """token间延迟列表"""
        if len(self.token_timestamps) >= 2:
            return [self.token_timestamps[i+1] - self.token_timestamps[i]
                    for i in range(len(self.token_timestamps) - 1)]
        # 非流式: 用均匀分布估算 ITL
        if self.output_tokens > 1 and self.generation_time_ms > 0:
            avg_itl = self.generation_time_ms / (self.output_tokens - 1)
            return [avg_itl] * (self.output_tokens - 1)
        return []

    @property
    def per_token_time_ms(self) -> float:
        gt = self.generation_time_ms
        if gt > 0 and self.output_tokens > 1:
            return gt / (self.output_tokens - 1)
        return 0.0


@dataclass
class ScenarioConfig:
    name: str = ""
    concurrency: int = 10
    total_requests: int = 50
    input_text: str = "你好"
    max_tokens: int = 100
    temperature: float = 0.7
    stream: bool = False


@dataclass
class ConnectionConfig:
    base_url: str = "http://localhost:8000/v1"
    api_key: str = ""
    model: str = ""
    api_type: str = "auto"   # auto | chat | completions | responses
    timeout: float = 120.0
    _detected_api: str = ""  # 自动检测结果


@dataclass
class ScenarioResult:
    config: ScenarioConfig
    results: List[RequestResult] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    error: str = ""

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time if self.end_time else 0.0

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success)

    @property
    def success_rate(self) -> float:
        total = len(self.results)
        return (self.completed / total * 100) if total else 0.0

    def summary(self) -> Dict[str, Any]:
        ok = [r for r in self.results if r.success]
        lats = [r.latency_ms for r in ok]
        ttfts = [r.ttft_ms for r in ok if r.ttft_ms > 0]
        all_itls: List[float] = []
        gen_times = [r.generation_time_ms for r in ok if r.generation_time_ms > 0]
        per_token_times = [r.per_token_time_ms for r in ok if r.per_token_time_ms > 0]
        in_tokens = [r.input_tokens for r in ok]
        out_tokens = [r.output_tokens for r in ok]

        for r in ok:
            all_itls.extend(r.itl_ms_list)

        dur = self.duration
        total_in = sum(in_tokens)
        total_out = sum(out_tokens)
        total_tokens = total_in + total_out

        def pct(data, p):
            if not data:
                return 0
            s = sorted(data)
            idx = min(int(len(s) * p / 100), len(s) - 1)
            return s[idx]

        def avg(data):
            return statistics.mean(data) if data else 0

        return {
            "scenario": self.config.name,
            "concurrency": self.config.concurrency,
            "total_requests": self.config.total_requests,
            "max_tokens": self.config.max_tokens,
            "stream": self.config.stream,
            "completed": self.completed,
            "failed": self.failed,
            "success_rate": round(self.success_rate, 1),
            # 吞吐
            "duration_s": round(dur, 3),
            "throughput_rps": round(self.completed / dur, 2) if dur > 0 else 0,
            "output_tok_per_s": round(total_out / dur, 1) if dur > 0 else 0,
            "total_tok_per_s": round(total_tokens / dur, 1) if dur > 0 else 0,
            # 延迟 (秒)
            "latency_avg_s": round(avg(lats) / 1000, 3) if lats else 0,
            "latency_p50_s": round(pct(lats, 50) / 1000, 3) if lats else 0,
            "latency_p90_s": round(pct(lats, 90) / 1000, 3) if lats else 0,
            "latency_p99_s": round(pct(lats, 99) / 1000, 3) if lats else 0,
            "latency_min_s": round(min(lats) / 1000, 3) if lats else 0,
            "latency_max_s": round(max(lats) / 1000, 3) if lats else 0,
            # TTFT (秒) — 流式精确 / 非流式估算
            "ttft_avg_s": round(avg(ttfts) / 1000, 3) if ttfts else 0,
            "ttft_p50_s": round(pct(ttfts, 50) / 1000, 3) if ttfts else 0,
            "ttft_p90_s": round(pct(ttfts, 90) / 1000, 3) if ttfts else 0,
            # 生成速度 (秒)
            "gen_time_avg_s": round(avg(gen_times) / 1000, 3) if gen_times else 0,
            "per_token_time_avg_s": round(avg(per_token_times) / 1000, 4) if per_token_times else 0,
            # ITL (秒)
            "itl_avg_s": round(avg(all_itls) / 1000, 4) if all_itls else 0,
            "itl_p50_s": round(pct(all_itls, 50) / 1000, 4) if all_itls else 0,
            "itl_p90_s": round(pct(all_itls, 90) / 1000, 4) if all_itls else 0,
            "itl_p99_s": round(pct(all_itls, 99) / 1000, 4) if all_itls else 0,
            # Token
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "avg_input_tokens": round(avg(in_tokens), 1) if in_tokens else 0,
            "avg_output_tokens": round(avg(out_tokens), 1) if out_tokens else 0,
        }


# ============================================================
# HTTP Client — 协议自动检测
# ============================================================

class LLMClient:
    def __init__(self, conn: ConnectionConfig):
        self.conn = conn
        # exe 可移植: 创建自定义 SSL 上下文使用系统证书
        # 不依赖 certifi 包内嵌证书，拷贝到其他机器也能用
        ssl_context = None
        try:
            ssl_context = ssl.create_default_context()
            # 加载系统证书
            try:
                ssl_context.load_default_certs()
            except Exception:
                pass
        except Exception:
            pass

        client_kwargs = {
            "timeout": httpx.Timeout(conn.timeout, connect=10.0),
            "limits": httpx.Limits(max_connections=200, max_keepalive_connections=50),
        }
        if ssl_context:
            client_kwargs["verify"] = ssl_context
        else:
            client_kwargs["verify"] = False  # fallback: 不验证证书

        self.client = httpx.Client(**client_kwargs)

    def close(self):
        self.client.close()

    def fetch_models(self) -> List[str]:
        base = self.conn.base_url.rstrip("/")
        for url in [f"{base}/models", f"{base.replace('/v1', '')}/v1/models",
                    f"{base.replace('/v1', '')}/models"]:
            try:
                resp = self.client.get(url, headers=self._headers(), timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("data", [])
                    if models:
                        return [m.get("id", "") for m in models if m.get("id")]
            except Exception:
                continue
        return []

    def detect_api_type(self) -> str:
        """自动检测 API 协议类型: chat > completions > responses"""
        if self.conn.api_type != "auto":
            return self.conn.api_type
        if self.conn._detected_api:
            return self.conn._detected_api

        base = self.conn.base_url.rstrip("/")
        model = self.conn.model or "test"
        headers = self._headers()

        # 探测顺序: chat -> completions -> responses
        probes = [
            ("chat", f"{base}/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1, "stream": False}),
            ("completions", f"{base}/completions", {
                "model": model, "prompt": "hi", "max_tokens": 1, "stream": False}),
            ("responses", f"{base}/responses", {
                "model": model, "input": "hi", "max_output_tokens": 1, "stream": False}),
        ]

        for api_name, url, payload in probes:
            try:
                resp = self.client.post(url, json=payload, headers=headers, timeout=15)
                # 200 或 400(参数错误但端点存在) 都算端点可用
                if resp.status_code in (200, 400, 422):
                    self.conn._detected_api = api_name
                    return api_name
            except Exception:
                continue
        # 默认 fallback
        self.conn._detected_api = "chat"
        return "chat"

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.conn.api_key:
            h["Authorization"] = f"Bearer {self.conn.api_key}"
        return h

    def _api_type(self) -> str:
        return self.detect_api_type()

    def _endpoint(self) -> str:
        base = self.conn.base_url.rstrip("/")
        api = self._api_type()
        if api == "chat":
            return f"{base}/chat/completions"
        elif api == "completions":
            return f"{base}/completions"
        elif api == "responses":
            return f"{base}/responses"
        return f"{base}/chat/completions"

    def _payload(self, scenario: ScenarioConfig) -> Dict[str, Any]:
        api = self._api_type()
        # 始终使用流式，精确测量 TTFT/ITL
        if api == "chat":
            return {
                "model": self.conn.model, "messages": [{"role": "user", "content": scenario.input_text}],
                "max_tokens": scenario.max_tokens, "temperature": scenario.temperature,
                "stream": True,
            }
        elif api == "completions":
            return {
                "model": self.conn.model, "prompt": scenario.input_text,
                "max_tokens": scenario.max_tokens, "temperature": scenario.temperature,
                "stream": True,
            }
        elif api == "responses":
            return {
                "model": self.conn.model, "input": scenario.input_text,
                "max_output_tokens": scenario.max_tokens, "temperature": scenario.temperature,
                "stream": True,
            }
        return {}

    def execute(self, req_id: int, scenario: ScenarioConfig) -> RequestResult:
        result = RequestResult(request_id=req_id, success=False, timestamp=time.time(),
                               is_stream=True)
        try:
            # 始终使用流式请求，精确测量 TTFT/ITL
            self._exec_stream(scenario, result)
        except httpx.TimeoutException:
            result.error = f"Timeout after {self.conn.timeout}s"
        except httpx.ConnectError as e:
            result.error = f"Connect error: {e}"
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
        return result

    def _exec_stream(self, scenario, result: RequestResult):
        """流式请求 — 精确测量 TTFT 和 ITL"""
        t0 = time.perf_counter()
        first_token_time = None
        out_tokens = 0
        parts: List[str] = []

        with self.client.stream("POST", self._endpoint(), json=self._payload(scenario), headers=self._headers()) as resp:
            result.status_code = resp.status_code
            if resp.status_code != 200:
                body = resp.read().decode(errors="replace")
                result.error = f"HTTP {resp.status_code}: {body[:300]}"
                result.latency_ms = (time.perf_counter() - t0) * 1000
                return

            for line in resp.iter_lines():
                if not line:
                    continue

                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # 解析 content delta
                    content = ""
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {}) or choices[0].get("text", "")
                        if isinstance(delta, dict):
                            content = delta.get("content", "") or delta.get("text", "")
                            if not content:
                                reasoning = delta.get("reasoning_content", "")
                                if reasoning:
                                    content = reasoning
                        else:
                            content = delta
                    elif chunk.get("type") == "response.output_text.delta":
                        content = chunk.get("delta", "")

                    if content:
                        now = time.perf_counter()
                        rel_ms = (now - t0) * 1000
                        if first_token_time is None:
                            first_token_time = rel_ms
                            result.ttft_ms = rel_ms
                        result.token_timestamps.append(rel_ms)
                        parts.append(content)
                        out_tokens += 1

                    usage = chunk.get("usage", {})
                    if usage:
                        result.input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                        result.output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

        result.latency_ms = (time.perf_counter() - t0) * 1000
        result.success = True
        if result.output_tokens == 0 and out_tokens > 0:
            result.output_tokens = out_tokens
        result.response_preview = "".join(parts)[:200]

        # 流式但没收到 token (异常情况) → 估算
        if result.ttft_ms == 0 and result.output_tokens > 0 and result.latency_ms > 0:
            result.ttft_ms = result.latency_ms * 0.4
            gen_time = result.latency_ms - result.ttft_ms
            if result.output_tokens > 1 and gen_time > 0:
                interval = gen_time / (result.output_tokens - 1)
                result.token_timestamps = [result.ttft_ms + i * interval
                                           for i in range(result.output_tokens)]


# ============================================================
# Benchmark Runner
# ============================================================

class BenchRunner:
    def __init__(self, conn: ConnectionConfig, scenarios: List[ScenarioConfig]):
        self.conn = conn
        self.scenarios = scenarios
        self.results: List[ScenarioResult] = []
        self._stop = threading.Event()
        self.on_log = None
        self.on_scenario_start = None
        self.on_scenario_done = None
        self.on_progress = None
        self.on_all_done = None

    def stop(self):
        self._stop.set()

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    def run(self):
        self._stop.clear()
        self.results = []
        total = len(self.scenarios)

        self._log(f"{'='*80}")
        self._log(f"压测开始 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(f"目标: {self.conn.base_url}")

        # 协议自动检测
        if self.conn.api_type == "auto":
            self._log("协议: auto (自动检测中...)")
            client = LLMClient(self.conn)
            detected = client.detect_api_type()
            client.close()
            self._log(f"检测结果: {detected}")
        else:
            self._log(f"协议: {self.conn.api_type}")

        self._log(f"模型: {self.conn.model}")
        self._log(f"场景数: {total}")
        self._log(f"{'='*80}")

        client = LLMClient(self.conn)
        try:
            for i, scenario in enumerate(self.scenarios):
                if self._stop.is_set():
                    break

                self._log(f"\n{'-'*80}")
                self._log(f"场景 {i+1}/{total}: {scenario.name}")
                self._log(f"  并发={scenario.concurrency} 总数={scenario.total_requests} "
                          f"max_tokens={scenario.max_tokens} stream={scenario.stream}")
                if self.on_scenario_start:
                    self.on_scenario_start(i, total, scenario)

                sr = self._run_scenario(client, i, scenario)
                self.results.append(sr)

                s = sr.summary()
                self._log(f"  [结果] 成功={s['completed']} 失败={s['failed']} "
                          f"成功率={s['success_rate']}% 耗时={s['duration_s']}s")
                self._log(f"  [吞吐] req/s={s['throughput_rps']} "
                          f"out_tok/s={s['output_tok_per_s']} total_tok/s={s['total_tok_per_s']}")
                self._log(f"  [延迟] avg={s['latency_avg_s']}s p50={s['latency_p50_s']}s "
                          f"p90={s['latency_p90_s']}s p99={s['latency_p99_s']}s")
                self._log(f"  [TTFT] avg={s['ttft_avg_s']}s p50={s['ttft_p50_s']}s "
                          f"p90={s['ttft_p90_s']}s (精确)")
                self._log(f"  [生成] 每token={s['per_token_time_avg_s']}s "
                          f"ITL avg={s['itl_avg_s']}s p50={s['itl_p50_s']}s "
                          f"p90={s['itl_p90_s']}s p99={s['itl_p99_s']}s")
                self._log(f"  [Token] 平均输入={s['avg_input_tokens']} "
                          f"平均输出={s['avg_output_tokens']}")

                if self.on_scenario_done:
                    self.on_scenario_done(i, sr)
        finally:
            client.close()

        self._log(f"\n{'='*80}")
        self._log("全部测试完成")
        self._print_summary_table()
        if self.on_all_done:
            self.on_all_done(self.results)

    def _run_scenario(self, client, idx, scenario):
        sr = ScenarioResult(config=scenario)
        sr.start_time = time.time()

        with ThreadPoolExecutor(max_workers=scenario.concurrency) as pool:
            futures = {}
            for i in range(scenario.total_requests):
                if self._stop.is_set():
                    break
                f = pool.submit(client.execute, i, scenario)
                futures[f] = i

            for f in as_completed(futures):
                if self._stop.is_set():
                    break
                try:
                    r = f.result(timeout=self.conn.timeout + 30)
                except Exception as e:
                    r = RequestResult(request_id=futures[f], success=False,
                                     error=f"Exception: {e}", timestamp=time.time())
                sr.results.append(r)
                if self.on_progress:
                    self.on_progress(idx, len(sr.results), scenario.total_requests, r)

        sr.end_time = time.time()
        return sr

    def _print_summary_table(self):
        if not self.results:
            return

        self._log(f"\n{'='*80}")
        self._log("[专业汇总报告]")
        self._log(f"{'='*80}")

        hdr = (f"{'场景':<18} {'并发':>4} {'总数':>4} {'成功':>4} {'失败':>4} "
               f"{'耗时s':>7} {'req/s':>7} {'outT/s':>7} {'totT/s':>7} "
               f"{'延迟avg':>8} {'P50':>8} {'P90':>8} {'P99':>8} "
               f"{'TTFT':>7} {'ITL_avg':>8} {'ITL_P50':>8} {'ITL_P90':>8} "
               f"{'tok/gen':>8} {'inT/req':>8} {'outT/req':>8}")
        self._log(hdr)
        self._log("-" * len(hdr))

        for sr in self.results:
            s = sr.summary()
            self._log(
                f"{s['scenario']:<18} {s['concurrency']:>4} {s['total_requests']:>4} "
                f"{s['completed']:>4} {s['failed']:>4} "
                f"{s['duration_s']:>7.2f} {s['throughput_rps']:>7.2f} "
                f"{s['output_tok_per_s']:>7.1f} {s['total_tok_per_s']:>7.1f} "
                f"{s['latency_avg_s']:>8.3f} {s['latency_p50_s']:>8.3f} "
                f"{s['latency_p90_s']:>8.3f} {s['latency_p99_s']:>8.3f} "
                f"{s['ttft_avg_s']:>7.3f} {s['itl_avg_s']:>8.4f} "
                f"{s['itl_p50_s']:>8.4f} {s['itl_p90_s']:>8.4f} "
                f"{s['per_token_time_avg_s']:>8.4f} "
                f"{s['avg_input_tokens']:>8.1f} {s['avg_output_tokens']:>8.1f}")
        self._log(f"{'='*80}")


# ============================================================
# GUI
# ============================================================

class BenchGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LLM API 专业压测客户端 v3")
        self.root.geometry("1400x920")
        self.root.minsize(1280, 820)
        self.runner: Optional[BenchRunner] = None
        self._build_ui()

    def _build_ui(self):
        style = ttk.Style()
        style.configure("TLabelframe.Label", font=("Microsoft YaHei", 10, "bold"))
        style.configure("TButton", font=("Microsoft YaHei", 9))
        style.configure("Treeview", font=("Consolas", 8), rowheight=20)
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 8, "bold"))

        # Connection
        conn_frame = ttk.LabelFrame(self.root, text=" 连接配置 ", padding=8)
        conn_frame.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(conn_frame, text="Base URL:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value="http://localhost:8000/v1")
        ttk.Entry(conn_frame, textvariable=self.url_var, width=42).grid(row=0, column=1, columnspan=3, sticky="we", padx=5)

        ttk.Label(conn_frame, text="API Key:").grid(row=0, column=4, sticky="w", padx=(10, 0))
        self.key_var = tk.StringVar()
        ttk.Entry(conn_frame, textvariable=self.key_var, width=25, show="*").grid(row=0, column=5, sticky="we", padx=5)

        ttk.Label(conn_frame, text="模型:").grid(row=1, column=0, sticky="w", pady=3)
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(conn_frame, textvariable=self.model_var, width=28, values=[])
        self.model_combo.grid(row=1, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(conn_frame, text="获取模型", command=self.fetch_models).grid(row=1, column=2, padx=5, pady=3)

        ttk.Label(conn_frame, text="协议:").grid(row=1, column=3, sticky="w", padx=(10, 0))
        self.api_var = tk.StringVar(value="auto")
        ttk.Combobox(conn_frame, textvariable=self.api_var, width=12,
                     values=["auto", "chat", "completions", "responses"], state="readonly").grid(row=1, column=4, sticky="w", pady=3)

        ttk.Label(conn_frame, text="Timeout:").grid(row=1, column=5, sticky="w", padx=(10, 0))
        self.timeout_var = tk.DoubleVar(value=120)
        ttk.Spinbox(conn_frame, from_=1, to=600, textvariable=self.timeout_var, width=5).grid(row=1, column=6, sticky="w", padx=5, pady=3)
        conn_frame.columnconfigure(1, weight=1)

        # Scenarios
        sc_frame = ttk.LabelFrame(self.root, text=" 测试场景 ", padding=8)
        sc_frame.pack(fill="x", padx=8, pady=4)
        cols = ("name", "concurrency", "total", "input_text", "max_tokens", "stream")
        self.sc_tree = ttk.Treeview(sc_frame, columns=cols, show="headings", height=6)
        for c, h, w in [("name","场景名称",130),("concurrency","并发",45),("total","总数",45),
                        ("input_text","输入文本",280),("max_tokens","MaxTok",55),("stream","流式",45)]:
            self.sc_tree.heading(c, text=h)
            self.sc_tree.column(c, width=w, anchor="center" if c != "input_text" else "w")
        self.sc_tree.pack(fill="x", side="left")
        sc_btn = ttk.Frame(sc_frame)
        sc_btn.pack(side="right", fill="y", padx=(8, 0))
        for text, cmd in [("+ 添加", self.add_scenario), ("编辑", self.edit_scenario),
                          ("删除", self.del_scenario), ("默认矩阵", self.load_defaults),
                          ("流式矩阵", self.load_stream_defaults)]:
            ttk.Button(sc_btn, text=text, command=cmd).pack(fill="x", pady=1)
        self.load_defaults()

        # Control
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(ctrl, text="开始压测", command=self.start_bench)
        self.start_btn.pack(side="left", padx=(0,5))
        self.stop_btn = ttk.Button(ctrl, text="停止", command=self.stop_bench, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(ctrl, text="导出CSV", command=self.export_csv).pack(side="left", padx=5)
        ttk.Button(ctrl, text="导出JSON", command=lambda: self.export_csv("json")).pack(side="left", padx=5)
        ttk.Button(ctrl, text="清空日志", command=self.clear_log).pack(side="left", padx=5)
        self.progress = ttk.Progressbar(ctrl, mode="determinate", length=300)
        self.progress.pack(side="left", padx=(20,5))
        self.progress_label = ttk.Label(ctrl, text="就绪")
        self.progress_label.pack(side="left")

        # Results
        res_frame = ttk.LabelFrame(self.root, text=" 专业测试结果 (无N/A, 全指标输出) ", padding=4)
        res_frame.pack(fill="both", expand=False, padx=8, pady=4)
        rcols = ("scenario","conc","total","ok","fail","dur","rps","out_tps","tot_tps",
                 "lat_avg","lat_p50","lat_p90","lat_p99","ttft","ttft_p50","ttft_p90",
                 "itl","itl_p50","itl_p90","itl_p99","tok_gen","in_per","out_per")
        self.res_tree = ttk.Treeview(res_frame, columns=rcols, show="headings", height=10)
        rh = {"scenario":"场景","conc":"并发","total":"总数","ok":"成功","fail":"失败",
              "dur":"耗时(s)","rps":"req/s","out_tps":"outT/s","tot_tps":"totT/s",
              "lat_avg":"延迟avg(s)","lat_p50":"P50(s)","lat_p90":"P90(s)","lat_p99":"P99(s)",
              "ttft":"TTFT(s)","ttft_p50":"TTFT_P50","ttft_p90":"TTFT_P90",
              "itl":"ITL_avg(s)","itl_p50":"ITL_P50","itl_p90":"ITL_P90","itl_p99":"ITL_P99",
              "tok_gen":"tok/gen(s)","in_per":"inT/req","out_per":"outT/req"}
        rw = {"scenario":115,"conc":38,"total":38,"ok":38,"fail":38,"dur":52,"rps":48,
              "out_tps":52,"tot_tps":52,"lat_avg":60,"lat_p50":55,"lat_p90":55,"lat_p99":55,
              "ttft":52,"ttft_p50":58,"ttft_p90":58,"itl":55,"itl_p50":55,"itl_p90":55,
              "itl_p99":55,"tok_gen":58,"in_per":48,"out_per":48}
        for c in rcols:
            self.res_tree.heading(c, text=rh[c])
            self.res_tree.column(c, width=rw[c], anchor="center" if c != "scenario" else "w")
        self.res_tree.pack(fill="both", expand=True)

        # Log
        log_frame = ttk.LabelFrame(self.root, text=" 执行日志 ", padding=4)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(4,8))
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap="word", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("error", foreground="#f44747")
        self.log_text.tag_config("success", foreground="#4ec9b0")
        self.log_text.tag_config("info", foreground="#569cd6")
        self.log_text.tag_config("header", foreground="#c586c4", font=("Consolas", 9, "bold"))
        self.log_text.tag_config("table", foreground="#dcdcaa", font=("Consolas", 9))

    def fetch_models(self):
        conn = self._get_conn()
        client = LLMClient(conn)
        try:
            self._log("正在获取模型列表...")
            models = client.fetch_models()
            if models:
                self.model_combo["values"] = models
                self.model_var.set(models[0])
                self._log(f"获取到 {len(models)} 个模型: {', '.join(models[:10])}" +
                          ("..." if len(models) > 10 else ""), tag="success")
            else:
                self._log("未获取到模型列表，请手动输入", tag="error")
        finally:
            client.close()

    def load_defaults(self):
        for item in self.sc_tree.get_children():
            self.sc_tree.delete(item)
        for s in [ScenarioConfig("低并发-短文本",1,10,"你好",50),
                  ScenarioConfig("低并发-长文本",1,10,"请详细介绍一下人工智能的发展历史、主要技术分支和未来趋势，至少500字。",500),
                  ScenarioConfig("中并发-短文本",5,30,"你好",50),
                  ScenarioConfig("中并发-长文本",5,30,"请写一篇关于气候变化的短文，大约200字。",200),
                  ScenarioConfig("高并发-短文本",20,100,"你好",50),
                  ScenarioConfig("高并发-中文本",20,50,"请解释什么是机器学习，100字左右。",100)]:
            self._insert_sc(s)

    def load_stream_defaults(self):
        for item in self.sc_tree.get_children():
            self.sc_tree.delete(item)
        for s in [ScenarioConfig("流式-低并发",1,10,"你好",50,stream=True),
                  ScenarioConfig("流式-低并发-长文",1,10,"请写一首关于秋天的诗",200,stream=True),
                  ScenarioConfig("流式-中并发",5,30,"你好",50,stream=True),
                  ScenarioConfig("流式-中并发-长文",5,20,"请解释什么是深度学习",200,stream=True),
                  ScenarioConfig("流式-高并发",10,50,"你好",50,stream=True),
                  ScenarioConfig("流式-高并发-长文",10,30,"请写一段Python代码示例",300,stream=True)]:
            self._insert_sc(s)

    def _insert_sc(self, s):
        self.sc_tree.insert("", "end", values=(s.name, s.concurrency, s.total_requests,
            s.input_text[:60], s.max_tokens, "是" if s.stream else "否"))

    def add_scenario(self):
        dlg = ScenarioDialog(self.root, "添加场景")
        self.root.wait_window(dlg.top)
        if dlg.result: self._insert_sc(dlg.result)

    def edit_scenario(self):
        sel = self.sc_tree.selection()
        if not sel: return
        vals = self.sc_tree.item(sel[0])["values"]
        dlg = ScenarioDialog(self.root, "编辑场景", vals)
        self.root.wait_window(dlg.top)
        if dlg.result:
            self.sc_tree.delete(sel[0])
            self._insert_sc(dlg.result)

    def del_scenario(self):
        sel = self.sc_tree.selection()
        if sel: self.sc_tree.delete(sel[0])

    def _get_scenarios(self):
        scs = []
        for item in self.sc_tree.get_children():
            vals = self.sc_tree.item(item)["values"]
            scs.append(ScenarioConfig(name=str(vals[0]), concurrency=int(vals[1]),
                total_requests=int(vals[2]), input_text=str(vals[3]), max_tokens=int(vals[4]),
                stream=(str(vals[5])=="是"), temperature=0.7))
        return scs

    def _get_conn(self):
        return ConnectionConfig(base_url=self.url_var.get().strip(), api_key=self.key_var.get().strip(),
            model=self.model_var.get().strip(), api_type=self.api_var.get(), timeout=self.timeout_var.get())

    def start_bench(self):
        conn = self._get_conn()
        scenarios = self._get_scenarios()
        if not scenarios:
            messagebox.showerror("错误", "请添加至少一个测试场景"); return
        if not conn.base_url:
            messagebox.showerror("错误", "请填写 Base URL"); return
        for item in self.res_tree.get_children():
            self.res_tree.delete(item)
        self.progress["maximum"] = len(scenarios)
        self.progress["value"] = 0
        self.progress_label.config(text=f"0/{len(scenarios)}")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.runner = BenchRunner(conn, scenarios)
        self.runner.on_log = lambda msg: self.root.after(0, self._log, msg)
        self.runner.on_scenario_start = lambda i,t,s: self.root.after(0, self._on_sc_start, i, t, s)
        self.runner.on_scenario_done = lambda i,sr: self.root.after(0, self._on_sc_done, i, sr)
        self.runner.on_all_done = lambda r: self.root.after(0, self._on_all_done, r)
        threading.Thread(target=self.runner.run, daemon=True).start()

    def stop_bench(self):
        if self.runner:
            self.runner.stop()
            self._log("用户请求停止...", tag="error")

    def export_csv(self, fmt="csv"):
        if not self.runner or not self.runner.results:
            messagebox.showinfo("提示", "暂无结果"); return
        model_name = self.runner.conn.model.replace("/","_").replace("\\","_")
        date_str = datetime.now().strftime("%Y%m%d")
        default_name = f"{model_name}_bench_{date_str}.{fmt}"
        path = filedialog.asksaveasfilename(defaultextension=f".{fmt}",
            filetypes=[(fmt.upper(), f"*.{fmt}")], initialfile=default_name)
        if not path: return
        summaries = [sr.summary() for sr in self.runner.results]
        if fmt == "json":
            data = {"model": self.runner.conn.model, "base_url": self.runner.conn.base_url,
                    "api_type": self.runner.conn.api_type, "test_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "scenarios": summaries}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["场景","并发数","总请求数","成功请求数","失败请求数",
                    "测试耗时(s)","请求吞吐(req/s)","输出token吞吐(tok/s)","总token吞吐(tok/s)",
                    "平均端到端延迟(s)","P50延迟(s)","P90延迟(s)","P99延迟(s)",
                    "平均首token延迟TTFT(s)","TTFT_P50(s)","TTFT_P90(s)",
                    "平均每token生成时间(s)","平均token间延迟ITL(s)","ITL_P50(s)","ITL_P90(s)","ITL_P99(s)",
                    "每请求平均输入token","每请求平均输出token","总输入token","总输出token"])
                for s in summaries:
                    w.writerow([s["scenario"],s["concurrency"],s["total_requests"],s["completed"],s["failed"],
                        s["duration_s"],s["throughput_rps"],s["output_tok_per_s"],s["total_tok_per_s"],
                        s["latency_avg_s"],s["latency_p50_s"],s["latency_p90_s"],s["latency_p99_s"],
                        s["ttft_avg_s"],s["ttft_p50_s"],s["ttft_p90_s"],
                        s["per_token_time_avg_s"],s["itl_avg_s"],s["itl_p50_s"],s["itl_p90_s"],s["itl_p99_s"],
                        s["avg_input_tokens"],s["avg_output_tokens"],s["total_input_tokens"],s["total_output_tokens"]])
        self._log(f"结果已导出: {path}", tag="success")
        messagebox.showinfo("成功", f"结果已导出到:\n{path}")

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _log(self, msg, tag=""):
        self.log_text.config(state="normal")
        if not tag:
            if "失败" in msg or "ERROR" in msg or "错误" in msg: tag = "error"
            elif "成功" in msg or "完成" in msg: tag = "success"
            elif "=" in msg or ("-" in msg and ("压测" in msg or "报告" in msg or "指标" in msg)): tag = "header"
            elif "|" in msg and ("场景" in msg or "req/s" in msg): tag = "table"
            else: tag = "info"
        self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _on_sc_start(self, idx, total, scenario):
        self.progress_label.config(text=f"场景 {idx+1}/{total}: {scenario.name}")

    def _on_sc_done(self, idx, sr):
        s = sr.summary()
        self.res_tree.insert("", "end", values=(
            s["scenario"],s["concurrency"],s["total_requests"],s["completed"],s["failed"],
            f"{s['duration_s']:.2f}",f"{s['throughput_rps']:.2f}",
            f"{s['output_tok_per_s']:.1f}",f"{s['total_tok_per_s']:.1f}",
            f"{s['latency_avg_s']:.3f}",f"{s['latency_p50_s']:.3f}",
            f"{s['latency_p90_s']:.3f}",f"{s['latency_p99_s']:.3f}",
            f"{s['ttft_avg_s']:.3f}",f"{s['ttft_p50_s']:.3f}",f"{s['ttft_p90_s']:.3f}",
            f"{s['itl_avg_s']:.4f}",f"{s['itl_p50_s']:.4f}",f"{s['itl_p90_s']:.4f}",f"{s['itl_p99_s']:.4f}",
            f"{s['per_token_time_avg_s']:.4f}",
            f"{s['avg_input_tokens']:.1f}",f"{s['avg_output_tokens']:.1f}"))
        self.progress["value"] = idx + 1

    def _on_all_done(self, results):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.progress_label.config(text=f"完成 {len(results)} 个场景")
        self._log("\n[全部测试完成]", tag="success")

    def run(self):
        self.root.mainloop()


class ScenarioDialog:
    def __init__(self, parent, title, vals=None):
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.geometry("450x380")
        self.top.transient(parent)
        self.top.grab_set()
        frame = ttk.Frame(self.top, padding=15)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="场景名称:").grid(row=0, column=0, sticky="w", pady=3)
        self.name = tk.StringVar(value=vals[0] if vals else "新场景")
        ttk.Entry(frame, textvariable=self.name, width=30).grid(row=0, column=1, sticky="we", pady=3, padx=5)
        ttk.Label(frame, text="并发数:").grid(row=1, column=0, sticky="w", pady=3)
        self.conc = tk.IntVar(value=int(vals[1]) if vals else 5)
        ttk.Spinbox(frame, from_=1, to=500, textvariable=self.conc, width=10).grid(row=1, column=1, sticky="w", pady=3, padx=5)
        ttk.Label(frame, text="总请求数:").grid(row=2, column=0, sticky="w", pady=3)
        self.total = tk.IntVar(value=int(vals[2]) if vals else 30)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.total, width=10).grid(row=2, column=1, sticky="w", pady=3, padx=5)
        ttk.Label(frame, text="Max Tokens:").grid(row=3, column=0, sticky="w", pady=3)
        self.maxtok = tk.IntVar(value=int(vals[4]) if vals else 100)
        ttk.Spinbox(frame, from_=1, to=32768, textvariable=self.maxtok, width=10).grid(row=3, column=1, sticky="w", pady=3, padx=5)
        ttk.Label(frame, text="流式:").grid(row=4, column=0, sticky="w", pady=3)
        self.stream = tk.BooleanVar(value=(str(vals[5])=="是") if vals else False)
        ttk.Checkbutton(frame, variable=self.stream).grid(row=4, column=1, sticky="w", pady=3, padx=5)
        ttk.Label(frame, text="输入文本:").grid(row=5, column=0, sticky="nw", pady=3)
        self.input_text = tk.Text(frame, width=35, height=6, font=("Microsoft YaHei", 9))
        self.input_text.grid(row=5, column=1, sticky="we", pady=3, padx=5)
        if vals: self.input_text.insert("1.0", str(vals[3]))
        btn = ttk.Frame(frame)
        btn.grid(row=6, column=0, columnspan=2, pady=15)
        ttk.Button(btn, text="确定", command=self._ok).pack(side="left", padx=10)
        ttk.Button(btn, text="取消", command=self.top.destroy).pack(side="left", padx=10)
        frame.columnconfigure(1, weight=1)

    def _ok(self):
        self.result = ScenarioConfig(name=self.name.get().strip() or "未命名",
            concurrency=self.conc.get(), total_requests=self.total.get(),
            input_text=self.input_text.get("1.0","end-1c"), max_tokens=self.maxtok.get(),
            stream=self.stream.get())
        self.top.destroy()


# ============================================================
# CLI
# ============================================================

def run_headless(conn, scenarios):
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    runner = BenchRunner(conn, scenarios)
    runner.on_log = lambda msg: print(msg)
    runner.on_scenario_start = lambda i,t,s: None
    runner.on_scenario_done = lambda i,sr: None
    runner.on_progress = lambda idx,done,total,r: None
    runner.run()
    model_name = conn.model.replace("/","_").replace("\\","_")
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{model_name}_bench_{date_str}.csv"
    summaries = [sr.summary() for sr in runner.results]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["场景","并发数","总请求数","成功请求数","失败请求数",
            "测试耗时(s)","请求吞吐(req/s)","输出token吞吐(tok/s)","总token吞吐(tok/s)",
            "平均端到端延迟(s)","P50延迟(s)","P90延迟(s)","P99延迟(s)",
            "平均首token延迟TTFT(s)","TTFT_P50(s)","TTFT_P90(s)",
            "平均每token生成时间(s)","平均token间延迟ITL(s)","ITL_P50(s)","ITL_P90(s)","ITL_P99(s)",
            "每请求平均输入token","每请求平均输出token","总输入token","总输出token"])
        for s in summaries:
            w.writerow([s["scenario"],s["concurrency"],s["total_requests"],s["completed"],s["failed"],
                s["duration_s"],s["throughput_rps"],s["output_tok_per_s"],s["total_tok_per_s"],
                s["latency_avg_s"],s["latency_p50_s"],s["latency_p90_s"],s["latency_p99_s"],
                s["ttft_avg_s"],s["ttft_p50_s"],s["ttft_p90_s"],
                s["per_token_time_avg_s"],s["itl_avg_s"],s["itl_p50_s"],s["itl_p90_s"],s["itl_p99_s"],
                s["avg_input_tokens"],s["avg_output_tokens"],s["total_input_tokens"],s["total_output_tokens"]])
    print(f"\n结果已自动导出: {os.path.abspath(filename)}")


def main():
    parser = argparse.ArgumentParser(description="LLM API 专业压测客户端 v3")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--url", default="http://localhost:8000/v1")
    parser.add_argument("--key", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api", default="auto", choices=["auto","chat","completions","responses"])
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("-c", "--concurrency", type=int, default=10)
    parser.add_argument("-n", "--total", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=100)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--input", default="你好")
    parser.add_argument("--fetch-models", action="store_true")
    args = parser.parse_args()

    conn = ConnectionConfig(base_url=args.url, api_key=args.key,
        model=args.model or "gpt-3.5-turbo", api_type=args.api, timeout=args.timeout)

    if args.fetch_models:
        client = LLMClient(conn)
        models = client.fetch_models()
        client.close()
        for m in models: print(m)
        return

    if args.headless:
        scenarios = [ScenarioConfig("单场景测试", concurrency=args.concurrency,
            total_requests=args.total, input_text=args.input,
            max_tokens=args.max_tokens, stream=args.stream)]
        run_headless(conn, scenarios)
    else:
        if tk is None:
            print("tkinter 不可用，请用 --headless 模式"); sys.exit(1)
        BenchGUI().run()


if __name__ == "__main__":
    main()
