# Research archive

These reports document how the implementation was recovered and tested. They
include dated assumptions, failed experiments and scripts tied to earlier
kernel builds. Use [the current support summary](../status.md) and
[setup guide](../setup.md) for operation. A historical “not yet tested” statement
is not the current project status.

## Runtime and model compilation

- [cuDLA call path](cudla-call-path.md), [Windows runtime submission](windows-submission.md)
- [DLA integration design](dla-integration-design.md), [native runtime](native-runtime.md), [loadable runtime](loadable-runtime.md)
- [Compiler and model probe](model-probe.md), [convolution compiler](convolution-compiler.md), [super-resolution model research](github-demo-research.md)
- [KMD task protocol](windows-kmd-task.md), [buffer lifetime](windows-buffer-lifetime.md), [bypass task](bypass-task.md)

## Firmware and hardware contracts

- [Verified contract](contract.md), [ACPI audit](acpi-audit.md), [Linux bring-up](linux-backend.md)
- [HFRP protocol](hfrp-irq.md), [VBIOS policy](hfrp-vbios.md), [board startup and ROM acquisition](p16-board-start.md)
- [SROOT transport](sroot-transport.md), [signed envelope](sroot-envelope.md), [Spark OSROOT comparison](spark-osroot-research.md)
- [Default configuration](default-config.md), [diagnostic fields](diagnostic-contract.md), [statistics](statistics-v2.md)
- [Region 5](region5.md), [producer](region5-producer.md), [history](region5-history.md)

## Historical boot diagnostics

[Boot logging](boot-logging.md) and [FRTS capture](npu2-frts-capture.md) explain
isolated diagnostic kernels, capture attempts and their recovery procedures.
Their install/build commands are not steps in ordinary inference startup.

The [tool index](../../scripts/README.md) explains the corresponding script
families and prerequisites. Original vendor binaries, decompiler output,
private captures and full run logs remain outside Git.
