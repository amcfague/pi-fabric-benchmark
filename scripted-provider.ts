import { readFileSync } from "node:fs";
import { isAbsolute } from "node:path";

export default async function (pi: any) {
  const specPath = process.env.PI_BENCHMARK_SCRIPTED_SPEC;
  const aiEntry = process.env.PI_BENCHMARK_AI_ENTRY;
  if (!specPath || !aiEntry || !isAbsolute(specPath) || !isAbsolute(aiEntry)) {
    throw new Error("Scripted benchmark paths must be absolute");
  }
  const { createAssistantMessageEventStream } = await import(aiEntry);
  const spec = JSON.parse(readFileSync(specPath, "utf8"));
  const allowed: Record<string, string[]> = {
    subagents: ["subagents_enable", "subagent"],
    "tintin-subagents": ["Agent", "get_subagent_result"],
    fabric: ["fabric_exec"],
  };
  if (spec.version !== 1 || !allowed[spec.arm] || !Array.isArray(spec.stages) || !spec.stages.length) {
    throw new Error("Invalid scripted benchmark spec");
  }
  const ids = new Set<string>();
  for (const stage of spec.stages) {
    const entries = stage.calls ?? stage.collect;
    if (!Array.isArray(entries) || !entries.length || Boolean(stage.calls) === Boolean(stage.collect)) {
      throw new Error("Invalid scripted benchmark stage");
    }
    for (const entry of entries) {
      const name = stage.collect ? "get_subagent_result" : entry.name;
      if (typeof entry.id !== "string" || !entry.id.startsWith("bench-") || ids.has(entry.id)
          || !allowed[spec.arm].includes(name)) {
        throw new Error("Invalid scripted benchmark call");
      }
      ids.add(entry.id);
    }
  }

  pi.registerProvider("benchmark-driver", {
    api: "benchmark-driver-api", baseUrl: "http://localhost", apiKey: "unused",
    models: ["scripted", "probe"].map((id) => ({
      id, name: id === "probe" ? "Offline benchmark probe" : "Scripted benchmark",
      reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 8192, maxTokens: 256,
    })),
    streamSimple(model: any, context: any, options: any) {
      const stream = createAssistantMessageEventStream();
      const output: any = {
        role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: "pending", timestamp: Date.now(),
      };
      queueMicrotask(() => {
        try {
          if (options?.signal?.aborted) throw new Error("Scripted benchmark aborted");
          stream.push({ type: "start", partial: output });
          if (model.id === "probe") {
            const text = "benchmark probe complete";
            output.usage.input = 1;
            output.usage.output = 1;
            output.usage.totalTokens = 2;
            output.content.push({ type: "text", text });
            stream.push({ type: "text_start", contentIndex: 0, partial: output });
            stream.push({ type: "text_end", contentIndex: 0, content: text, partial: output });
            output.stopReason = "stop";
            stream.push({ type: "done", reason: "stop", message: output });
            stream.end();
            return;
          }
          const results = context.messages.filter((message: any) =>
            message.role === "toolResult" && typeof message.toolCallId === "string"
            && message.toolCallId.startsWith("bench-"));
          const byId = new Map<string, any>();
          for (const result of results) {
            if (byId.has(result.toolCallId)) throw new Error("Duplicate scripted result");
            byId.set(result.toolCallId, result);
          }
          let completed = 0;
          let calls: Array<{ id: string; name: string; arguments: Record<string, unknown> }> = [];
          for (const stage of spec.stages) {
            const entries = stage.calls ?? stage.collect;
            if (results.length === completed) {
              calls = stage.calls ?? stage.collect.map((entry: any) => {
                const launch = byId.get(entry.launchId);
                const agentId = launch?.details?.agentId;
                if (launch?.toolName !== "Agent" || launch.details?.status !== "background"
                    || typeof agentId !== "string" || !agentId) {
                  throw new Error("Missing background Agent ID");
                }
                return { id: entry.id, name: "get_subagent_result",
                  arguments: { agent_id: agentId, wait: true, verbose: false } };
              });
              break;
            }
            for (const entry of entries) {
              const result = byId.get(entry.id);
              if (!result || result.isError || result.toolName !== (stage.collect ? "get_subagent_result" : entry.name)) {
                throw new Error("Scripted child call failed or is missing");
              }
            }
            completed += entries.length;
          }
          if (results.length !== completed && !calls.length) throw new Error("Unexpected scripted results");
          if (calls.length) {
            for (const call of calls) {
              const toolCall = { type: "toolCall", ...call };
              output.content.push(toolCall);
              const contentIndex = output.content.length - 1;
              stream.push({ type: "toolcall_start", contentIndex, id: call.id, toolName: call.name, partial: output });
              stream.push({ type: "toolcall_end", contentIndex, toolCall, partial: output });
            }
            output.stopReason = "toolUse";
          } else {
            const text = "Scripted dispatch complete";
            output.content.push({ type: "text", text });
            stream.push({ type: "text_start", contentIndex: 0, partial: output });
            stream.push({ type: "text_end", contentIndex: 0, content: text, partial: output });
            output.stopReason = "stop";
          }
          stream.push({ type: "done", reason: output.stopReason, message: output });
          stream.end();
        } catch (error) {
          output.stopReason = "error";
          output.errorMessage = String(error);
          stream.push({ type: "error", reason: "error", error: output });
          stream.end();
        }
      });
      return stream;
    },
  });
}
