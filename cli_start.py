import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
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
CLI_STATE = DEPS_BIN / "cli_start_state.json"
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


def try_read_json(path):
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


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


def maa_cli_processes():
    if os.name != "nt":
        return []

    processes = []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MaaPiCli.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        result = None

    if result is not None and result.returncode == 0:
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) < 2 or row[0].upper() == "INFO:":
                continue
            try:
                pid = int(row[1])
            except ValueError:
                continue
            processes.append({"name": row[0], "pid": pid})

    try:
        ps_result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-Process -Name MaaPiCli -ErrorAction SilentlyContinue | ForEach-Object { \"$($_.ProcessName),$($_.Id)\" }",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        ps_result = None

    if ps_result is not None and ps_result.returncode == 0:
        known_pids = {item["pid"] for item in processes}
        for line in ps_result.stdout.splitlines():
            parts = line.strip().split(",", 1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            if pid not in known_pids:
                processes.append({"name": f"{parts[0]}.exe", "pid": pid})
                known_pids.add(pid)
    return processes


def format_processes(processes):
    return ", ".join(f"{item['name']}({item['pid']})" for item in processes) or "无"


def terminate_process(pid):
    if os.name == "nt":
        return subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    return subprocess.run(
        ["kill", "-TERM", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def cleanup_stale_maapicli(instance_name):
    processes = maa_cli_processes()
    if not processes:
        append_automas_log("INF", instance_name, "未发现残留 MaaPiCli 进程。")
        return True

    ok = True
    append_automas_log("WRN", instance_name, f"准备清理残留 MaaPiCli 进程：{format_processes(processes)}")
    for item in processes:
        result = terminate_process(item["pid"])
        if result.returncode == 0:
            append_automas_log("INF", instance_name, f"已清理残留 MaaPiCli：PID={item['pid']}")
        else:
            ok = False
            detail = (result.stderr or result.stdout or "").strip()
            append_automas_log("ERR", instance_name, f"清理残留 MaaPiCli 失败：PID={item['pid']} {detail}")
    return ok


def assert_no_maapicli_running(instance_name):
    processes = maa_cli_processes()
    if not processes:
        return
    message = f"发现已有 MaaPiCli 正在运行：{format_processes(processes)}。请先清理残留命令行任务。"
    append_automas_log("ERR", instance_name, message)
    raise SystemExit(message)


def assert_files_available(paths, instance_name):
    locked = []
    for path in paths:
        if path is None or not path.exists():
            continue
        try:
            with path.open("r+", encoding="utf-8"):
                pass
        except OSError as exc:
            locked.append(f"{path} ({exc})")
    if not locked:
        return
    message = "配置文件被其他进程占用，已中止启动：" + "；".join(locked)
    append_automas_log("ERR", instance_name, message)
    raise SystemExit(message)


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
    raw_config = adb.get("Config") or {}
    if isinstance(raw_config, str):
        try:
            raw_config = json.loads(raw_config) if raw_config.strip() else {}
        except json.JSONDecodeError:
            raw_config = {}

    return {
        "name": adb.get("Name", ""),
        "adb_path": adb.get("AdbPath", ""),
        "address": adb.get("AdbSerial", ""),
        "config": raw_config,
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
            "config": controller.get("config", "{}"),
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


def read_cli_state():
    data = try_read_json(CLI_STATE)
    if not isinstance(data, dict):
        return []
    backups = []
    for item in data.get("backups", []) or []:
        path = item.get("path")
        backup = item.get("backup")
        if path and backup:
            backups.append((Path(path), Path(backup)))
    return backups


def export_runtime_files(interface, config):
    write_json(CLI_INTERFACE, interface)
    write_json(CLI_CONFIG, config)
    if CLI_STATE.exists():
        CLI_STATE.unlink()


def restore_runtime_files(backups):
    for path in (CLI_INTERFACE, CLI_CONFIG):
        if path.exists():
            path.unlink()
    for path, backup in backups:
        if backup.exists():
            if path.exists():
                path.unlink()
            shutil.move(str(backup), str(path))
    if CLI_STATE.exists():
        CLI_STATE.unlink()


def restore_stale_runtime_files(instance_name):
    backups = read_cli_state()
    if not backups:
        return False
    missing = [backup for _, backup in backups if not backup.exists()]
    if missing:
        append_automas_log(
            "ERR",
            instance_name,
            "发现 CLI 运行状态文件，但备份缺失，无法自动恢复：" + ", ".join(str(path) for path in missing),
        )
        return False
    restore_runtime_files(backups)
    append_automas_log("INF", instance_name, "已恢复上次残留的 CLI 临时运行配置。")
    return True


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
    parser = argparse.ArgumentParser(description="按配置名同步 MaaPiCli 直接运行配置")
    parser.add_argument("-n", "--name", required=True, help="配置名字，例如：全套日常")
    parser.add_argument("--export-config", action="store_true", help="生成 MaaPiCli -d 使用的运行配置")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要同步的 MaaPiCli 配置，不写入")
    parser.add_argument("--clean-stale-cli", action="store_true", help="清理残留 MaaPiCli 进程并恢复上次 CLI 临时配置")
    args = parser.parse_args()

    if not CLI_EXE.exists():
        raise SystemExit(f"找不到 MaaPiCli：{CLI_EXE}")

    _, instance_name, instance_path, instance = find_instance(args.name)

    if args.clean_stale_cli:
        cleanup_ok = cleanup_stale_maapicli(instance_name)
        restore_stale_runtime_files(instance_name)
        raise SystemExit(0 if cleanup_ok else 1)

    interface = read_json(ROOT / "interface.json")
    cli_interface = build_cli_interface(interface)
    cli_config = build_cli_config(interface, instance)

    if args.dry_run:
        print(json.dumps(cli_config, ensure_ascii=False, indent=4))
        return

    assert_no_maapicli_running(instance_name)
    restore_stale_runtime_files(instance_name)
    if CLI_STATE.exists():
        message = f"发现未恢复的 CLI 运行状态文件：{CLI_STATE}，请先执行 --clean-stale-cli。"
        append_automas_log("ERR", instance_name, message)
        raise SystemExit(message)
    assert_files_available(
        [
            ROOT / "interface.json",
            CLI_INTERFACE,
            CLI_CONFIG,
            instance_path,
        ],
        instance_name,
    )

    print(f"[MGA CLI] 使用配置：{instance_name}")
    print(f"[MGA CLI] 已同步 MaaPiCli -d 配置：{CLI_CONFIG}")
    ensure_agent_binary()
    export_runtime_files(cli_interface, cli_config)
    append_automas_log("INF", instance_name, f"已同步 MaaPiCli -d 直接运行配置：{CLI_CONFIG}")


if __name__ == "__main__":
    main()
