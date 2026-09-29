"""本地 MaaFramework 图像匹配实验室服务。"""

from __future__ import annotations

import base64
import argparse
import copy
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = ROOT / "assets" / "resource"
PIPELINE_ROOT = ASSET_ROOT / "pipeline"
INSTANCE_ROOT = ROOT / "config" / "instances"
RUNTIME_ROOT = Path(tempfile.gettempdir()) / "MGA-match-lab"
PAGE_PATH = Path(__file__).parent / "index.html"
SERVICE_NAME = "MGA-Match-Lab"
Maa_BIN = ROOT / "python" / "Lib" / "site-packages" / "maa" / "bin"
PORT = 0
MAX_IMAGE_BYTES = 32 * 1024 * 1024
SUPPORTED = {"TemplateMatch", "FeatureMatch", "ColorMatch"}
PORT_FILE = RUNTIME_ROOT / "port.txt"
LOGGER = logging.getLogger("maa_match_lab")

# 让项目自带的 Maa Python 绑定加载项目随包的 MaaFramework DLL。
os.environ["MAAFW_BINARY_PATH"] = str(Maa_BIN)
os.environ["PATH"] = str(Maa_BIN) + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory") and Maa_BIN.is_dir():
    os.add_dll_directory(str(Maa_BIN))

try:
    import numpy as np
    from PIL import Image
    from maa.controller import AdbController
    from maa.define import MaaAdbScreencapMethodEnum
    from maa.library import Library
    from maa.pipeline import JPipelineParser, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker
except Exception as exc:  # 让页面仍能展示，错误会由 /api/health 传给界面。
    np = None
    Image = None
    AdbController = None
    MaaAdbScreencapMethodEnum = None
    Library = None
    JPipelineParser = None
    JRecognitionType = None
    Resource = None
    Tasker = None
    IMPORT_ERROR = f"Maa 运行库加载失败：{exc}"
else:
    IMPORT_ERROR = ""


def _json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    return value if isinstance(value, dict) else {}


def _recognition(node: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """兼容 Pipeline v1 与 v2 节点格式。"""
    value = node.get("recognition")
    if isinstance(value, dict):
        kind = value.get("type")
        params = value.get("param", {})
        return kind if isinstance(kind, str) else None, params if isinstance(params, dict) else {}
    if isinstance(value, str):
        ignored = {
            "recognition", "action", "next", "on_error", "focus", "enabled", "timeout",
            "rate_limit", "pre_delay", "post_delay", "pre_wait_freezes", "post_wait_freezes",
            "repeat", "repeat_delay", "repeat_wait_freezes", "max_hit", "inverse_roi",
            "attach", "focus", "anchor", "jump_back",
        }
        return value, {key: item for key, item in node.items() if key not in ignored and not key.startswith("$")}
    return None, {}


def _action(node: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """读取 Pipeline v1/v2 节点的动作类型和参数。"""
    value = node.get("action")
    if isinstance(value, dict):
        kind = value.get("type")
        params = value.get("param", {})
        return kind if isinstance(kind, str) else None, params if isinstance(params, dict) else {}
    if isinstance(value, str):
        return value, {"target": node.get("target")}
    return None, {}


def _entries() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    pipelines: list[dict[str, str]] = []
    entries: list[dict[str, Any]] = []
    if not PIPELINE_ROOT.is_dir():
        return entries, pipelines
    for path in sorted(PIPELINE_ROOT.rglob("*.json")):
        relative = path.relative_to(PIPELINE_ROOT).as_posix()
        pipelines.append({"path": relative, "label": path.stem})
        try:
            data = _json(path)
        except (OSError, json.JSONDecodeError):
            continue
        for name, node in data.items():
            if name.startswith("$") or not isinstance(node, dict):
                continue
            kind, params = _recognition(node)
            if kind in SUPPORTED:
                action_type, action_params = _action(node)
                entries.append({
                    "name": name,
                    "file": relative,
                    "type": kind,
                    "roi": params.get("roi", [0, 0, 0, 0]),
                    "template": params.get("template"),
                    "action_type": action_type,
                    "action_target": action_params.get("target"),
                })
    return entries, pipelines


def _configured_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    if not INSTANCE_ROOT.is_dir():
        return devices
    for path in sorted(INSTANCE_ROOT.glob("*.json")):
        try:
            data = _json(path)
            adb = data.get("AdbDevice") or {}
            adb_path = str(adb.get("AdbPath", "")).replace("/", "\\")
            serial = str(adb.get("AdbSerial", ""))
            if not adb_path or not serial:
                continue
            config = adb.get("Config") or {}
            # MGA instance files store Config as a JSON string, but Maa's
            # AdbController expects a Python object and serializes it itself.
            if isinstance(config, str):
                config = json.loads(config) if config.strip() else {}
            if not isinstance(config, dict):
                continue
            key = hashlib.sha1(f"{adb_path}\n{serial}".encode("utf-8")).hexdigest()[:16]
            device = {
                "id": key,
                "name": str(data.get("Name") or adb.get("Name") or serial),
                "adb_path": adb_path,
                "serial": serial,
                "config": config,
                "instance": str(data.get("Name") or path.stem),
            }
            if not any(item["id"] == key for item in devices):
                devices.append(device)
        except (OSError, json.JSONDecodeError):
            continue
    return devices


def _image_from_base64(value: Any) -> np.ndarray:
    if not isinstance(value, str):
        raise ValueError("图片数据为空")
    if value.startswith("data:"):
        value = value.split(",", 1)[1] if "," in value else ""
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("图片数据无法解码") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 32 MB")
    try:
        with Image.open(io.BytesIO(raw)) as decoded:
            if decoded.width > 8000 or decoded.height > 8000:
                raise ValueError("图片宽高不能超过 8000 像素")
            rgb = np.asarray(decoded.convert("RGB"), dtype=np.uint8)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取这张图片，请使用 PNG、JPG 或 WebP") from exc
    # Maa ImageBuffer 使用 BGR，与项目的原生 OpenCV 识别器一致。
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _png_data(image: np.ndarray) -> str:
    return base64.b64encode(_png_bytes(image)).decode("ascii")


def _png_bytes(image: np.ndarray) -> bytes:
    encoded = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(image[:, :, ::-1])).save(encoded, format="PNG")
    return encoded.getvalue()


def _rect(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if all(hasattr(value, key) for key in ("x", "y", "w", "h")):
        return {"x": int(value.x), "y": int(value.y), "w": int(value.w), "h": int(value.h)}
    return value


def _result_dict(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    result = {"box": _rect(getattr(item, "box", None))}
    for key in ("score", "count", "text", "label"):
        value = getattr(item, key, None)
        if value is not None:
            result[key] = value
    return result


def _node_recognition(node: Any) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(node, dict):
        return None, {}
    return _recognition(node)


class MaaMatcher:
    """把实验室操作交给项目随包的 MaaFramework 识别器。"""

    def __init__(self) -> None:
        if IMPORT_ERROR:
            raise RuntimeError(IMPORT_ERROR)
        if not ASSET_ROOT.is_dir():
            raise RuntimeError(f"找不到资源目录：{ASSET_ROOT}")
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.tasker = Tasker()
        self.resource = Resource()
        load_job = self.resource.post_bundle(ASSET_ROOT)
        load_job.wait()
        if not load_job.status.succeeded:
            raise RuntimeError("MaaFramework 加载 assets/resource 失败，请检查本机 Maa 日志")
        if not Library.framework().MaaTaskerBindResource(self.tasker._handle, self.resource._handle):
            raise RuntimeError("MaaFramework 绑定资源失败")
        self.loaded_templates: set[str] = set()

    def node_data(self, entry: str) -> dict[str, Any]:
        node = self.resource.get_node_data(entry)
        if not node:
            raise ValueError(f"Pipeline 中找不到节点「{entry}」")
        kind, params = _node_recognition(node)
        if kind not in SUPPORTED:
            raise ValueError(f"节点「{entry}」的识别类型是 {kind or '未知'}，本工具支持 TemplateMatch、FeatureMatch、ColorMatch")
        return {"name": entry, "type": kind, "params": params}

    def reload_assets(self) -> None:
        if not self.resource.clear():
            raise RuntimeError("Maa 资源正在加载，暂时无法刷新 Pipeline 缓存")
        load_job = self.resource.post_bundle(ASSET_ROOT)
        load_job.wait()
        if not load_job.status.succeeded:
            raise RuntimeError("MaaFramework 重新加载 assets/resource 失败，请检查本机 Maa 日志")
        self.loaded_templates.clear()
        self.tasker.clear_cache()

    def _load_user_template(self, template_image: np.ndarray) -> str:
        digest = hashlib.sha256(template_image.tobytes() + str(template_image.shape).encode("ascii")).hexdigest()[:24]
        key = f"__match_lab__/{digest}.png"
        if key in self.loaded_templates:
            return key
        bundle = RUNTIME_ROOT / "templates" / digest
        image_path = bundle / "image" / "__match_lab__" / f"{digest}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(_png_bytes(template_image))
        job = self.resource.post_bundle(bundle)
        job.wait()
        if not job.status.succeeded:
            raise RuntimeError("MaaFramework 加载粘贴的模板图失败")
        self.loaded_templates.add(key)
        return key

    def match(self, body: dict[str, Any]) -> dict[str, Any]:
        target = _image_from_base64(body.get("source"))
        entry = str(body.get("entry") or "").strip()
        mode = str(body.get("mode") or "TemplateMatch")
        if entry:
            node = self.node_data(entry)
            if node["type"] != mode:
                raise ValueError(f"所选 Pipeline 节点使用 {node['type']}，请切换到对应模式")
            params = copy.deepcopy(node["params"])
        else:
            params = {}
        if mode not in SUPPORTED:
            raise ValueError("请选择 TemplateMatch、FeatureMatch 或 ColorMatch")

        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("匹配参数格式无效")
        allowed = {
            "TemplateMatch": {"roi", "template", "threshold", "method", "green_mask", "order_by", "index"},
            "FeatureMatch": {"roi", "template", "count", "detector", "ratio", "green_mask", "order_by", "index"},
            "ColorMatch": {"roi", "lower", "upper", "count", "method", "connected", "order_by", "index"},
        }[mode]
        for key, value in overrides.items():
            if key in allowed:
                params[key] = value

        if isinstance(params.get("roi"), str):
            raise ValueError(
                f"此节点的 ROI 引用了前序节点「{params['roi']}」。单节点识别没有前序结果，请关闭“使用 Pipeline ROI”并手动填写 ROI。"
            )

        uploaded_template = body.get("template")
        if mode in ("TemplateMatch", "FeatureMatch"):
            if uploaded_template:
                template_image = _image_from_base64(uploaded_template)
                params["template"] = self._load_user_template(template_image)
            if not params.get("template"):
                raise ValueError("请粘贴/选择一张模板图，或选择一个包含 template 的 Pipeline 节点")
            if isinstance(params["template"], str):
                params["template"] = [params["template"]]
            if mode == "TemplateMatch" and "threshold" in overrides and len(params["template"]) > 1:
                params["threshold"] = [overrides["threshold"]] * len(params["template"])
            if mode == "TemplateMatch" and "threshold" not in params:
                params["threshold"] = [0.7] * len(params["template"])
        elif mode == "ColorMatch":
            if "lower" not in params or "upper" not in params:
                raise ValueError("ColorMatch 需要设置 lower 和 upper 颜色范围")

        try:
            reco_type = JRecognitionType(mode)
            reco_param = JPipelineParser._parse_recognition_param(reco_type, params)
        except Exception as exc:
            raise ValueError(f"Pipeline 参数无法解析：{exc}") from exc

        job = self.tasker.post_recognition(reco_type, reco_param, target)
        job.wait()
        if not job.status.succeeded:
            raise RuntimeError("MaaFramework 识别任务执行失败，请检查识别参数或日志")
        task_detail = job.get()
        if task_detail is None:
            raise RuntimeError("MaaFramework 没有返回识别详情")
        reco = next((node.recognition for node in task_detail.nodes if node.recognition is not None), None)
        if reco is None:
            raise RuntimeError("MaaFramework 没有返回识别结果")

        all_results = [_result_dict(item) for item in reco.all_results]
        best = _result_dict(reco.best_result)
        score = None
        count = None
        if best:
            score = best.get("score")
            count = best.get("count")
        # 某些识别器在未命中时仍提供 all_results；保留最佳原始分数供调参使用。
        if score is None and mode == "TemplateMatch":
            scores = [item.get("score") for item in all_results if item and isinstance(item.get("score"), (int, float))]
            score = max(scores) if scores else None
        if count is None and mode in ("FeatureMatch", "ColorMatch"):
            counts = [item.get("count") for item in all_results if item and isinstance(item.get("count"), int)]
            count = max(counts) if counts else None

        return {
            "mode": mode,
            "entry": entry,
            "hit": bool(reco.hit),
            "box": _rect(reco.box),
            "best": best,
            "score": score,
            "count": count,
            "all_results": all_results,
            "roi": params.get("roi", [0, 0, 0, 0]),
            "threshold": params.get("threshold"),
            "count_threshold": params.get("count"),
            "image_width": int(target.shape[1]),
            "image_height": int(target.shape[0]),
        }


_matcher: MaaMatcher | None = None
_matcher_lock = threading.RLock()


def _get_matcher() -> MaaMatcher:
    global _matcher
    with _matcher_lock:
        if _matcher is None:
            _matcher = MaaMatcher()
        return _matcher


def _device(device_id: str) -> dict[str, Any]:
    for item in _configured_devices():
        if item["id"] == device_id:
            return item
    raise ValueError("设备配置已变化，请刷新设备列表")


def _screencap(device_id: str) -> dict[str, Any]:
    if IMPORT_ERROR or AdbController is None:
        raise RuntimeError(IMPORT_ERROR or "Maa ADB 控制器不可用")
    device = _device(device_id)
    controller = None
    try:
        LOGGER.info(
            "[screencap] Creating ADB controller: device=%s, serial=%s, adb=%s",
            device["name"], device["serial"], device["adb_path"],
        )
        controller = AdbController(
            adb_path=device["adb_path"],
            address=device["serial"],
            screencap_methods=MaaAdbScreencapMethodEnum.Default,
            config=device["config"],
        )
        LOGGER.info("[screencap] Connecting to %s", device["serial"])
        connection = controller.post_connection()
        connection.wait()
        if not connection.status.succeeded or not controller.connected:
            raise RuntimeError(f"连接模拟器失败：{device['name']} ({device['serial']})")
        LOGGER.info("[screencap] Connected; requesting screenshot")
        job = controller.post_screencap()
        job.wait()
        if not job.status.succeeded:
            raise RuntimeError("模拟器截图失败")
        image = job.get()
        if image is None or image.size == 0:
            raise RuntimeError("模拟器没有返回截图")
        LOGGER.info("[screencap] Screenshot received: %sx%s", image.shape[1], image.shape[0])
        return {
            "data": _png_data(image),
            "name": f"{device['name']} · Maa 截图",
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
        }
    finally:
        if controller is not None and getattr(controller, "_handle", None):
            Library.framework().MaaControllerDestroy(controller._handle)
            controller._handle = None


class Handler(BaseHTTPRequestHandler):
    server_version = "MaaMatchLab/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _allowed_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                return False
            try:
                if parsed.port not in (None, PORT):
                    return False
            except ValueError:
                return False
        return True

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > MAX_IMAGE_BYTES * 3:
            raise ValueError("请求为空或超过大小限制")
        value = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求内容格式无效")
        return value

    def do_GET(self) -> None:
        if not self._allowed_host():
            self._json(403, {"error": "仅允许本机访问"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            try:
                content = PAGE_PATH.read_bytes()
                self._send(200, content, "text/html; charset=utf-8")
            except OSError as exc:
                self._json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/health":
            self._json(200, {
                "service": SERVICE_NAME,
                "port": PORT,
                "pid": os.getpid(),
                "ok": not bool(IMPORT_ERROR),
                "maa": "ready" if _matcher is not None else ("error" if IMPORT_ERROR else "not_loaded"),
                "version": Library.version() if not IMPORT_ERROR else None,
                "error": IMPORT_ERROR or None,
            })
            return
        if parsed.path == "/api/pipelines":
            entries, pipelines = _entries()
            self._json(200, {"pipelines": pipelines, "entries": entries})
            return
        if parsed.path == "/api/node":
            query = parse_qs(parsed.query)
            entry = (query.get("entry") or [""])[0]
            if not entry:
                self._json(400, {"error": "缺少 Pipeline 节点名"})
                return
            try:
                with _matcher_lock:
                    self._json(200, _get_matcher().node_data(entry))
            except Exception as exc:
                self._json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/devices":
            public_devices = [
                {"id": item["id"], "name": item["name"], "serial": item["serial"]}
                for item in _configured_devices()
            ]
            self._json(200, {"devices": public_devices})
            return
        self._json(404, {"error": "找不到此路径"})

    def do_POST(self) -> None:
        if not self._allowed_host():
            self._json(403, {"error": "仅允许本机访问"})
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/pipelines/refresh":
                with _matcher_lock:
                    if _matcher is not None:
                        _matcher.reload_assets()
                entries, pipelines = _entries()
                self._json(200, {"pipelines": pipelines, "entries": entries})
                return
            body = self._body()
            if parsed.path == "/api/match":
                with _matcher_lock:
                    result = _get_matcher().match(body)
                self._json(200, result)
                return
            if parsed.path == "/api/screencap":
                self._json(200, _screencap(str(body.get("device_id") or "")))
                return
            if parsed.path == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._json(404, {"error": "找不到此路径"})
        except (ValueError, KeyError) as exc:
            LOGGER.warning("Request rejected: %s %s: %s", self.command, parsed.path, exc)
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            LOGGER.exception("Request failed: %s %s: %s", self.command, parsed.path, exc)
            self._json(500, {"error": str(exc)})


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global PAGE_PATH, PORT_FILE, RUNTIME_ROOT, SERVICE_NAME
    parser = argparse.ArgumentParser(description="本地 Maa 图像匹配与 ROI 工具服务")
    parser.add_argument("--roi-only", action="store_true", help="仅提供独立 ROI 坐标工具页面")
    args = parser.parse_args()
    if args.roi_only:
        SERVICE_NAME = "MGA-ROI-Lab"
        PAGE_PATH = Path(__file__).resolve().parents[1] / "roi-lab" / "index.html"
        RUNTIME_ROOT = Path(tempfile.gettempdir()) / "MGA-roi-lab"
    PORT_FILE = RUNTIME_ROOT / "port.txt"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    if Image is None and not args.roi_only:
        print(IMPORT_ERROR, file=sys.stderr)
        raise SystemExit(1)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    log_dir = RUNTIME_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if IMPORT_ERROR:
        LOGGER.warning("Maa ADB screenshot support is unavailable: %s", IMPORT_ERROR)
    else:
        Tasker._set_api_properties()
        Tasker.set_log_dir(log_dir)
    LOGGER.info("Maa native logs: %s", log_dir)
    global PORT
    server = LocalServer(("127.0.0.1", 0), Handler)
    PORT = int(server.server_address[1])
    PORT_FILE.write_text(str(PORT), encoding="ascii")
    url = f"http://127.0.0.1:{PORT}/"
    LOGGER.info("%s 已启动：%s", "Maa ROI 坐标工具" if args.roi_only else "Maa 图像匹配实验室", url)
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        LOGGER.exception("Could not open the browser automatically")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if PORT_FILE.read_text(encoding="ascii").strip() == str(PORT):
                PORT_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
