"""Which devices the provider has agreed to share. Nothing is offered unless chosen explicitly.

* GPUs are offered only when selected with --gpu (index or UUID prefix).
* A GPU that drives a display is refused even when selected, unless --allow-display-gpu is given,
  so a provider's main GPU is never used by accident.
* The CPU is offered only with --offer-cpu.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .workloads import device_workloads


@dataclass
class OfferPolicy:
    gpus: Tuple[str, ...] = ()
    offer_cpu: bool = False
    allow_display_gpu: bool = False

    def selects(self, device) -> bool:
        for sel in self.gpus:
            sel = sel.strip()
            if sel.isdigit() and int(sel) == device.slot:
                return True
            if not sel.isdigit() and device.uuid and device.uuid.lower().startswith(sel.lower()):
                return True
        return False

    def apply(self, devices: list) -> List[str]:
        """Set offered/offer_note/workloads on each device. Returns warnings to show the provider."""
        warnings = []
        gpus = [d for d in devices if d.device_type == "gpu"]
        for sel in self.gpus:
            if not any(self.selects_one(sel, d) for d in gpus):
                warnings.append(f"--gpu {sel} does not match any detected GPU")
        for d in devices:
            d.workloads, reasons = device_workloads(d)
            if d.device_type == "cpu":
                d.offered = self.offer_cpu
                d.offer_note = None if d.offered else "Not offered. Start the agent with --offer-cpu to run CPU workloads."
                continue
            if not self.selects(d):
                d.offered = False
                d.offer_note = f"Not offered. Start the agent with --gpu {d.slot} to offer it."
            elif d.display_active and not self.allow_display_gpu:
                d.offered = False
                d.offer_note = ("Drives a display, so it is not used. Add --allow-display-gpu to offer it anyway "
                                "(jobs may then slow or freeze the desktop).")
                warnings.append(f"GPU {d.slot} ({d.model}) drives a display and was not offered")
            elif not d.workloads:
                d.offered = False
                d.offer_note = "Selected but cannot run any workload: " + "; ".join(reasons)
                warnings.append(f"GPU {d.slot} ({d.model}) cannot run any workload: {'; '.join(reasons)}")
            else:
                d.offered = True
                d.offer_note = None
        return warnings

    def selects_one(self, sel: str, device) -> bool:
        return OfferPolicy(gpus=(sel,)).selects(device)
