# Documentation

## Use the NPU

1. [Setup and reproduction](setup.md): platform, dependencies, pinned vendor inputs, model compilation and startup.
2. [Super-resolution demo](../demo/README.md): run a complete image and inspect its output and reference metrics.
3. [Support status](status.md): measured behavior and remaining limits.

Additional applications: [denoising and classification](../demo/vision.md).

## Understand the implementation

- [cuDLA backend](cudla-backend.md): implemented API, CUDA staging, ownership and allocation behavior.
- [Shutdown and reload](shutdown-reload.md): firmware confirmation, lifetime ordering and hardware qualification.
- [Tool index](../scripts/README.md): operational commands, tests and research tools.
- [Contributing](../CONTRIBUTING.md): development workflow and verification by change type.

## Examine the evidence

- [Measurement receipts](evidence/README.md): exact hashes and physical/offline outcomes.
- [Published demo images](../demo/assets/README.md): unmodified sample and actual DLA output.
- [Research archive](research/README.md): original driver/firmware analysis, compiler bridge and historical boot experiments.

Research reports preserve the evidence available at their stated date. Follow
the maintained guides above for current operation; historical boot commands are
not a general installation sequence.
