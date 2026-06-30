import argparse
import json
import os
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
SUCCESS_LOG = "任务已全部完成"
ERROR_LOG = "MGA_TASK_FAILED|已放弃本次任务"


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
            "name": adb.get("Name", ""),
            "adb_path": adb.get("AdbPath", ""),
            "address": adb.get("AdbSerial", ""),
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


def run_maapicli(env):
    process = subprocess.Popen([str(CLI_EXE)], cwd=str(DEPS_BIN), stdin=subprocess.PIPE, text=True, env=env)
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
        exit_code = run_maapicli(env)
    finally:
        restore_runtime_files(backups)

    if exit_code == 0:
        append_automas_log("INF", instance_name, f"{SUCCESS_LOG}！")
    else:
        append_automas_log("ERR", instance_name, f"{ERROR_LOG}，退出码={exit_code}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
