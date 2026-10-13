"""With/without stats for each chart-pattern flag over the setup library."""
from __future__ import annotations

import math
import random
import sys

from hl_bot.research.setup_search import build_library, write


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """2x2 [[a,b],[c,d]] two-sided Fisher exact p."""
    n = a + b + c + d
    r1, c1 = a + b, a + c

    def p(x):
        return math.comb(c1, x) * math.comb(n - c1, r1 - x) / math.comb(n, r1)

    obs = p(a)
    lo, hi = max(0, r1 - (n - c1)), min(r1, c1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= obs * (1 + 1e-9)))


def boot_diff(xs, ys, reps=4000, seed=7):
    if not xs or not ys:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    out = []
    for _ in range(reps):
        a = [rnd.choice(xs) for _ in xs]
        b = [rnd.choice(ys) for _ in ys]
        out.append(sum(a) / len(a) - sum(b) / len(b))
    out.sort()
    return out[int(0.025 * reps)], out[int(0.975 * reps)]


FLAGS = ["dt", "hs", "dt_at", "hs_at", "dt_neck", "hs_neck"]


def study(lib) -> list[dict]:
    done = [s for s in lib if s.win is not None and s.r is not None]
    rows = []
    for setting in ("A", "B"):
        for flag in FLAGS + ["any", "any_at", "any_neck"]:
            def has(s):
                f = s.f
                if flag == "any":
                    return f[f"dt_{setting}"] or f[f"hs_{setting}"]
                if flag == "any_at":
                    return f[f"dt_at_{setting}"] or f[f"hs_at_{setting}"]
                if flag == "any_neck":
                    return f[f"dt_neck_{setting}"] or f[f"hs_neck_{setting}"]
                return f[f"{flag}_{setting}"]
            w = [s for s in done if has(s)]
            wo = [s for s in done if not has(s)]
            a, b = sum(s.win for s in w), len(w) - sum(s.win for s in w)
            c, d = sum(s.win for s in wo), len(wo) - sum(s.win for s in wo)
            ci = boot_diff([s.r for s in w], [s.r for s in wo])
            rows.append({
                "setting": setting, "flag": flag, "n_with": len(w), "n_without": len(wo),
                "win_with": a / len(w) if w else float("nan"), "win_without": c / len(wo) if wo else float("nan"),
                "avg_r_with": sum(s.r for s in w) / len(w) if w else float("nan"),
                "avg_r_without": sum(s.r for s in wo) / len(wo) if wo else float("nan"),
                "fisher_p": fisher_two_sided(a, b, c, d) if w and wo else float("nan"),
                "r_diff_ci": ci,
            })
    return rows


def main(argv=None) -> int:
    lib = build_library()
    rows = study(lib)
    md = ["| setting | pattern | n with | n without | +2R with | +2R without | avg R with | avg R without | Fisher p | avg R diff 95% CI |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['setting']} | {r['flag']} | {r['n_with']} | {r['n_without']} | {r['win_with']:.0%} | "
                  f"{r['win_without']:.0%} | {r['avg_r_with']:+.2f} | {r['avg_r_without']:+.2f} | {r['fisher_p']:.2f} | "
                  f"[{r['r_diff_ci'][0]:+.2f}, {r['r_diff_ci'][1]:+.2f}] |")
    text = "\n".join(md)
    write("patterns", text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
