"""上线评审判定（纯函数，确定性）：整体 + 必验渠道 + 护栏，同一口径。

口径（与算法 B 同源的双比例 z 检验，双侧）：
  p1 = 实验组转化率, p2 = 对照组转化率
  z  = (p1-p2)/sqrt(p_pool(1-p_pool)(1/n1+1/n2))，双侧 p = 2(1-Φ(|z|))
  direction：p < α 且 diff > 0 → up（显著上涨）
             p < α 且 diff < 0 → down（显著下滑）
             其余             → flat（不显著）

上线门禁（三条同时满足才可上线，缺一不可）：
  1. 整体（全渠道汇总的主指标）显著上涨 —— 整体不显著或显著下滑都不能上线；
  2. 所有必验渠道无一显著下滑 —— 任一必验渠道显著负向即拦截，
     其余渠道涨得再好也不参与抵扣（挑选表现好的渠道翻不了案）；
  3. 所有护栏指标无一显著下滑（护栏恶化即拦截）。

输入全部是预先聚合好的格子 {"n1","c1","n2","c2"}，不做任何 IO；
同样输入两次调用逐位一致。
"""
from __future__ import annotations

from src.services.algorithms import stats

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_FLAT = "flat"

VERDICT_SHIP = "ship"
VERDICT_HOLD = "hold"


def compare(variant_n: int, variant_conv: int, control_n: int, control_conv: int, alpha: float) -> dict:
    """单格双比例检验：返回率、差值、z、p 与方向。"""
    n1, c1 = int(variant_n), int(variant_conv)
    n2, c2 = int(control_n), int(control_conv)
    if n1 <= 0 or n2 <= 0:
        raise ValueError("对照与实验组样本量都必须大于 0")
    if not (0 <= c1 <= n1 and 0 <= c2 <= n2):
        raise ValueError("转化数必须落在 [0, n] 区间内")
    p1 = c1 / n1
    p2 = c2 / n2
    pooled = (c1 + c2) / (n1 + n2)
    variance = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    z = (p1 - p2) / variance**0.5 if variance > 0 else 0.0
    p_value = 2.0 * stats.norm_sf(abs(z)) if variance > 0 else 1.0
    diff = p1 - p2
    if p_value < alpha and diff > 0:
        direction = DIRECTION_UP
    elif p_value < alpha and diff < 0:
        direction = DIRECTION_DOWN
    else:
        direction = DIRECTION_FLAT
    return {
        "control_n": n2,
        "control_conversions": c2,
        "variant_n": n1,
        "variant_conversions": c1,
        "control_rate": stats.round_half_up(p2, 6),
        "variant_rate": stats.round_half_up(p1, 6),
        "diff": stats.round_half_up(diff, 6),
        "z": stats.round_half_up(z, 4),
        "p_value": p_value,
        "direction": direction,
    }


def _judge_cell(cell: dict, alpha: float) -> dict:
    result = compare(cell["n1"], cell["c1"], cell["n2"], cell["c2"], alpha)
    result["significant_negative"] = result["direction"] == DIRECTION_DOWN
    return result


def evaluate(
    *,
    overall: dict,
    channels: list[dict],
    guardrails: list[dict],
    alpha: float = 0.05,
) -> dict:
    """整体验证：overall 主指标格子 + 必验渠道格子 + 护栏格子 → 结论与阻塞项。

    channels / guardrails 的每一项都要带渠道 / 指标标识（channel / metric_key），
    评审范围完全由调用方（已登记的口径）决定，这里不做任何筛选。
    """
    if not 0 < alpha < 0.5:
        raise ValueError("alpha 必须在 (0, 0.5) 之间")
    overall_result = _judge_cell(overall, alpha)
    channel_results = []
    for cell in channels:
        result = _judge_cell(cell, alpha)
        result["channel"] = cell["channel"]
        channel_results.append(result)
    guardrail_results = []
    for cell in guardrails:
        result = _judge_cell(cell, alpha)
        result["metric_key"] = cell["metric_key"]
        guardrail_results.append(result)

    blockers: list[str] = []
    if overall_result["direction"] == DIRECTION_DOWN:
        blockers.append("overall:主指标整体显著下滑")
    elif overall_result["direction"] != DIRECTION_UP:
        blockers.append("overall:主指标整体未显著上涨")
    for result in channel_results:
        if result["significant_negative"]:
            blockers.append(f"channel:{result['channel']} 主指标显著下滑")
    for result in guardrail_results:
        if result["significant_negative"]:
            blockers.append(f"guardrail:{result['metric_key']} 护栏恶化")

    verdict = VERDICT_SHIP if not blockers else VERDICT_HOLD
    return {
        "alpha": alpha,
        "overall": overall_result,
        "channels": channel_results,
        "guardrails": guardrail_results,
        "verdict": verdict,
        "blockers": blockers,
    }
