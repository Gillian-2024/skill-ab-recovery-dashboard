"""实验统计：比率指标 delta method（按设备聚类）、SRM 卡方、MDE。

输入是每组一行的充分统计量：n, Σx, Σy, Σx², Σy², Σxy（单位 = 设备）。
"""
import math



def ratio_stats(n, sx, sy, sxx, syy, sxy):
    """返回 (R, Var(R))；R = Σx/Σy。"""
    if not n or not sy:
        return None, None
    mx, my = sx / n, sy / n
    vx = max(sxx / n - mx * mx, 0.0)
    vy = max(syy / n - my * my, 0.0)
    cxy = sxy / n - mx * my
    r = mx / my
    var = (vx - 2 * r * cxy + r * r * vy) / (n * my * my)
    return r, max(var, 0.0)


def _p_two_sided(z):
    return math.erfc(abs(z) / math.sqrt(2))


def _z(q):
    """标准正态分位数（Acklam 有理逼近，误差 < 1e-9），免依赖 scipy。"""
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    if q < 0.02425:
        t = math.sqrt(-2 * math.log(q))
        return (((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / ((((d[0] * t + d[1]) * t + d[2]) * t + d[3]) * t + 1)
    if q > 1 - 0.02425:
        return -_z(1 - q)
    t = q - 0.5
    r = t * t
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * t / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def compare(ctrl, trt, alpha=0.05, power=0.8):
    """ctrl / trt 为 (R, Var)；返回绝对差、相对差及其 CI、p、MDE（双侧 alpha，给定 power）。"""
    za, zb = _z(1 - alpha / 2), _z(power)
    rc, vc = ctrl
    rt, vt = trt
    if rc is None or rt is None:
        return None
    d = rt - rc
    se = math.sqrt(vc + vt)
    out = {"diff": d, "se": se,
           "ci_low": d - za * se, "ci_high": d + za * se,
           "p": _p_two_sided(d / se) if se > 0 else None,
           "mde_abs": (za + zb) * se}
    if rc:
        rel = rt / rc - 1
        se_rel = math.sqrt(vt / rc ** 2 + rt ** 2 * vc / rc ** 4)
        out.update(rel=rel, rel_ci_low=rel - za * se_rel, rel_ci_high=rel + za * se_rel,
                   mde_rel=(za + zb) * se_rel)
    if out["p"] is None:
        out["verdict"] = "无方差"
    elif out["p"] < alpha:
        out["verdict"] = "显著上升" if d > 0 else "显著下降"
    else:
        out["verdict"] = "不可判定（|Δ| < MDE）" if abs(d) < out["mde_abs"] else "不显著"
    return out


def _gammq(a, x):
    """正则化上不完全 Gamma Q(a, x)，用于卡方生存函数。"""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        ap, s, delta = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1
            delta *= x / ap
            s += delta
            if abs(delta) < abs(s) * 1e-12:
                break
        return 1.0 - s * math.exp(-x + a * math.log(x) - gln)
    b = x + 1 - a
    c, d = 1e300, 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1) < 1e-12:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def srm(counts, weights=None):
    """各组设备数的卡方检验（默认等比分流）。返回 (chi2, p)。"""
    k = len(counts)
    tot = sum(counts)
    if k < 2 or tot == 0:
        return None, None
    w = weights or [1.0] * k
    sw = sum(w)
    chi = sum((o - tot * wi / sw) ** 2 / (tot * wi / sw) for o, wi in zip(counts, w))
    return chi, _gammq((k - 1) / 2.0, chi / 2.0)
