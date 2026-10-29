"""Transparent, synthetic orthopedic planning algorithms for teaching only."""

from __future__ import annotations

def _number(data, key, low, high):
    try:
        if isinstance(data[key], bool):
            raise ValueError()
        value = float(data[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


def _choice(data, key, choices):
    value = data.get(key)
    if value not in choices:
        raise ValueError(f"{key} must be one of {', '.join(choices)}")
    return value


def analyze(scenario: str, measurements: dict) -> dict:
    functions = {"spine": _spine, "joint": _joint, "trauma": _trauma}
    if scenario not in functions:
        raise ValueError("Unsupported scenario")
    result = functions[scenario](measurements)
    result["ranked_options"].sort(key=lambda x: x["score"], reverse=True)
    result["model_version"] = "ORTHO-RULES 2.0"
    result["score_note"] = "教学规则评分，用于当前候选之间的比较，不代表成功率或临床有效性。"
    result["sensitivity"] = sensitivity(scenario, measurements)
    return result


def sensitivity(scenario, measurements):
    """One-variable exploration: hold all other inputs constant, no probabilities."""
    if scenario == "spine":
        key, label, unit, values, fn = "flexibility_pct", "柔韧性", "%", (15, 30, 45, 60, 75, 90), _spine
    elif scenario == "joint":
        key, label, unit, fn = "bone_diameter_mm", "骨性尺寸", "mm", _joint
        baseline = float(measurements[key])
        values = [max(35, min(90, baseline + offset)) for offset in (-4, -2, 0, 2, 4)]
    else:
        key, label, unit, values, fn = "displacement_mm", "移位距离", "mm", (5, 10, 15, 20, 30), _trauma
    points = []
    for value in values:
        r = fn({**measurements, key: value})
        top = max(r["ranked_options"], key=lambda x: x["score"])
        metric = round(float(measurements["cobb_angle"]) * (1 - min(0.72, max(0.28, 0.28 + value * 0.004))), 1) if scenario == "spine" else top["score"]
        points.append({"x": value, "y": metric, "option": top["name"]})
    return {"parameter": key, "label": label, "unit": unit,
            "metric": "示意目标 Cobb 角（°）" if scenario == "spine" else "首选候选评分",
            "baseline": float(measurements[key]), "points": points,
            "note": "仅改变一个输入参数，其他条件保持固定；用于理解规则响应。"}


def _spine(m):
    cobb = _number(m, "cobb_angle", 10, 100)
    flexibility = _number(m, "flexibility_pct", 0, 100)
    balance = _number(m, "coronal_balance_mm", 0, 100)
    apex = _choice(m, "apex_region", ("thoracic", "thoracolumbar", "lumbar"))
    correction = min(0.72, max(0.28, 0.28 + flexibility * 0.004))
    target = round(cobb * (1 - correction), 1)
    span = {
        "thoracic": "胸椎顶椎上下各 3 节段（示意）",
        "thoracolumbar": "胸腰段顶椎上下各 3 节段（示意）",
        "lumbar": "腰椎顶椎上下各 2 节段（示意）",
    }[apex]
    flags = []
    if cobb >= 60:
        flags.append("Cobb 角较大：应复核柔韧性与神经监测方案")
    if balance >= 40:
        flags.append("冠状面失衡较明显：应补充全脊柱站立位测量")
    if flexibility < 25:
        flags.append("柔韧性较低：目标矫正幅度需谨慎")
    return {
        "title": "脊柱侧弯术前规划建议",
        "summary": f"示意目标 Cobb 角约 {target}°，预测矫正比例 {correction * 100:.0f}%；建议围绕{span}进行人工规划。",
        "metrics": [
            {"label": "输入 Cobb 角", "value": f"{cobb:g}°"},
            {"label": "示意目标", "value": f"{target}°"},
            {"label": "柔韧性", "value": f"{flexibility:g}%"},
        ],
        "ranked_options": [
            {"name": "标准矫形路径", "score": 82 if flexibility >= 35 else 69,
             "reason": "按柔韧性估算矫正目标，节段范围仅为初始讨论草案。"},
            {"name": "分期评估路径", "score": 76 if flexibility < 35 or cobb >= 60 else 62,
             "reason": "在大角度或低柔韧性病例中优先补齐动态片与风险评估。"},
        ],
        "flags": flags or ["需由脊柱外科医师复核站立位与侧屈位影像。"],
        "required_review": ["影像测量复核", "融合节段与矢状面平衡", "神经监测与并发症评估"],
        "method": "启发式约束模型；矫正比例按柔韧性线性估算并限幅至 28%–72%。",
        "calculation": [f"矫正比例 = 28% + {flexibility:g}% × 0.4 = {correction * 100:.0f}%（限幅 28%–72%）",
                        f"示意目标 = {cobb:g}° × (1 − {correction * 100:.0f}%) = {target}°",
                        f"顶椎区域对应的讨论范围：{span}"],
    }


IMPLANTS = (
    {"name": "演示髋臼杯 H44", "joint": "hip", "diameter": 44, "canal": 11, "offset": 34},
    {"name": "演示髋臼杯 H48", "joint": "hip", "diameter": 48, "canal": 12, "offset": 38},
    {"name": "演示髋臼杯 H52", "joint": "hip", "diameter": 52, "canal": 13, "offset": 42},
    {"name": "演示髋臼杯 H56", "joint": "hip", "diameter": 56, "canal": 14, "offset": 46},
    {"name": "演示髋臼杯 H60", "joint": "hip", "diameter": 60, "canal": 15, "offset": 50},
    {"name": "演示膝关节组件 K58", "joint": "knee", "diameter": 58, "canal": 10, "offset": 0},
    {"name": "演示膝关节组件 K62", "joint": "knee", "diameter": 62, "canal": 11, "offset": 0},
    {"name": "演示膝关节组件 K66", "joint": "knee", "diameter": 66, "canal": 12, "offset": 0},
    {"name": "演示膝关节组件 K70", "joint": "knee", "diameter": 70, "canal": 13, "offset": 0},
    {"name": "演示膝关节组件 K74", "joint": "knee", "diameter": 74, "canal": 14, "offset": 0},
)


def _joint(m):
    joint = _choice(m, "joint_type", ("hip", "knee"))
    diameter = _number(m, "bone_diameter_mm", 35, 90)
    canal = _number(m, "canal_width_mm", 5, 25)
    offset = _number(m, "offset_mm", 0, 70) if joint == "hip" else 0
    quality = _choice(m, "bone_quality", ("normal", "low"))
    scores = []
    for item in IMPLANTS:
        if item["joint"] != joint:
            continue
        error = abs(item["diameter"] - diameter) * 3 + abs(item["canal"] - canal) * 5
        if joint == "hip":
            error += abs(item["offset"] - offset) * 1.5
        if quality == "low":
            error += 4
        score = max(0, round(100 - error))
        scores.append({"name": item["name"], "score": score,
                       "dimensions": {"diameter": item["diameter"], "canal": item["canal"], "offset": item["offset"]},
                       "deviations": {"diameter": round(abs(item["diameter"] - diameter), 2), "canal": round(abs(item["canal"] - canal), 2), "offset": round(abs(item["offset"] - offset), 2) if joint == "hip" else 0},
                       "reason": f"外径 {item['diameter']} mm；髓腔参考 {item['canal']} mm" +
                                 (f"；偏心距 {item['offset']} mm" if joint == "hip" else "")})
    scores.sort(key=lambda x: x["score"], reverse=True)
    flags = ["骨质偏低：固定方式与初期稳定性需重点复核"] if quality == "low" else []
    if scores[0]["score"] < 75:
        flags.append("最高匹配分低于 75：目录中可能没有合适规格")
    return {
        "title": "关节置换假体匹配建议",
        "summary": f"当前演示目录的首选为{scores[0]['name']}，几何匹配分 {scores[0]['score']} / 100。",
        "metrics": [{"label": "骨性尺寸", "value": f"{diameter:g} mm"},
                    {"label": "髓腔宽度", "value": f"{canal:g} mm"},
                    {"label": "首选评分", "value": str(scores[0]["score"]) }],
        "ranked_options": scores,
        "flags": flags or ["术中试模、软组织平衡与实际器械规格必须复核。"],
        "required_review": ["影像标定与骨性尺寸", "器械说明书及适应证", "术中试模与软组织平衡"],
        "method": "对演示器械目录计算加权绝对尺寸差；分数不是临床成功率。",
        "calculation": ["尺寸误差 = 外径差 × 3 + 髓腔差 × 5" + (" + 偏心距差 × 1.5" if joint == "hip" else ""),
                        "骨质偏低时，统一增加 4 分误差惩罚；评分 = max(0, round(100 − 误差))。",
                        "目录包含髋、膝各 5 个虚构规格；髓腔和偏心距为演示组合参数。"],
    }


def _trauma(m):
    site = _choice(m, "fracture_site", ("femur", "tibia", "radius"))
    pattern = _choice(m, "fracture_pattern", ("simple", "comminuted"))
    displacement = _number(m, "displacement_mm", 0, 50)
    articular = m.get("articular_involvement", False)
    if not isinstance(articular, bool):
        raise ValueError("articular_involvement must be a boolean")
    tissue = _choice(m, "soft_tissue", ("intact", "compromised"))
    scores = {"锁定钢板": 67, "髓内钉": 68, "外固定架": 42}
    if site == "radius":
        scores["锁定钢板"] += 15
        scores["髓内钉"] -= 25
    else:
        scores["髓内钉"] += 13
    if pattern == "comminuted":
        scores["锁定钢板"] += 5
        scores["髓内钉"] += 3
    if articular:
        scores["锁定钢板"] += 13
        scores["髓内钉"] -= 10
    if tissue == "compromised":
        scores["外固定架"] += 38
        scores["锁定钢板"] -= 16
        scores["髓内钉"] -= 10
    if displacement > 15:
        scores["外固定架"] += 6
    options = sorted(({"name": name, "score": min(99, max(0, score)),
                       "reason": {"锁定钢板": "适用于需要直接复位与角稳定讨论的场景。",
                                  "髓内钉": "适用于长骨髓腔内固定讨论的场景。",
                                  "外固定架": "可用于软组织条件受限时的阶段性稳定讨论。"}[name]}
                      for name, score in scores.items()), key=lambda x: x["score"], reverse=True)
    flags = []
    if articular:
        flags.append("关节面受累：需补充 CT 和关节面复位评估")
    if tissue == "compromised":
        flags.append("软组织受损：应优先完成清创与感染风险评估")
    if displacement > 15:
        flags.append("移位较大：需由创伤骨科复核复位策略")
    return {
        "title": "骨折内固定方案建议",
        "summary": f"规则评分首选{options[0]['name']}，得分 {options[0]['score']} / 100；仍需以复位目标和软组织条件为准。",
        "metrics": [{"label": "骨折部位", "value": {"femur": "股骨", "tibia": "胫骨", "radius": "桡骨"}[site]},
                    {"label": "移位", "value": f"{displacement:g} mm"},
                    {"label": "首选评分", "value": str(options[0]["score"]) }],
        "ranked_options": options,
        "flags": flags or ["需结合完整影像、血运神经状态和感染风险复核。"],
        "required_review": ["骨折分型与影像完整性", "软组织及血运神经状态", "复位与固定策略"],
        "method": "基于部位、粉碎程度、关节面、软组织与移位的可解释加权规则。",
        "calculation": ["基础分：锁定钢板 67，髓内钉 68，外固定架 42。",
                        "桡骨：钢板 +15、髓内钉 −25；其他长骨：髓内钉 +13。",
                        "粉碎：钢板 +5、髓内钉 +3；关节面受累：钢板 +13、髓内钉 −10。",
                        "软组织受损：外固定架 +38、钢板 −16、髓内钉 −10；移位 >15 mm：外固定架 +6。"],
    }
