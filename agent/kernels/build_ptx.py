"""Compile agent/kernels/*.ll to PTX with LLVM's NVPTX backend (via llvmlite).

The generated PTX is committed in agent/sharedcompute_agent/kernels/ so provider machines need only
the NVIDIA driver, which JIT-compiles PTX for the installed GPU. Target sm_30 (Kepler) and PTX ISA 3.2
(CUDA 5.5) keep old GPUs and old drivers working; newer GPUs JIT the same PTX forward.

Usage (any OS, no GPU needed):
    pip install "llvmlite==0.43.0"      # bundles LLVM 14 with NVPTX
    python agent/kernels/build_ptx.py           # rewrite the committed PTX
    python agent/kernels/build_ptx.py --check   # exit 1 if the committed PTX is stale
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "sharedcompute_agent" / "kernels"
TARGET_CPU = "sm_30"


def compile_ll(path: Path) -> str:
    import llvmlite.binding as llvm

    llvm.initialize()
    llvm.initialize_all_targets()
    llvm.initialize_all_asmprinters()
    module = llvm.parse_assembly(path.read_text())
    module.verify()
    machine = llvm.Target.from_triple("nvptx64-nvidia-cuda").create_target_machine(cpu=TARGET_CPU, opt=2)
    header = f"// Generated from agent/kernels/{path.name} by agent/kernels/build_ptx.py. Do not edit.\n"
    return header + machine.emit_assembly(module)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for source in sorted(HERE.glob("*.ll")):
        ptx = compile_ll(source)
        target = OUT / (source.stem + ".ptx")
        if check:
            if not target.exists() or target.read_text() != ptx:
                stale.append(target.name)
        else:
            OUT.mkdir(exist_ok=True)
            target.write_text(ptx)
            print(f"wrote {target}")
    if stale:
        print(f"Stale PTX: {', '.join(stale)}. Run python agent/kernels/build_ptx.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
