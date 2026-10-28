# Documentation index

[Current status](status.md) is the operational summary. [Setup](setup.md) is
the reproduction entry point. The documents below preserve the evidence and
history behind the implementation; earlier “pending” statements describe the
stage/date at which they were written, not necessarily current support.

## Current operation

| Document | Purpose |
| --- | --- |
| [Setup](setup.md) | Inputs, environments, offline builds and explicit hardware qualification |
| [Status](status.md) | Verified results, pending checks and announcement scope |
| [cuDLA backend](cudla-backend.md) | Implemented API and physical eight-job result |
| [Shutdown/reload](shutdown-reload.md) | Cleanup contract, fault tests and pending hardware cycle |
| [Super-resolution demo](../demo/README.md) | Image pipeline, usage and qualification limits |
| [Tool index](../scripts/README.md) | Which commands to use and which alter hardware/boot state |

## Implementation and research evidence

- API and runtime: [cuDLA call path](cudla-call-path.md),
  [DLA integration design](dla-integration-design.md),
  [native runtime](native-runtime.md), [loadable runtime](loadable-runtime.md),
  [Windows submissions](windows-submission.md),
  [KMD task protocol](windows-kmd-task.md), [buffer lifetime](windows-buffer-lifetime.md).
- Firmware and board: [board policy/startup](p16-board-start.md),
  [ACPI audit](acpi-audit.md), [Linux backend](linux-backend.md),
  [HFRP protocol](hfrp-irq.md), [VBIOS policy](hfrp-vbios.md),
  [SROOT transport](sroot-transport.md), [envelopes](sroot-envelope.md),
  [Spark OSROOT comparison](spark-osroot-research.md).
- Models: [compiler probe](model-probe.md), [convolution compiler](convolution-compiler.md),
  [demo research](github-demo-research.md), [bypass task](bypass-task.md).
- Records/configuration: [task contract](contract.md), [default config](default-config.md),
  [diagnostic contract](diagnostic-contract.md), [statistics](statistics-v2.md),
  [region 5](region5.md), [producer](region5-producer.md), [history](region5-history.md).

## Historical boot/capture workflows

[Boot logging](boot-logging.md) and [npu2 FRTS capture](npu2-frts-capture.md)
document earlier kernel/UKI/capture experiments. They remain useful to reproduce
specific investigations and recovery decisions. Their install/build tools are
not steps in ordinary model execution. Follow their exact input/release guards;
do not run every script in sequence as an installation procedure.

Tracked numerical receipts identify exact test artifacts. Full local logs,
vendor bytes, ROM captures and generated images remain in ignored `results/`
and `sources/`. Historical failures are preserved rather than rewritten as
successes; newer results are summarized in [status](status.md).
