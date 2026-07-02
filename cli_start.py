import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INSTANCES_DIR = ROOT / "config" / "instances"
DEPS_BIN = ROOT / "deps" / "bin"
CLI_EXE = DEPS_BIN / "MaaPiCli.exe"
CLI_CONFIG_DIR = DEPS_BIN / "config"
CLI_INTERFACE = DEPS_BIN / "interface.json"
CLI_CONFIG = CLI_CONFIG_DIR / "maa_pi_config.json"
CLI_AGENT_BINARY = DEPS_BIN / "MaaAgentBinary"
SOURCE_AGENT_BINARY = ROOT / "deps" / "share" / "MaaAgentBinary"
MAA_DEBUG_LOG = DEPS_BIN / "debug" / "maa.log"
SUCCESS_LOG = "任务已全部完成"
ERROR_LOG = "MGA_TASK_FAILED|已放弃本次任务"
ERROR_LOG_MARKERS = tuple(marker for marker in ERROR_LOG.split("|") if marker)
CRITICAL_MAA_LOG_MARKERS = (
    "Parse config failed",
    "Failed to create control unit",
    "MaaAdbControllerCreate] Failed",
    "handle is null",
)
MAA_TASK_START_MARKER = "MaaNS::Tasker::post_task"
MAA_EVENT_RE = re.compile(r"\[msg=([^\]]+)\].*\[details=(\{.*\})\]\s*$")
MAA_SOURCE_RE = re.compile(r"^\[[^\]]+\](?:\[[^\]]+\]){6}")
SKIP_ACTION_TYPES = {"DoNothing"}


def read_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")


def automas_log_path(now=None):
    now = now or datetime.now()
    return ROOT / "logs" / f"log-{now:%Y%m%d}.log"


def append_automas_log(level, instance_name, message):
    now = datetime.now()
    path = automas_log_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"[{now:%Y-%m-%d %H:%M:%S.%f}][{level}] "
        f"[cfg=CLI][inst={instance_name}][src=cli_start][op=RunMaaPiCli] {message}\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line)

    MAA_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MAA_DEBUG_LOG.open("a", encoding="utf-8", errors="replace") as f:
        f.write(line)


def append_automas_bridge_log(level, instance_name, message):
    now = datetime.now()
    path = automas_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"[{now:%Y-%m-%d %H:%M:%S.%f}][{level}] "
        f"[cfg=CLI][inst={instance_name}][src=cli_start][op=MaaPiCliLog] {message}\n"
    )
    with path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(line)


def parse_maa_event(line):
    if "MaaNS::EventDispatcher::notify" not in line:
        return None
    match = MAA_EVENT_RE.search(line)
    if match is None:
        return None
    try:
        return match.group(1), json.loads(match.group(2))
    except json.JSONDecodeError:
        return None


def summarize_maa_event(msg, details):
    entry = details.get("entry")
    name = details.get("name")

    if msg == "Tasker.Task.Starting" and entry:
        return "INF", f"Maa任务开始：{entry}"
    if msg == "Tasker.Task.Succeeded" and entry:
        return "INF", f"Maa任务完成：{entry}"
    if msg == "Tasker.Task.Failed" and entry:
        return "WRN", f"Maa任务失败：{entry}"

    action_details = details.get("action_details") or {}
    action_type = action_details.get("action")
    action_name = action_details.get("name") or name
    if msg == "Node.Action.Succeeded" and action_name:
        if action_type in SKIP_ACTION_TYPES:
            return None
        suffix = f" ({action_type})" if action_type else ""
        return "INF", f"动作成功：{action_name}{suffix}"
    if msg == "Node.Action.Failed" and action_name:
        suffix = f" ({action_type})" if action_type else ""
        return "WRN", f"动作失败：{action_name}{suffix}"

    if msg == "Node.PipelineNode.Failed" and name:
        return "WRN", f"节点失败：{name}"

    focus = details.get("focus")
    if msg == "Node.Recognition.Failed" and name and isinstance(focus, dict):
        hint = focus.get("Node.Recognition.Failed")
        if hint:
            return "WRN", f"识别未命中：{name} - {hint}"

    return None


def summarize_generic_maa_log(line):
    if any(marker in line for marker in ERROR_LOG_MARKERS):
        return None
    if "][ERR]" in line:
        level = "ERR"
    elif "][WRN]" in line:
        level = "WRN"
    else:
        return None

    text = MAA_SOURCE_RE.sub("", line).strip()
    if not text:
        text = line[-180:].strip()
    if len(text) > 180:
        text = f"{text[:177]}..."
    return level, f"Maa{('错误' if level == 'ERR' else '警告')}：{text}"


def summarize_maa_log(line):
    if not line.startswith("[20"):
        return None
    event = parse_maa_event(line)
    if event is not None:
        summary = summarize_maa_event(*event)
        if summary is not None:
            return summary
    return summarize_generic_maa_log(line)


def bridge_maa_log(process, start_pos, stop_event, instance_name):
    pos = start_pos
    pending = ""
    last_summary = None
    while True:
        if MAA_DEBUG_LOG.exists():
            size = MAA_DEBUG_LOG.stat().st_size
            if size < pos:
                pos = 0
                pending = ""
            if size > pos:
                with MAA_DEBUG_LOG.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                text = pending + chunk.decode("utf-8", errors="replace")
                lines = text.splitlines(keepends=True)
                pending = ""
                if lines and not lines[-1].endswith(("\n", "\r")):
                    pending = lines.pop()
                for line in lines:
                    line = line.rstrip("\r\n")
                    summary = summarize_maa_log(line)
                    if summary is not None and summary != last_summary:
                        level, message = summary
                        append_automas_bridge_log(level, instance_name, message)
                        last_summary = summary
        if stop_event.is_set() and process.poll() is not None:
            summary = summarize_maa_log(pending) if pending else None
            if summary is not None and summary != last_summary:
                level, message = summary
                append_automas_bridge_log(level, instance_name, message)
            break
        time.sleep(0.5)


def normalize_name(value):
    return value.strip().casefold()


def load_instances():
    instances = []
    for path in sorted(INSTANCES_DIR.glob("*.json")):
        data = read_json(path)
        name = data.get("InstanceName")
        if name:
            instances.append((path.stem, name, path, data))
    return instances


def find_instance(name):
    matches = [
        item
        for item in load_instances()
        if normalize_name(item[1]) == normalize_name(name) or normalize_name(item[0]) == normalize_name(name)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        available = ", ".join(item[1] for item in load_instances()) or "无"
        raise SystemExit(f"找不到配置：{name}\n可用配置：{available}")
    raise SystemExit(f"配置名不唯一：{name}")


def cases_for_option(interface, option_name):
    option = interface.get("option", {}).get(option_name, {})
    return option.get("cases", []) or []


def case_name_by_index(interface, option_name, index):
    cases = cases_for_option(interface, option_name)
    if index is None:
        default_case = interface.get("option", {}).get(option_name, {}).get("default_case")
        if default_case:
            return default_case
        index = 0
    if not cases:
        return None
    if index < 0 or index >= len(cases):
        raise SystemExit(f"选项 {option_name} 的 index 超出范围：{index}")
    return cases[index].get("name")


def cli_options_from_gui(interface, gui_option):
    name = gui_option.get("name")
    if not name:
        return []

    option_def = interface.get("option", {}).get(name, {})
    option_type = option_def.get("type", "select")

    if option_type == "checkbox":
        selected = gui_option.get("selected_cases")
        if selected is None:
            selected = []
            index = gui_option.get("index")
            case_name = case_name_by_index(interface, name, index)
            if case_name:
                selected.append(case_name)
        return [{"name": name, "values": selected}]

    if option_type == "input":
        inputs = gui_option.get("inputs") or gui_option.get("input") or gui_option.get("value") or {}
        return [{"name": name, "inputs": inputs}]

    value = gui_option.get("value")
    if value is None:
        value = case_name_by_index(interface, name, gui_option.get("index"))

    result = {"name": name, "value": value}
    results = [result]
    for sub in gui_option.get("sub_options", []) or []:
        results.extend(cli_options_from_gui(interface, sub))
    return results


def selected_tasks_from_instance(interface, instance):
    tasks = []
    for item in instance.get("TaskItems", []) or []:
        if not item.get("default_check", False):
            continue
        name = item.get("name")
        if not name:
            continue
        task = {"name": name}
        options = []
        for option in item.get("option", []) or []:
            options.extend(cli_options_from_gui(interface, option))
        if options:
            task["option"] = options
        tasks.append(task)
    return tasks


def build_adb_config(adb):
    config = adb.get("Config") or "{}"
    agent_path = adb.get("AgentPath") or str(CLI_AGENT_BINARY)
    agent_path = Path(agent_path)
    if not agent_path.is_absolute():
        agent_path = (DEPS_BIN / agent_path).resolve()

    return {
        "name": adb.get("Name", ""),
        "adb_path": adb.get("AdbPath", ""),
        "address": adb.get("AdbSerial", ""),
        "screencap_methods": adb.get("ScreencapMethods", 0),
        "input_methods": adb.get("InputMethods", 0),
        "config": config,
        "agent_path": str(agent_path),
    }


def build_cli_interface(interface):
    data = dict(interface)
    data["interface_version"] = 2

    for resource in data.get("resource", []) or []:
        paths = resource.get("path", []) or []
        resource["path"] = [str((ROOT / p).resolve()) if not Path(p).is_absolute() else p for p in paths]

    embedded_python = ROOT / "python" / "python.exe"
    python_exe = embedded_python if embedded_python.exists() else Path(sys.executable)
    data["agent"] = {
        "child_exec": str(python_exe.resolve()),
        "child_args": [str((ROOT / "agent" / "start_agent.py").resolve())],
    }

    return data


def build_cli_config(interface, instance):
    controller_name = instance.get("CurrentControllerName")
    controller = next(
        (item for item in interface.get("controller", []) if item.get("name") == controller_name),
        None,
    )
    if controller is None:
        raise SystemExit(f"interface.json 中找不到控制器：{controller_name}")

    adb = instance.get("AdbDevice") or {}

    config = {
        "controller": {
            "name": controller.get("name"),
            "type": controller.get("type"),
        },
        "adb": {
            **build_adb_config(adb),
        },
        "resource": instance.get("Resource"),
        "task": selected_tasks_from_instance(interface, instance),
    }

    if not config["task"]:
        raise SystemExit(f"配置 {instance.get('InstanceName')} 没有勾选任务")
    return config


def ensure_agent_binary():
    if CLI_AGENT_BINARY.exists():
        return
    if not SOURCE_AGENT_BINARY.exists():
        raise SystemExit(f"找不到 MaaAgentBinary：{SOURCE_AGENT_BINARY}")
    shutil.copytree(SOURCE_AGENT_BINARY, CLI_AGENT_BINARY)


def backup_path(path):
    backup = path.with_name(path.name + ".cli_start.bak")
    if not backup.exists():
        return backup
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return path.with_name(f"{path.name}.cli_start.{timestamp}.bak")


def install_runtime_files(interface, config):
    backups = []
    for path in (CLI_INTERFACE, CLI_CONFIG):
        if path.exists():
            backup = backup_path(path)
            if backup.exists():
                raise SystemExit(f"发现未恢复的 CLI 备份文件：{backup}")
            shutil.move(str(path), str(backup))
            backups.append((path, backup))
    write_json(CLI_INTERFACE, interface)
    write_json(CLI_CONFIG, config)
    return backups


def restore_runtime_files(backups):
    for path in (CLI_INTERFACE, CLI_CONFIG):
        if path.exists():
            path.unlink()
    for path, backup in backups:
        if backup.exists():
            if path.exists():
                path.unlink()
            shutil.move(str(backup), str(path))


def run_maapicli(env, maa_log_start, instance_name):
    process = subprocess.Popen([str(CLI_EXE)], cwd=str(DEPS_BIN), stdin=subprocess.PIPE, text=True, env=env)
    stop_event = threading.Event()
    bridge_thread = threading.Thread(
        target=bridge_maa_log,
        args=(process, maa_log_start, stop_event, instance_name),
        daemon=True,
    )
    bridge_thread.start()
    try:
        if process.stdin is not None:
            process.stdin.write("6\n")
            process.stdin.flush()
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        stop_event.set()
        bridge_thread.join(timeout=2)


def read_log_segment(path, start_pos):
    if not path.exists():
        return ""
    with path.open("rb") as f:
        if path.stat().st_size >= start_pos:
            f.seek(start_pos)
        return f.read().decode("utf-8", errors="replace")


def has_critical_maa_error(log_segment):
    return any(marker in log_segment for marker in CRITICAL_MAA_LOG_MARKERS)


def has_started_maa_task(log_segment):
    return MAA_TASK_START_MARKER in log_segment


def main():
    parser = argparse.ArgumentParser(description="按配置名以 MaaPiCli 命令行方式运行 MGA")
    parser.add_argument("-n", "--name", required=True, help="配置名字，例如：全套日常")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要运行的 MaaPiCli 配置，不启动")
    args = parser.parse_args()

    if not CLI_EXE.exists():
        raise SystemExit(f"找不到 MaaPiCli：{CLI_EXE}")

    _, instance_name, _, instance = find_instance(args.name)
    interface = read_json(ROOT / "interface.json")
    cli_interface = build_cli_interface(interface)
    cli_config = build_cli_config(interface, instance)

    if args.dry_run:
        print(json.dumps(cli_config, ensure_ascii=False, indent=4))
        return

    print(f"[MGA CLI] 使用配置：{instance_name}")
    print(f"[MGA CLI] 启动：{CLI_EXE}")
    append_automas_log("INF", instance_name, f"命令行启动开始：{CLI_EXE}")
    backups = install_runtime_files(cli_interface, cli_config)
    exit_code = 1
    try:
        ensure_agent_binary()
        env = os.environ.copy()
        env["MAAFW_BINARY_PATH"] = str(DEPS_BIN)
        maa_log_start = MAA_DEBUG_LOG.stat().st_size if MAA_DEBUG_LOG.exists() else 0
        exit_code = run_maapicli(env, maa_log_start, instance_name)
        maa_log_segment = read_log_segment(MAA_DEBUG_LOG, maa_log_start)
    finally:
        restore_runtime_files(backups)

    if (
        exit_code == 0
        and not has_critical_maa_error(maa_log_segment)
        and has_started_maa_task(maa_log_segment)
    ):
        append_automas_log("INF", instance_name, f"{SUCCESS_LOG}！")
    else:
        if exit_code == 0 and not has_started_maa_task(maa_log_segment):
            append_automas_log(
                "ERR",
                instance_name,
                f"{ERROR_LOG}，未检测到 MaaPiCli 任务启动",
            )
        else:
            append_automas_log("ERR", instance_name, f"{ERROR_LOG}，退出码={exit_code}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
