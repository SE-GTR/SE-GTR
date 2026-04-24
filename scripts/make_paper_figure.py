"""
Generate a paper-style pipeline figure (PNG + PDF).
"""
from __future__ import annotations

from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, ArrowStyle, FancyArrowPatch
import matplotlib as mpl


def box(ax, x, y, w, h, title, lines, fc, ec="#2b2b2b", lw=1.2, title_size=12, body_size=10):
    rect = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02",
                          linewidth=lw, edgecolor=ec, facecolor=fc)
    ax.add_patch(rect)
    ax.text(x + 0.02*w, y + h - 0.22*h, title, fontsize=title_size, fontweight="bold", va="top")
    ax.text(x + 0.02*w, y + h - 0.38*h, "\n".join(lines), fontsize=body_size, va="top")
    return rect


def arrow(ax, x1, y1, x2, y2, color="#2b2b2b"):
    arr = FancyArrowPatch((x1, y1), (x2, y2),
                          arrowstyle=ArrowStyle("Simple", head_length=8, head_width=8, tail_width=1.2),
                          linewidth=1.2, color=color)
    ax.add_patch(arr)


def main(out_png: Path, out_pdf: Path):
    mpl.rcParams["font.size"] = 10
    fig = plt.figure(figsize=(14, 8))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Palette
    c_in = "#e8f0fe"
    c_tool = "#e8f5e9"
    c_llm = "#fff3e0"
    c_out = "#f3e5f5"
    c_ctrl = "#eceff1"

    ax.text(0.03, 0.96, "LLM-driven Smelly Smell Repair Pipeline (JUnit4 + Ant + EvoSuite)", fontsize=16, fontweight="bold")
    ax.text(0.03, 0.935, "Inputs: Smelly JSON + EvoSuite tests; Outputs: patched tests + before/after reports + logs", fontsize=11)

    # Inputs
    b1 = box(ax, 0.03, 0.73, 0.28, 0.18, "Dataset / Inputs", [
        "SF110 projects (Ant build.xml)",
        "EvoSuite tests: evosuite-tests/**",
        "Smell report: sf110_smelly.json"
    ], fc=c_in)

    b2 = box(ax, 0.03, 0.50, 0.28, 0.18, "Index & Select", [
        "Parse Smelly JSON (13 smells)",
        "Map key → project + CUT + test file",
        "Group smells per test method"
    ], fc=c_ctrl)

    b3 = box(ax, 0.03, 0.27, 0.28, 0.18, "Context Builder", [
        "Extract failing test method",
        "Resolve CUT FQCN & source",
        "Extract CUT + related methods"
    ], fc=c_ctrl)

    # Repair engine
    b4 = box(ax, 0.36, 0.73, 0.28, 0.18, "Deterministic Fixers (optional)", [
        "NNA: remove redundant assertNotNull",
        "DS: extract common setup → @Before",
        "Fast, conservative transforms"
    ], fc=c_tool)

    b5 = box(ax, 0.36, 0.50, 0.28, 0.18, "LLM Repair Loop", [
        "Prompt = smell guide + code context",
        "Return unified diff (single file)",
        "Retry with compile error feedback"
    ], fc=c_llm)

    b6 = box(ax, 0.36, 0.27, 0.28, 0.18, "Patch Guardrails", [
        "No test deletion",
        "No @Ignore / disabling",
        "Method must remain present"
    ], fc=c_ctrl)

    # Tooling / Verification
    b7 = box(ax, 0.69, 0.73, 0.28, 0.18, "Build & Run (Ant)", [
        "ant clean compile compile-evosuite",
        "(optional) run generated tests",
        "Collect compiler / runtime logs"
    ], fc=c_tool)

    b8 = box(ax, 0.69, 0.50, 0.28, 0.18, "Smelly Re-Run", [
        "Run Smelly on patched project",
        "Compute smell delta (before/after)",
        "Keep raw JSON snapshots"
    ], fc=c_tool)

    b9 = box(ax, 0.69, 0.27, 0.28, 0.18, "Outputs / Artifacts", [
        "Patched tests (workdir/)",
        "patches/*.diff + pipeline.jsonl",
        "reports/ summary + per-test logs"
    ], fc=c_out)

    # Arrows
    arrow(ax, 0.17, 0.73, 0.17, 0.68)
    arrow(ax, 0.17, 0.50, 0.17, 0.45)
    arrow(ax, 0.31, 0.59, 0.36, 0.82)
    arrow(ax, 0.31, 0.36, 0.36, 0.59)
    arrow(ax, 0.50, 0.73, 0.50, 0.68)
    arrow(ax, 0.50, 0.50, 0.50, 0.45)
    arrow(ax, 0.64, 0.59, 0.69, 0.82)
    arrow(ax, 0.64, 0.36, 0.69, 0.59)
    arrow(ax, 0.83, 0.73, 0.83, 0.68)
    arrow(ax, 0.83, 0.50, 0.83, 0.45)

    # Footer
    ax.text(0.03, 0.05, "Policy: do not force coverage/mutation constraints in prompt; measure post-repair.", fontsize=10)
    ax.text(0.03, 0.03, "LLM: OpenAI-compatible endpoint (e.g., vLLM serving Qwen3-Coder-30B-A3B-FP8 on DGX Spark).", fontsize=10)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=300, bbox_inches="tight")
    fig.savefig(out_pdf, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-png", type=Path, required=True)
    ap.add_argument("--out-pdf", type=Path, required=True)
    args = ap.parse_args()
    main(args.out_png, args.out_pdf)
