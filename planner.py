"""Versioned, explainable teaching rules. No clinical model is bundled."""
from __future__ import annotations

import math

ALGORITHM_VERSION = "ortho-rules-2.0.0"
CATALOG_VERSION = "synthetic-components-2.0"
SCENARIOS = {"spine": "脊柱侧弯", "joint": "关节置换", "trauma": "骨折创伤"}
LABELS = {
    "cobb_angle": "Cobb 角（°）", "flexibility_pct": "柔韧性（%）",
    "coronal_balance_mm": "冠状面失衡（mm）", "apex_region": "顶椎区域",
    "joint_type": "关节类型", "bone_diameter_mm": "旧版骨性尺寸（mm）",
    "acetabular_diameter_mm": "髋臼参考直径（mm）", "canal_width_mm": "股骨髓腔参考宽度（mm）",
    "offset_mm": "股骨偏心距（mm）", "bone_quality": "骨质情况", "side": "侧别",
    "femoral_ap_mm": "股骨前后径（mm）", "femoral_ml_mm": "股骨内外径（mm）",
    "tibial_ap_mm": "胫骨前后径（mm）", "tibial_ml_mm": "胫骨内外径（mm）",
    "fracture_site": "骨折部位", "fracture_segment": "骨折骨段",
    "fracture_pattern": "骨折形态", "displacement_mm": "移位（mm）",
    "articular_involvement": "关节面受累", "soft_tissue": "软组织情况", "open_status": "开放情况",
}
VALUES = {"thoracic": "胸椎", "thoracolumbar": "胸腰段", "lumbar": "腰椎",
          "hip": "髋关节", "knee": "膝关节", "normal": "一般", "low": "偏低",
          "left": "左侧", "right": "右侧", "unspecified": "未说明", "unknown": "待评估",
          "femur": "股骨", "tibia": "胫骨", "radius": "桡骨", "proximal": "近端",
          "shaft": "骨干", "distal": "远端", "simple": "简单", "comminuted": "粉碎",
          "intact": "完整", "compromised": "受损", "closed": "闭合", "open": "开放"}


def display_value(value):
    if isinstance(value, bool):
        return "是" if value else "否"
    return VALUES.get(str(value), str(value))


def number(data, key, low, high):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{LABELS.get(key, key)}必须是数值")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{LABELS.get(key, key)}范围应为 {low}–{high}")
    return float(value)


def choice(data, key, choices):
    value = data.get(key)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{LABELS.get(key, key)}选项无效")
    return value


def validate_measurements(scenario, data):
    if not isinstance(data, dict):
        raise ValueError("缺少测量参数")
    m = dict(data)
    if scenario == "spine":
        for key, low, high in [("cobb_angle", 10, 100), ("flexibility_pct", 0, 100), ("coronal_balance_mm", 0, 100)]:
            m[key] = number(m, key, low, high)
        choice(m, "apex_region", ("thoracic", "thoracolumbar", "lumbar"))
        allowed = {"cobb_angle", "flexibility_pct", "coronal_balance_mm", "apex_region"}
    elif scenario == "joint":
        joint = choice(m, "joint_type", ("hip", "knee"))
        choice(m, "bone_quality", ("normal", "low"))
        m.setdefault("side", "unspecified")
        choice(m, "side", ("left", "right", "unspecified"))
        if joint == "hip":
            if "acetabular_diameter_mm" not in m and "bone_diameter_mm" in m:
                m["acetabular_diameter_mm"] = m["bone_diameter_mm"]
            for key, low, high in [("acetabular_diameter_mm", 35, 90), ("canal_width_mm", 5, 25), ("offset_mm", 0, 70)]:
                m[key] = number(m, key, low, high)
            allowed = {"joint_type", "bone_quality", "side", "acetabular_diameter_mm", "canal_width_mm", "offset_mm"}
        else:
            for key in ("femoral_ap_mm", "femoral_ml_mm", "tibial_ap_mm", "tibial_ml_mm"):
                m[key] = number(m, key, 25, 110)
            allowed = {"joint_type", "bone_quality", "side", "femoral_ap_mm", "femoral_ml_mm", "tibial_ap_mm", "tibial_ml_mm"}
    elif scenario == "trauma":
        choice(m, "fracture_site", ("femur", "tibia", "radius"))
        choice(m, "fracture_pattern", ("simple", "comminuted"))
        choice(m, "soft_tissue", ("intact", "compromised"))
        m["displacement_mm"] = number(m, "displacement_mm", 0, 50)
        if not isinstance(m.get("articular_involvement"), bool):
            raise ValueError("关节面受累必须为布尔值 true 或 false")
        m.setdefault("fracture_segment", "unspecified")
        m.setdefault("open_status", "unknown")
        choice(m, "fracture_segment", ("proximal", "shaft", "distal", "unspecified"))
        choice(m, "open_status", ("closed", "open", "unknown"))
        allowed = {"fracture_site", "fracture_pattern", "soft_tissue", "displacement_mm", "articular_involvement", "fracture_segment", "open_status"}
    else:
        raise ValueError("不支持的规划场景")
    return {key: value for key, value in m.items() if key in allowed}


def analyze(scenario, measurements):
    m = validate_measurements(scenario, measurements)
    result = {"spine": _spine, "joint": _joint, "trauma": _trauma}[scenario](m)
    result.update(algorithm_version=ALGORITHM_VERSION, catalog_version=CATALOG_VERSION,
                  evidence_level="教学规则，未进行临床有效性验证")
    result.setdefault("staged_options", [])
    result.setdefault("rejected_options", [])
    return result


def _spine(m):
    cobb, flex, balance = m["cobb_angle"], m["flexibility_pct"], m["coronal_balance_mm"]
    correction = min(.72, max(.28, .28 + flex * .004))
    target = round(cobb * (1 - correction), 1)
    options = [
        {"name": "标准评估路径", "score": 82 if flex >= 35 else 69,
         "reason": f"柔韧性 {flex:g}%；教学规则示意目标 {target}°。融合节段需另行确认。",
         "contributions": [f"柔韧性分支：{'≥35%' if flex >= 35 else '<35%'}", "分数来自教学规则，未校准为概率"]},
        {"name": "补充动态影像评估路径", "score": 76 if flex < 35 or cobb >= 60 else 62,
         "reason": f"Cobb {cobb:g}°，柔韧性 {flex:g}%；补充侧屈位、多弯及矢状面信息。",
         "contributions": [f"大角度或低柔韧性分支：{'触发' if flex < 35 or cobb >= 60 else '未触发'}"]},
    ]
    options.sort(key=lambda o: o["score"], reverse=True)
    flags = ["当前参数不足以确定融合节段或预测真实术后效果。"]
    if cobb >= 60:
        flags.append("大角度输入：需进一步核查影像、柔韧性及神经相关评估。")
    if flex < 25:
        flags.append("低柔韧性输入：示意矫正幅度需独立复核。")
    if balance >= 40:
        flags.append("冠状面失衡输入较大：需核查全脊柱站立位测量。")
    return {"title": "脊柱侧弯辅助规划", "readiness": "review_required",
            "summary": f"教学规则的示意目标为 {target}°。候选路径按规则分排序，尚未生成融合节段方案。",
            "metrics": [{"label": "输入 Cobb 角", "value": f"{cobb:g}°"}, {"label": "示意目标", "value": f"{target}°"}, {"label": "示意矫正比例", "value": f"{correction:.0%}"}],
            "ranked_options": options, "flags": flags,
            "required_review": ["端椎及关键点确认", "站立位、侧屈位与多弯信息", "病种分层与矢状面平衡"],
            "method": "保留原教学公式。柔韧性 0–100% 对应有效输出 28–68%；72% 为当前不可达的上限。未估算患者特异融合节段。"}


CUPS = [{"id": f"H{d}", "diameter": d, "family": "DEMO-A"} for d in (48, 52, 56)]
STEMS = [{"id": f"S{c}", "canal": c, "offset": o, "family": "DEMO-A"} for c, o in ((12, 38), (13, 42), (14, 46))]
KNEES = [{"id": f"K{a}", "femoral_ap_mm": a, "femoral_ml_mm": b, "tibial_ap_mm": c, "tibial_ml_mm": d}
         for a, b, c, d in ((58, 62, 42, 66), (62, 66, 46, 70), (66, 70, 50, 74))]


def _joint(m):
    eligible, rejected = [], []
    if m["joint_type"] == "hip":
        for cup in CUPS:
            for stem in STEMS:
                diffs = [abs(cup["diameter"] - m["acetabular_diameter_mm"]), abs(stem["canal"] - m["canal_width_mm"]), abs(stem["offset"] - m["offset_mm"])]
                error = sum(d * w for d, w in zip(diffs, (5, 8, 2)))
                reasons = [f"杯直径差 {diffs[0]:g} mm", f"柄髓腔参考差 {diffs[1]:g} mm", f"股骨偏心距差 {diffs[2]:g} mm"]
                failures = [f"{label}超出演示筛选范围" for label, d, lim in zip(("杯直径", "髓腔参考", "偏心距"), diffs, (4, 3, 8)) if d > lim]
                option = {"name": f"演示杯 {cup['id']} + 柄 {stem['id']}", "score": max(0, round(100 - error)), "raw_error": error,
                          "reason": "；".join(reasons), "contributions": [f"{r}，扣分 {d*w:g}" for r, d, w in zip(reasons, diffs, (5, 8, 2))],
                          "components": [cup["id"], stem["id"]], "exclusion_reasons": failures}
                (rejected if failures else eligible).append(option)
        metrics = [{"label": "髋臼参考直径", "value": f"{m['acetabular_diameter_mm']:g} mm"}, {"label": "股骨髓腔参考", "value": f"{m['canal_width_mm']:g} mm"}]
    else:
        keys = ("femoral_ap_mm", "femoral_ml_mm", "tibial_ap_mm", "tibial_ml_mm")
        for item in KNEES:
            diffs = [abs(item[k] - m[k]) for k in keys]
            error = sum(d*w for d, w in zip(diffs, (4, 3, 4, 3)))
            reasons = [f"{LABELS[k].split('（')[0]}差 {d:g} mm" for k, d in zip(keys, diffs)]
            failures = [r + "，超出演示筛选范围" for r, d in zip(reasons, diffs) if d > 5]
            option = {"name": f"演示膝组件组 {item['id']}", "score": max(0, round(100-error)), "raw_error": error,
                      "reason": "；".join(reasons), "contributions": reasons, "components": [item["id"]], "exclusion_reasons": failures}
            (rejected if failures else eligible).append(option)
        metrics = [{"label": "股骨前后径", "value": f"{m['femoral_ap_mm']:g} mm"}, {"label": "胫骨内外径", "value": f"{m['tibial_ml_mm']:g} mm"}]
    eligible.sort(key=lambda o: (o["raw_error"], o["name"]))
    rejected.sort(key=lambda o: (o["raw_error"], o["name"]))
    # Zero-score entries are never surfaced as usable candidates, even if all individual checks passed.
    rejected += [dict(o, exclusion_reasons=["综合误差过大，无有效匹配分"]) for o in eligible if o["score"] <= 0]
    eligible = [o for o in eligible if o["score"] > 0]
    flags = ["器械目录与筛选阈值均为演示配置；没有真实厂商兼容或三维贴合验证。"]
    if m["bone_quality"] == "low":
        flags.append("骨质偏低：需独立复核固定方式与稳定性，不能由尺寸分确定。")
    if not eligible:
        flags.append("当前目录无符合演示约束的候选，请补充资料或扩展有效器械目录。")
    return {"title": "关节组件匹配", "readiness": "review_required" if eligible else "no_candidate",
            "summary": f"演示目录中 {len(eligible)} 组通过几何筛选，排序第一为{eligible[0]['name']}。" if eligible else "当前目录没有可用候选，已停止首选推荐。",
            "metrics": metrics + [{"label": "通过筛选", "value": f"{len(eligible)} 组"}], "ranked_options": eligible,
            "rejected_options": rejected, "flags": flags,
            "required_review": ["影像标定及左右侧", "组件规格与真实兼容关系", "摆放姿态、骨覆盖及固定方式"],
            "method": "髋臼杯与股骨柄分开建模；先按演示几何阈值筛选，再按未截断误差排序。髋膝使用不同测量字段。分数不表示成功率。"}


def _trauma(m):
    plate, nail = 67, 68
    contributions = {"锁定钢板": ["教学初始分 67"], "髓内钉": ["教学初始分 68"]}
    changes = []
    if m["fracture_site"] == "radius":
        changes.append((15, -25, "桡骨部位规则"))
    else:
        changes.append((0, 13, "长骨部位规则"))
    if m["fracture_pattern"] == "comminuted":
        changes.append((5, 3, "粉碎形态规则"))
    if m["articular_involvement"]:
        changes.append((13, -10, "关节面受累规则"))
    if m["soft_tissue"] == "compromised":
        changes.append((-16, -10, "软组织条件规则"))
    for p, n, label in changes:
        plate += p
        nail += n
        contributions["锁定钢板"].append(f"{label} {p:+d}")
        contributions["髓内钉"].append(f"{label} {n:+d}")
    options = [{"name": name, "score": score, "reason": "；".join(contributions[name]), "contributions": contributions[name], "phase": "最终固定方式讨论"}
               for name, score in (("锁定钢板", plate), ("髓内钉", nail))]
    options.sort(key=lambda o: o["score"], reverse=True)
    flags = ["仅提供固定方式讨论；未生成器械规格、复位或螺钉路径方案。"]
    incomplete = m["fracture_segment"] == "unspecified" or m["open_status"] == "unknown"
    if incomplete:
        flags.append("骨段或开放情况尚未明确，资料不足，不形成首选结论。")
    staged = []
    if m["soft_tissue"] == "compromised" or m["open_status"] == "open":
        staged = [{"name": "阶段性稳定评估", "reason": "将外固定等阶段性策略单列讨论；转换条件需结合软组织、感染与整体情况确认。", "phase": "阶段性处理", "score": None}]
        flags.append("需单独评估软组织和开放情况，阶段性处理与最终固定候选分开展示。")
    if m["articular_involvement"]:
        flags.append("关节面受累：需补齐关节面与骨折分型信息。")
    if m["displacement_mm"] > 15:
        flags.append("移位输入较大：需复核影像与复位目标。")
    return {"title": "骨折固定策略讨论", "readiness": "needs_input" if incomplete else "review_required",
            "summary": "资料不足，以下仅列出一般候选，请先补充骨段与开放情况。" if incomplete else "分别展示最终固定候选与阶段性处理，排序仅反映教学规则。",
            "metrics": [{"label": "部位 / 骨段", "value": f"{VALUES[m['fracture_site']]} / {VALUES[m['fracture_segment']]}"}, {"label": "移位", "value": f"{m['displacement_mm']:g} mm"}, {"label": "开放情况", "value": VALUES[m["open_status"]]}],
            "ranked_options": options, "staged_options": staged, "flags": flags,
            "required_review": ["骨折分型与完整影像", "软组织、血运神经与感染风险", "最终固定与阶段性方案的适用条件"],
            "method": "保留可解释教学加减分，并逐项显示贡献。阶段性稳定不与内固定方式混合排名；骨段和开放情况缺失时不输出首选。"}


def cobb_from_points(points, width, height):
    if not isinstance(points, list) or len(points) != 4:
        raise ValueError("请依次选择两条终板线的四个端点")
    coords = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("测量点格式无效")
        coords.append((number({"x": point[0]}, "x", 0, 1)*width, number({"y": point[1]}, "y", 0, 1)*height))
    angles = []
    for a, b in ((coords[0], coords[1]), (coords[2], coords[3])):
        if math.hypot(b[0]-a[0], b[1]-a[1]) < 5:
            raise ValueError("每条终板线的两个端点至少相距 5 像素")
        angles.append(math.degrees(math.atan2(b[1]-a[1], b[0]-a[0])))
    delta = abs(angles[0]-angles[1]) % 180
    return round(min(delta, 180-delta), 1)
