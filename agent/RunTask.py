import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


@AgentServer.custom_action("运行子任务")
class RunTask(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = json.loads(argv.custom_action_param or "{}")
        entry = params.get("entry")
        if not entry:
            print("MGA_TASK_FAILED: [运行子任务] 未配置 entry")
            return False
        pipeline_override = params.get("pipeline_override", {})
        if not isinstance(pipeline_override, dict):
            print("MGA_TASK_FAILED: [运行子任务] pipeline_override 必须是对象")
            return False
        return context.run_task(entry, pipeline_override=pipeline_override) is not None
