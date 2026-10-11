"""Validate shared role dispatch with a local, simulated Maa controller."""
import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import LoggingLevelEnum, Tasker

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
# Register these same classes on the local Resource below, without opening an
# AgentServer transport in this standalone native-framework test process.
with patch("maa.library.Library.open"), patch(
    "maa.agent.agent_server.AgentServer.custom_action", side_effect=lambda name: lambda cls: cls
):
    import RoleRequirementTracker as tracker


def arg(params):
    return SimpleNamespace(custom_action_param=json.dumps(params))


nodes = {}
for path in (ROOT / "assets/resource/pipeline").glob("*.json"):
    for name, node in json.loads(path.read_text(encoding="utf-8")).items():
        if not name.startswith("$__"):
            assert name not in nodes, name
            nodes[name] = node

role_nodes = {name: node for name, node in nodes.items() if name.startswith("角色要求")}
for name, node in role_nodes.items():
    assert not any(f"检测{slot}角色" in name or f"处理{slot}角色" in name for slot in "左中右")
    for field in ("next", "on_error"):
        for child in node.get(field, []):
            assert child in nodes, (name, field, child)

record = tracker.RoleRequirementRecord()
run = tracker.RoleRequirementRunAndRecord()
with contextlib.redirect_stdout(io.StringIO()):
    ctx = Mock()
    assert record.run(ctx, arg({"op": "reset"}))
    assert not run.run(ctx, arg({"slot": "当前", "type": "交付机体", "entry": "test"}))
    assert not record.run(ctx, arg({"op": "skip", "slot": "当前"}))
    ctx.run_task.assert_not_called()
    assert tracker._records == {}
    for slot in "左中右":
        params = nodes[f"角色要求_点击{slot}角色_全检查"]["action"]["param"]["custom_action_param"]
        assert record.run(ctx, arg(params))
        overrides = ctx.override_pipeline.call_args.args[0]
        assert overrides["角色要求_点击当前角色_全检查"]["action"]["param"]["target"] == params["target"]
        assert overrides["角色要求_当前角色返回列表_全检查"]["next"] == [tracker._SLOT_NEXT[slot]]
    ctx.override_pipeline.return_value = False
    assert not record.run(ctx, arg(params))
    assert tracker._current_slot is None
    assert not record.run(ctx, arg({"op": "select_slot", "slot": "错", "target": [0, 0, 1, 1]}))
    tracker._current_slot = "右"
    tracker._current_run_skip_reason = "old"
    assert record.run(ctx, arg({"op": "reset"}))
    assert tracker._current_slot is tracker._current_run_skip_reason is None
print("位置未设置、配置失败、非法位置和重置检查通过。")


class LocalController(CustomController):
    def __init__(self):
        super().__init__()
        self.clicks = []

    def connect(self): return True
    def request_uuid(self): return "role-flow-local-test"
    def get_features(self): return 0
    def screencap(self): return np.zeros((720, 1280, 3), dtype=np.uint8)
    def click(self, x, y):
        self.clicks.append((x, y))
        return True
    def start_app(self, *args): raise AssertionError("unexpected operation")
    stop_app = swipe = touch_down = touch_move = touch_up = start_app
    click_key = input_text = key_down = key_up = start_app


class Outcome(CustomAction):
    current = "done"

    def run(self, context, argv):
        if self.current == "skip":
            return tracker.RoleRequirementMarkCurrentRunSkipped().run(context, arg({"reason": "条件不满足"}))
        return self.current == "done"


Tasker.set_stdout_level(LoggingLevelEnum.Off)
resource = Resource()
assert resource.post_bundle(ROOT / "assets/resource").wait().status.succeeded
assert resource.register_custom_action("角色要求记录", record)
assert resource.register_custom_action("角色要求执行并记录", run)
outcome = Outcome()
assert resource.register_custom_action("RoleFlowTestOutcome", outcome)
controller = LocalController()
assert controller.post_connection().wait().status.succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)

base = {name: {"pre_delay": 0, "post_delay": 0, "post_wait_freezes": 0, "timeout": 100, "rate_limit": 10}
        for name in role_nodes}
leaf = {"recognition": {"type": "DirectHit", "param": {}},
        "action": {"type": "DoNothing", "param": {}}, "next": [], "on_error": []}
for name in [*tracker._SLOT_NEXT.values(), "角色要求_等待角色列表_全检查", "角色要求_查找失败"]:
    base[name] = {**base[name], **leaf}
base["角色要求_当前角色返回列表_全检查"] = {
    **base["角色要求_当前角色返回列表_全检查"],
    "recognition": {"type": "DirectHit", "param": {}},
    "action": {"type": "DoNothing", "param": {}},
}
detectors = nodes["角色要求_点击当前角色_全检查"]["next"]
types = ["强化部队", "调度资金", "交付机体", "委托开发", "请求出击"]
cases = 0
with contextlib.redirect_stdout(io.StringIO()):
    for slot in "左中右":
        for kind in types:
            for status in ("done", "failed", "skip"):
                tracker._records.clear()
                tracker._current_slot = None
                controller.clicks.clear()
                outcome.current = status
                overrides = json.loads(json.dumps(base))
                for name in detectors:
                    overrides[name] = {**overrides[name], "enabled": False}
                detector = f"角色要求_检测{kind}_全检查"
                overrides[detector] = {**overrides[detector], "enabled": True,
                                       "recognition": {"type": "DirectHit", "param": {}}}
                child = nodes[f"角色要求_处理{kind}"]["action"]["param"]["custom_action_param"]["entry"]
                overrides[child] = {**base[child], **leaf,
                                    "action": {"type": "Custom", "param": {"custom_action": "RoleFlowTestOutcome"}}}
                job = tasker.post_task(f"角色要求_点击{slot}角色_全检查", overrides).wait()
                assert job.status.succeeded, (slot, kind, status)
                assert tracker._records == {slot: ({"status": status, "type": kind} if status != "skip"
                                                  else {"status": "skip", "reason": "条件不满足"})}, (slot, kind, status, tracker._records)
                target = nodes[f"角色要求_点击{slot}角色_全检查"]["action"]["param"]["custom_action_param"]["target"]
                assert len(controller.clicks) == 1, controller.clicks
                x, y = controller.clicks[0]
                assert target[0] <= x <= target[0] + target[2] and target[1] <= y <= target[1] + target[3]
                last = tasker.get_node_detail(job.get().node_id_list[-1]).name
                expected = ("角色要求_等待角色列表_全检查" if status == "done" else
                            "角色要求_查找失败" if kind in ("强化部队", "调度资金") else tracker._SLOT_NEXT[slot])
                assert last == expected, (slot, kind, status, last, expected)
                cases += 1
        # Unknown type must return according to the selected slot after timeout.
        tracker._records.clear()
        overrides = json.loads(json.dumps(base))
        for name in detectors:
            overrides[name] = {**overrides[name], "enabled": False}
        job = tasker.post_task(f"角色要求_点击{slot}角色_全检查", overrides).wait()
        assert job.status.succeeded
        assert tracker._records == {slot: {"status": "skip", "reason": "类别不明"}}
        assert tasker.get_node_detail(job.get().node_id_list[-1]).name == tracker._SLOT_NEXT[slot]
        # Special commission skip paths use the same current-slot record and return.
        special = "角色要求_检测特殊委托_全检查"
        unsupported = ["HARD主要关卡", "事件关卡", "击破单位", "获得单位", "内容不明"]
        for kind in unsupported:
            tracker._records.clear()
            overrides = json.loads(json.dumps(base))
            for name in detectors:
                overrides[name] = {**overrides[name], "enabled": False}
            overrides[special] = {**overrides[special], "enabled": True,
                                  "recognition": {"type": "DirectHit", "param": {}}}
            for name in nodes[special]["next"]:
                overrides[name] = {**overrides[name], "enabled": False}
            reason = "特殊委托内容无法判定"
            if kind != "内容不明":
                name = f"角色要求_特殊委托{kind}"
                overrides[name] = {**overrides[name], "enabled": True,
                                   "recognition": {"type": "DirectHit", "param": {}}}
                reason = f"特殊委托：{kind}尚未支持"
            job = tasker.post_task(f"角色要求_点击{slot}角色_全检查", overrides).wait()
            assert job.status.succeeded, (slot, kind)
            assert tracker._records == {slot: {"status": "skip", "reason": reason}}, (slot, kind, tracker._records)
            assert tasker.get_node_detail(job.get().node_id_list[-1]).name == tracker._SLOT_NEXT[slot]
print(f"原生流水线 {cases} 个完成/失败/软跳过场景及 3 个类别不明返回场景通过。")
print("原生流水线 15 个特殊委托跳过/内容不明返回场景通过。")
