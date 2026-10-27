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


def phi(z):
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _dens(z):
    return math.exp(-z * z / 2) / math.sqrt(2 * math.pi)


def z_of_two_sided(a):
    """双侧显著水平 a 对应的 |z| 阈值（二分，精度 1e-10）。"""
    lo, hi = 0.0, 40.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 2 * (1 - phi(mid)) > a:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# α 消耗函数（Lan-DeMets）：t = 信息比例（0~1]，返回到 t 为止累计可花掉的双侧 α
SPEND = {
    "obf": lambda t, a: 2 - 2 * phi(z_of_two_sided(a) / math.sqrt(t)),       # O'Brien-Fleming 型：早期很严
    "pocock": lambda t, a: a * math.log(1 + (math.e - 1) * t),               # Pocock 型：各次阈值接近
}
_GRID = 401


def gs_bounds(fracs, alpha=0.05, spend="obf"):
    """成组序贯检验的双侧 |z| 阈值：fracs = 各次看数的信息比例（升序，最后一次通常 = 1）。
    第 k 次在前 k−1 次都没越线的条件下越线的概率 = 消耗函数在 t_k 与 t_{k−1} 处之差；
    Z_k 由 Z_{k−1} 加独立增量得到（Cov = √(t_j/t_k)），继续区间内的密度用网格数值积分递推。"""
    f = SPEND[spend]
    out, prev_t, prev_a, grid, dens = [], 0.0, 0.0, None, None
    for t in fracs:
        t = min(max(float(t), 1e-6), 1.0)
        if t <= prev_t:                                       # 同一信息量重复看：沿用上一次阈值
            out.append(out[-1] if out else z_of_two_sided(alpha))
            continue
        a_k = f(t, alpha) - prev_a
        if grid is None:
            c = z_of_two_sided(max(a_k, 1e-15))
        else:
            dt_ = t - prev_t
            s = math.sqrt(dt_ / t)
            h = grid[1] - grid[0]
            w = [d * h * (0.5 if i in (0, len(grid) - 1) else 1) for i, d in enumerate(dens)]
            mu = [u * math.sqrt(prev_t / t) for u in grid]

            def cross(c):
                return sum(wi * (1 - phi((c - m) / s) + phi((-c - m) / s)) for wi, m in zip(w, mu))
            lo, hi = 0.0, 40.0
            for _ in range(80):
                mid = (lo + hi) / 2
                if cross(mid) > a_k:
                    lo = mid
                else:
                    hi = mid
            c = (lo + hi) / 2
        out.append(c)
        # 继续区间（|Z_k| < c）上的密度
        new = [-c + 2 * c * i / (_GRID - 1) for i in range(_GRID)]
        if grid is None:
            nd = [_dens(z) for z in new]
        else:
            nd = [sum(wi * _dens((z - m) / s) / s for wi, m in zip(w, mu)) for z in new]
        grid, dens, prev_t, prev_a = new, nd, t, f(t, alpha)
    return out


def bh_pass(ps, level):
    """Benjamini-Hochberg：ps = p 值列表（None 不参与），返回同序的是否通过（FDR 控制在 level）。"""
    idx = sorted((p, i) for i, p in enumerate(ps) if p is not None)
    m, k = len(idx), 0
    for r, (p, _) in enumerate(idx, 1):
        if p <= r / m * level:
            k = r
    ok = {i for _, i in idx[:k]}
    return [i in ok for i in range(len(ps))]


def _ratio_grad_cov(n, s, u, v, w, z):
    """两个比率 U = Σu/Σv、W = Σw/Σz（同一批设备）的 delta method 协方差 Cov(U, W)。s = 充分统计量取值函数。"""
    mu, mv, mw, mz = s(u) / n, s(v) / n, s(w) / n, s(z) / n
    if not mv or not mz:
        return None
    c = lambda a, b: s(a, b) / n - (s(a) / n) * (s(b) / n)
    gu, gv, gw, gz = 1 / mv, -mu / mv ** 2, 1 / mz, -mw / mz ** 2
    return (gu * gw * c(u, w) + gu * gz * c(u, z) + gv * gw * c(v, w) + gv * gz * c(v, z)) / n


def cuped_group(st):
    """单组 CUPED 原料：st = n + Σ / Σ两两乘积（x, y = 实验期分子 / 分母；a, b = 上线前同口径分子 / 分母）。
    返回 (Y, X, Var Y, Var X, Cov(Y, X))；上线前分母全为 0 → None。"""
    n = st.get("n")
    if not n or not st.get("sy") or not st.get("sb"):
        return None

    def s(a, b=None):
        if b is None:
            return st["s" + a]
        return st.get("s" + a + b, st.get("s" + b + a))
    Y, X = st["sx"] / st["sy"], st["sa"] / st["sb"]
    return (Y, X, max(_ratio_grad_cov(n, s, "x", "y", "x", "y"), 0.0), max(_ratio_grad_cov(n, s, "a", "b", "a", "b"), 0.0),
            _ratio_grad_cov(n, s, "x", "y", "a", "b"))


def cuped_compare(c, t, alpha=0.05, power=0.8):
    """c / t = cuped_group 结果。θ 两组合并估计；返回调整后的差、se、z 与方差降幅；与 compare 同口径（绝对差）。"""
    if not c or not t:
        return None
    vx = c[3] + t[3]
    v0 = c[2] + t[2]
    if vx <= 0 or v0 <= 0:
        return None
    cov = c[4] + t[4]
    theta = cov / vx
    d = (t[0] - c[0]) - theta * (t[1] - c[1])
    var = max(v0 - cov * cov / vx, 1e-300)
    se = math.sqrt(var)
    za, zb = _z(1 - alpha / 2), _z(power)
    out = {"diff": d, "se": se, "z": d / se, "p": _p_two_sided(d / se), "theta": theta,
           "ci_low": d - za * se, "ci_high": d + za * se, "var_cut": 1 - var / v0, "mde_abs": (za + zb) * se}
    raw_d, raw_se = t[0] - c[0], math.sqrt(v0)
    out.update(raw_diff=raw_d, raw_se=raw_se, raw_z=raw_d / raw_se)
    if c[0]:
        out["rel"] = d / c[0]
        out["raw_rel"] = raw_d / c[0]
    return out


def heterogeneity(effects):
    """Cochran Q：effects = [(Δ, se)]，检验各分群效应是否一致。返回 (Q, df, p)；不足 2 群 → None。"""
    xs = [(d, 1 / se ** 2) for d, se in effects if se and se > 0]
    if len(xs) < 2:
        return None
    sw = sum(w for _, w in xs)
    mean = sum(d * w for d, w in xs) / sw
    q = sum(w * (d - mean) ** 2 for d, w in xs)
    df = len(xs) - 1
    return q, df, _gammq(df / 2.0, q / 2.0)


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
