# Qualification evidence

Receipts identify exact artifacts and the boundary between original-code
emulation, modeled I/O and physical hardware. Existing receipt contents are
preserved when the repository is reorganized. New measurements receive new
records; [current status](../status.md) summarizes the supported behavior.

| Receipt | Scope |
| --- | --- |
| [Current physical NPU qualification](npu-hardware-2026-10-28.json) | 48 fixture jobs, two healthy reload cycles and two complete super-resolution image runs |
| [Vision examples](vision-hardware-2026-10-29.json) | Six denoising/classification image runs, 657 physical application submissions, batch reuse and three healthy stops/startups |
| [Vision compiler reproduction](vision-reproduction-2026-10-29.json) | Fresh outputs for all 15 pinned vision loadables; existing super-resolution artifact unchanged |
| [First cuDLA hardware qualification](cudla-hardware-2026-10-28.json) | Eight independent numerical jobs with the earlier retention-only driver |
| [First super-resolution window](superres-tile-2026-10-28.json) | One physical 64x64 window; not a complete image |
| [Offline model reproduction](offline-reproduction-2026-10-28.json) | Clean source export reconstructed all four pinned loadables |
| [First reload attempt](reload-attempt-2026-10-28.json) | Startup and inference passed; host mask assertion stopped shutdown before power-off |
| [Lifecycle review](lifecycle-review-2026-10-28.json) | Original-code comparisons, confirmation fix and joined shutdown/transport tests before requalification |

Complete local logs and generated outputs remain in ignored `results/`.
The published [demo images](../../demo/assets/README.md) retain the hashes from
the current physical receipt. Two healthy reload cycles establish a bounded
qualification on one board, not long-term reliability or uncertain-failure recovery.
