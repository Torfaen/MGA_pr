import json
from difflib import SequenceMatcher
import re
import time
import unicodedata

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


_target_unit_name = ""
_target_unit_rarity = ""
_target_unit_type = ""

_TARGET_RARITY_ROI = [58, 88, 35, 35]
_TARGET_TYPE_ROI = [92, 88, 35, 35]
_DETAIL_RARITY_ROI = [65, 385, 60, 75]
_DETAIL_TYPE_ROI = [120, 385, 60, 75]
_ROUTE_TYPE_ICON_CLICK_OFFSET = [0, -130]
_ROUTE_TYPE_ICON_MIN_CENTER_Y = 260


def _parse_params(raw: str) -> dict:
    if isinstance(raw, dict):
        return raw
    parsed = json.loads(raw or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _screencap(context: Context):
    return context.tasker.controller.post_screencap().wait().get()


def _ocr_texts(context: Context, image, node_name: str) -> list[str]:
    detail = context.run_recognition(node_name, image)
    if not detail:
        return []

    texts = []
    for attr in ("best_result",):
        item = getattr(detail, attr, None)
        text = getattr(item, "text", "")
        if text:
            texts.append(text)

    for attr in ("filtered_results", "all_results"):
        for item in getattr(detail, attr, []) or []:
            text = getattr(item, "text", "")
            if text:
                texts.append(text)

    return texts


def _normalize_name(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text).upper()


def _name_score(text: str) -> int:
    normalized = _normalize_name(text)
    if not normalized:
        return 0

    blocked = (
        "CAPITAL",
        "NONE",
        "持有",
        "数量",
        "战斗力",
        "地形",
        "适性",
        "装置",
        "目标",
        "单位",
        "信息",
        "所需",
        "材料",
        "取消",
        "全部开发",
        "获取途径",
        "强化",
    )
    if any(word in normalized for word in blocked):
        return 0

    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", normalized))
    has_alnum = bool(re.search(r"[0-9A-Z]", normalized))
    if not has_cjk:
        return 0

    return len(normalized) + (3 if has_alnum else 0)


def _pick_unit_name(texts: list[str]) -> str:
    candidates = [(text, _name_score(text)) for text in texts]
    candidates = [(text, score) for text, score in candidates if score > 0]
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[1])[0].strip()


def _names_match(target: str, current: str) -> bool:
    target_norm = _normalize_name(target)
    current_norm = _normalize_name(current)
    if not target_norm or not current_norm:
        return False
    if target_norm == current_norm:
        return True

    max_len = max(len(target_norm), len(current_norm))
    min_len = min(len(target_norm), len(current_norm))
    max_delta = max(1, int(max_len * 0.2))
    if max_len - min_len > max_delta:
        return False

    return SequenceMatcher(None, target_norm, current_norm).ratio() >= 0.72


def _name_similarity(target: str, current: str) -> float:
    target_norm = _normalize_name(target)
    current_norm = _normalize_name(current)
    if not target_norm or not current_norm:
        return 0.0
    return SequenceMatcher(None, target_norm, current_norm).ratio()


def _crop_roi(image, roi: list[int]):
    if image is None or len(image.shape) < 3:
        return None

    height, width = image.shape[:2]
    x, y, w, h = [int(value) for value in roi]
    x1 = max(0, min(width, x))
    y1 = max(0, min(height, y))
    x2 = max(0, min(width, x + w))
    y2 = max(0, min(height, y + h))
    if x1 >= x2 or y1 >= y2:
        return None
    return image[y1:y2, x1:x2, :3].astype(np.int16)


def _mask_score(mask) -> int:
    return int(mask.sum())


def _detect_rarity(image, roi: list[int]) -> str:
    crop = _crop_roi(image, roi)
    if crop is None:
        return ""

    b = crop[:, :, 0]
    g = crop[:, :, 1]
    r = crop[:, :, 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    saturation = maxc - minc
    area = crop.shape[0] * crop.shape[1]

    scores = {
        "SSR": _mask_score((r > 155) & (g > 110) & (b < 125) & (r >= g - 20) & (saturation > 45)),
        "SR": _mask_score((b > 115) & (g > 105) & (r < 165) & (b >= r + 15) & (g >= r + 5) & (saturation > 35)),
        "R": _mask_score((r > 125) & (g > 55) & (g < 155) & (b < 135) & (r >= g + 15) & (saturation > 40)),
    }
    threshold = max(18, int(area * 0.012))
    best, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score >= threshold:
        return best

    neutral_score = _mask_score((maxc > 120) & (saturation < 60))
    if neutral_score >= max(24, int(area * 0.02)):
        return "N"
    return ""


def _detect_unit_type(image, roi: list[int]) -> str:
    crop = _crop_roi(image, roi)
    if crop is None:
        return ""

    b = crop[:, :, 0]
    g = crop[:, :, 1]
    r = crop[:, :, 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    saturation = maxc - minc
    area = crop.shape[0] * crop.shape[1]

    scores = {
        "攻击": _mask_score((r > 120) & (r > g + 30) & (r > b + 35) & (saturation > 45)),
        "支援": _mask_score((r > 115) & (g > 95) & (b < 125) & (np.abs(r - g) < 80) & (saturation > 35)),
        "耐久": _mask_score((b > 115) & (b > g + 15) & (b > r + 35) & (saturation > 40)),
    }
    best, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score >= max(14, int(area * 0.01)):
        return best
    return ""


def _detect_unit_traits(image, rarity_roi: list[int], type_roi: list[int]) -> tuple[str, str]:
    return _detect_rarity(image, rarity_roi), _detect_unit_type(image, type_roi)


def _traits_match(target_rarity: str, target_type: str, current_rarity: str, current_type: str) -> bool:
    return bool(target_rarity and target_type and current_rarity and current_type) and (
        target_rarity == current_rarity and target_type == current_type
    )


def _format_traits(name: str, rarity: str, unit_type: str) -> str:
    return f"name={name or '-'}, rarity={rarity or '-'}, type={unit_type or '-'}"


def _click_point(context: Context, x: int, y: int) -> None:
    context.tasker.controller.post_click(int(x), int(y)).wait()
    time.sleep(0.8)


def _click_center(context: Context, roi: list[int]) -> None:
    x, y, w, h = roi
    _click_point(context, x + w // 2, y + h // 2)


def _connected_components(mask, min_area: int = 80) -> list[tuple[int, int, int, int, int]]:
    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    components: list[tuple[int, int, int, int, int]] = []

    for start_y in range(height):
        for start_x in range(width):
            if visited[start_y, start_x] or not mask[start_y, start_x]:
                continue

            stack = [(start_x, start_y)]
            visited[start_y, start_x] = True
            xs: list[int] = []
            ys: list[int] = []

            while stack:
                x, y = stack.pop()
                xs.append(x)
                ys.append(y)
                for ny in range(max(0, y - 1), min(height, y + 2)):
                    for nx in range(max(0, x - 1), min(width, x + 2)):
                        if visited[ny, nx] or not mask[ny, nx]:
                            continue
                        visited[ny, nx] = True
                        stack.append((nx, ny))

            area = len(xs)
            if area < min_area:
                continue
            components.append((area, min(xs), min(ys), max(xs) + 1, max(ys) + 1))

    return components


def _has_route_quantity_box(image, icon_x: int, icon_y: int) -> bool:
    crop = _crop_roi(image, [icon_x + 22, icon_y - 24, 120, 52])
    if crop is None or crop.shape[1] < 95:
        return False

    b = crop[:, :, 0]
    g = crop[:, :, 1]
    r = crop[:, :, 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    muted = (maxc < 150) & (maxc - minc < 95)
    return int(muted.sum()) >= 3000


def _detect_route_type_icons(image, max_candidates: int = 24) -> list[tuple[int, int]]:
    if image is None or len(image.shape) < 3:
        return []

    height, width = image.shape[:2]
    search_x1 = min(250, width)
    search_y1 = min(162, height)
    search_x2 = min(width - 20, width)
    search_y2 = min(690, height)
    if search_x1 >= search_x2 or search_y1 >= search_y2:
        return []

    crop = image[search_y1:search_y2, search_x1:search_x2, :3].astype(np.int16)
    b = crop[:, :, 0]
    g = crop[:, :, 1]
    r = crop[:, :, 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    saturation = maxc - minc

    red = (r > 120) & (r > g + 30) & (r > b + 35) & (saturation > 45)
    yellow = (r > 115) & (g > 95) & (b < 125) & (np.abs(r - g) < 80) & (saturation > 35)
    blue = (b > 115) & (b > g + 15) & (b > r + 35) & (saturation > 40)
    icon_color = red | yellow | blue

    components: list[tuple[int, int, int, int, int]] = []
    for area, x0, y0, x1, y1 in _connected_components(icon_color, min_area=80):
        box_w = x1 - x0
        box_h = y1 - y0
        if 14 <= box_w <= 55 and 12 <= box_h <= 55:
            components.append((area, search_x1 + x0, search_y1 + y0, search_x1 + x1, search_y1 + y1))

    icons: list[tuple[int, int, int]] = []
    for area, x0, y0, x1, y1 in components:
        center_x = (x0 + x1) // 2
        center_y = (y0 + y1) // 2
        if any(
            25 <= other_x - center_x <= 75 and abs(other_y - center_y) <= 22
            for _other_area, other_x0, other_y0, other_x1, other_y1 in components
            for other_x, other_y in [((other_x0 + other_x1) // 2, (other_y0 + other_y1) // 2)]
        ):
            continue
        if center_y < _ROUTE_TYPE_ICON_MIN_CENTER_Y:
            continue
        if not _has_route_quantity_box(image, center_x, center_y):
            continue
        icons.append((area, center_x, center_y))

    icons.sort(reverse=True)
    centers: list[tuple[int, int]] = []
    min_dist_sq = 90 * 90
    for _area, x, y in icons:
        if any((x - old_x) * (x - old_x) + (y - old_y) * (y - old_y) < min_dist_sq for old_x, old_y in centers):
            continue
        centers.append((x, y))
        if len(centers) >= max_candidates:
            break

    return sorted(centers, key=lambda point: (point[1], point[0]))


def _detect_route_candidates(
    image,
    max_candidates: int = 24,
    click_offset: list[int] | None = None,
) -> list[tuple[int, int]]:
    offset_x, offset_y = click_offset or _ROUTE_TYPE_ICON_CLICK_OFFSET
    icons = _detect_route_type_icons(image, max_candidates)
    return [(x + int(offset_x), y + int(offset_y)) for x, y in icons]


@AgentServer.custom_action("角色要求交付机体记录目标")
class RoleRequirementDeliveryRememberTarget(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _target_unit_name, _target_unit_rarity, _target_unit_type

        params = _parse_params(argv.custom_action_param)
        image = _screencap(context)
        texts = _ocr_texts(context, image, "角色要求_交付机体目标信息OCR")
        name = _pick_unit_name(texts)
        if not name:
            print(f"MGA_TASK_FAILED: [角色要求] 交付机体目标名识别失败，OCR={texts}")
            return False

        rarity_roi = params.get("rarity_roi", _TARGET_RARITY_ROI)
        type_roi = params.get("type_roi", _TARGET_TYPE_ROI)
        rarity, unit_type = _detect_unit_traits(image, rarity_roi, type_roi)
        if not rarity or not unit_type:
            print(
                "MGA_TASK_FAILED: [角色要求] 交付机体目标稀有度/类型识别失败，"
                f"{_format_traits(name, rarity, unit_type)}"
            )
            return False

        _target_unit_name = name
        _target_unit_rarity = rarity
        _target_unit_type = unit_type
        print(f"[角色要求] 交付机体目标：{_format_traits(_target_unit_name, _target_unit_rarity, _target_unit_type)}")
        return True


@AgentServer.custom_action("角色要求交付机体选择目标")
class RoleRequirementDeliverySelectTarget(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = _parse_params(argv.custom_action_param)
        target_name = params.get("target") or _target_unit_name
        target_rarity = params.get("rarity") or _target_unit_rarity
        target_type = params.get("unit_type") or params.get("type") or _target_unit_type
        if not target_name or not target_rarity or not target_type:
            print(
                "MGA_TASK_FAILED: [角色要求] 交付机体缺少目标信息，"
                f"{_format_traits(target_name, target_rarity, target_type)}"
            )
            return False

        image = _screencap(context)
        candidates = _detect_route_candidates(
            image,
            int(params.get("max_candidates", 24)),
            params.get("route_icon_click_offset", _ROUTE_TYPE_ICON_CLICK_OFFSET),
        )
        if not candidates:
            print("MGA_TASK_FAILED: [角色要求] 开发路线图未识别到候选机体节点")
            return False

        print(f"[角色要求] 开发路线图候选机体节点：{candidates}")
        cancel_roi = params.get("cancel_roi", [380, 620, 260, 90])
        detail_rarity_roi = params.get("detail_rarity_roi", _DETAIL_RARITY_ROI)
        detail_type_roi = params.get("detail_type_roi", _DETAIL_TYPE_ROI)

        for index, (x, y) in enumerate(candidates, 1):
            if not context.tasker.running:
                return False

            print(f"[角色要求] 尝试候选机体 {index}/{len(candidates)}: ({x}, {y})")
            _click_point(context, x, y)

            detail_image = _screencap(context)
            texts = _ocr_texts(context, detail_image, "角色要求_交付机体详情名OCR")
            info_title = _ocr_texts(context, detail_image, "角色要求_开发目标单位信息OCR")
            current_name = _pick_unit_name(texts)
            current_rarity, current_type = _detect_unit_traits(detail_image, detail_rarity_roi, detail_type_roi)
            name_ok = _names_match(target_name, current_name)
            traits_ok = _traits_match(target_rarity, target_type, current_rarity, current_type)
            similarity = _name_similarity(target_name, current_name)
            print(
                "[角色要求] 候选机体："
                f"{_format_traits(current_name or str(texts), current_rarity, current_type)}，"
                f"目标={_format_traits(target_name, target_rarity, target_type)}，"
                f"name_ok={name_ok}, traits_ok={traits_ok}, similarity={similarity:.2f}"
            )

            if name_ok and traits_ok:
                print(f"[角色要求] 已选择交付机体：{_format_traits(current_name, current_rarity, current_type)}")
                return True

            if current_name or any("开发目标单位信息" in text for text in info_title):
                result = context.run_task("角色要求_取消开发目标信息")
                if result is None:
                    _click_center(context, cancel_roi)

        print(f"MGA_TASK_FAILED: [角色要求] 未找到目标交付机体：{target_name}")
        return False
