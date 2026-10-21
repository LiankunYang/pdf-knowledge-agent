"""限制可用工具，记录模型请求、工具结果和执行状态。"""
import json
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

class ApplicationToolsOnly(AgentMiddleware):
    """按当前会话的白名单筛选可用工具。"""
    allowed = {"search_web", "read_web_page", "inspect_sample_pdf", "ask_user", "save_report", "write_todos"}

    def wrap_model_call(self, request, handler):
        tools = [t for t in request.tools if (t.get("name") if isinstance(t, dict) else getattr(t, "name", None)) in self.allowed]
        return handler(request.override(tools=tools))

    def wrap_tool_call(self, request, handler):
        if request.tool_call["name"] not in self.allowed:
            return ToolMessage(content="Tool outside this experiment's allowed scope.", tool_call_id=request.tool_call["id"], status="error")
        return handler(request)


class SessionSupport:
    def clean(self, value):
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, list): return [self.clean(v) for v in value]
            if isinstance(value, dict): return {str(k): self.clean(v) for k, v in value.items()}
            return value


def run_agent(session, agent, task, config=None):
    """记录框架流式返回的模型请求、工具结果和执行状态。"""
    config = config or {"recursion_limit": 30}
    pending = []
    answer = ""
    try:
        for update in agent.stream(task, config=config, stream_mode="updates"):
            if "__interrupt__" in update:
                for item in update["__interrupt__"]:
                    value = item.value
                    pending.append(value)
                    session.log("human_interrupt", payload=value)
                    print("\n[等待人工反馈]", json.dumps(value, ensure_ascii=False)[:1200], flush=True)
                continue
            for node, changes in update.items():
                if not isinstance(changes, dict):
                    continue
                for message in changes.get("messages", []):
                    if isinstance(message, AIMessage):
                        session.log("model_response", content_characters=len(str(message.content)),
                                    finish_reason=message.response_metadata.get("finish_reason"),
                                    invalid_tool_calls=message.invalid_tool_calls, usage=message.usage_metadata)
                        if message.invalid_tool_calls:
                            print("模型返回了未能解析的工具参数，请检查本轮记录。", flush=True)
                            raise RuntimeError("Invalid model tool-call arguments")
                        if message.tool_calls:
                            session.log("model_tool_request", calls=message.tool_calls, usage=message.usage_metadata)
                            for call in message.tool_calls:
                                args = dict(call["args"])
                                if "report_markdown" in args:
                                    args["report_markdown"] = f"[完整报告 {len(args['report_markdown'])} 字符]"
                                print("\n[模型请求]", call["name"], json.dumps(args, ensure_ascii=False), flush=True)
                        elif message.content:
                            answer = str(message.content)
                            session.log("model_answer", content=answer, usage=message.usage_metadata)
                            print("\n[模型回答]", answer, flush=True)
                        else:
                            raise RuntimeError("Model returned neither usable text nor a tool call")
                    elif isinstance(message, ToolMessage):
                        session.log("tool_result", name=message.name, tool_call_id=message.tool_call_id, content=message.content)
                        print("[工具返回]", message.name, str(message.content)[:450], flush=True)
        session.log("execution_end", waiting_for_human=bool(pending), reports=len(session.reports))
    except Exception as error:
        session.log("execution_error", error_type=type(error).__name__, http_status=getattr(error, "status_code", None))
        print("本轮错误类型：", type(error).__name__, flush=True)
        raise RuntimeError("执行未完成，真实过程已保留在run.json；请先定位错误。") from None
    return {"interrupts": pending, "answer": answer, "reports": session.reports}
