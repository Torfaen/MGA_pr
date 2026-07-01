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

        print(f"[运行子任务] 开始执行: {entry}")
        result = context.run_task(entry)
        if result is None:
            print(f"MGA_TASK_FAILED: [运行子任务] 子任务执行失败: {entry}")
            return False

        print(f"[运行子任务] 子任务执行完成: {entry}")
        return True
