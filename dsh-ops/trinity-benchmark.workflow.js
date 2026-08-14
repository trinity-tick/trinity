// ═══════════════════════════════════════════════════════════════════════
// trinity-benchmark.workflow.js — DSH workflow 示例（可粘贴到 workflow 工具）
//
// 说明：这是给 DSH workflow 工具（workflow）使用的脚本体示例，不是独立可运行
// 文件。在 DSH 会话中调用 workflow 工具时把本文件内容（去掉注释头即可）作为
// script 参数提交，meta 参数按下方示例填写。它演示了 P1 建议的落地模式：
//   - 用 parallel() 并行扇出多个 trinity 基准套件（替代 benchmark/ 里串行的
//     子进程链，见 benchmark/run_benchmark.py 的 subprocess 调用）；
//   - 每个套件一个独立子代理（替代 A2A TaskManager 只登记不执行的"空转"，
//     见 trinity/a2a/task_manager.py）；
//   - 结构化收集结果并汇总（替代自由文本 result dict）。
//
// meta 参数（JSON）:
//   {
//     "name": "trinity-benchmark",
//     "description": "并行运行 trinity 基准套件并汇总对比",
//     "phases": [
//       {"title": "bench", "detail": "并行跑各基准套件"},
//       {"title": "synthesize", "detail": "汇总结果生成对比报告"}
//     ]
//   }
// ═══════════════════════════════════════════════════════════════════════

const SUITES = args.suites ?? ["longmemeval", "locomo", "squad", "latency"];
const TRINITY = "C:\\Users\\Administrator\\trinity";
// 使用系统 Python（trinity 完整安装；项目 .venv 缺 fastapi/psycopg2 等依赖）
const PY = "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe";
const OUT = args.outDir ?? "C:\\Users\\Administrator\\.trinity\\bench-results";

phase("bench");

// 每个套件一个独立子代理，并行执行；任一失败只把该项置 null，不阻塞其余套件
const runs = await parallel(
  SUITES.map((suite) => async () => {
    const script = {
      longmemeval: "benchmark\\run_benchmark.py",
      locomo: "benchmark\\locomo_real_eval_v2.py",
      squad: "benchmark\\squad_hybrid_runner.py",
      latency: "benchmark\\run_latency_bench.py",
      concurrency: "benchmark\\concurrency_bench.py",
    }[suite];

    if (!script) {
      log(`unknown suite: ${suite}`);
      return { suite, status: "SKIP", reason: "unknown suite" };
    }

    // 子代理只负责"跑一个套件 + 汇报结构化结果"；LLM key 从环境注入，不落盘。
    // 用 schema 强制结构化输出，避免自由文本解析失败（首次实测中 JSON.parse 抛错导致结果丢失）。
    const result = await agent(
      `你是 trinity 基准执行代理。请运行：\n` +
        `  cd ${TRINITY}\n` +
        `  ${PY} ${script} --output-dir ${OUT}${args.apiKey ? ` --api-key ${args.apiKey}` : ""}\n` +
        `等待命令完成（可能较久）。然后读取 ${OUT} 下该套件生成的报告/日志，如实汇报。`,
      {
        label: `bench-${suite}`,
        phase: "bench",
        schema: {
          type: "object",
          properties: {
            suite: { type: "string", const: suite },
            status: { type: "string", enum: ["PASS", "FAIL"] },
            metrics: { type: "object", description: "关键指标" },
            artifacts: { type: "array", items: { type: "string" }, description: "产物文件" },
          },
          required: ["suite", "status"],
          additionalProperties: false,
        },
      }
    );

    if (!result) {
      return { suite, status: "FAIL", reason: "agent failed or timed out" };
    }
    // schema 模式下 result 已是对象；字符串场景做一次容错解析
    if (typeof result === "string") {
      try {
        return JSON.parse(result);
      } catch {
        return { suite, status: "FAIL", reason: "unparseable agent output" };
      }
    }
    return result;
  })
);

phase("synthesize");

// 汇总阶段：把各套件结果交给一个 agent 生成对比报告（结构化 schema 校验输出）
const report = await agent(
  `汇总以下 trinity 基准结果并生成 markdown 对比报告（含与 README 声称指标的对齐情况）：\n` +
    JSON.stringify(runs.filter(Boolean), null, 2),
  {
    label: "bench-synthesize",
    phase: "synthesize",
    schema: {
      type: "object",
      properties: {
        summary: { type: "string", description: "总体结论" },
        perSuite: {
          type: "object",
          description: "每个套件的状态与关键指标",
        },
        regressions: { type: "array", items: { type: "string" }, description: "明显退化项" },
      },
      required: ["summary", "perSuite"],
      additionalProperties: false,
    },
  }
);

log(`benchmark summary written; suites executed: ${runs.length}`);
return {
  outDir: OUT,
  suites: runs.filter(Boolean),
  report: report ?? "synthesis agent failed",
};
