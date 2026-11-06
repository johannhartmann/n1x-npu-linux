# Shared SROOT DSP clients

The kernel-only interface in
[`n1x_sroot_client.h`](../include/n1x_sroot_client.h) serializes DSP image
uploads with NPU bootstrap under `boot_lock`. It accepts MT8901 signed containers with supported image IDs and valid section
bounds, and permits one published attempt per image ID. SROOT authenticates the
original certificate/signatures; the host does not pin firmware file hashes.
It does not expose raw SROOT MMIO or a userspace upload interface.

ABI v1 retains the temporary image allocation and its provider module/runtime-PM
references until reboot. ABI v2 separates that allocation from the published
DSP lifetime lease. A completed v2 exchange must have raw reply header exactly
`0x18`, command `0x18`, no payload words and zero status/detail. Only then is
the temporary image freed and its CPU/DMA addresses cleared. The published
lease flag and both references remain held; NPU shutdown still returns `EBUSY`
before changing accelerator state, including when every staging buffer was freed.

Any rejected, malformed or uncertain submitted exchange poisons the shared
loader and retains its staging memory and references. Neither ABI can retry
a published image or release an earlier instance. Calling v2 after a v1 upload
does not retire that v1 allocation. The DSP client separately owns its operating
DRAM, AFE SRAM, task state and power votes. Freeing its image staging does not
stop a DSP, establish whole-core DMA quiescence or release the provider lease.

The native reference is `nvsrootmbx.sys`, SHA256
`f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a`.
Its upload routine `0x140031c98` calls allocation `0x140003b08`, image copy
`0x1400039b0`, the synchronous exchange `0x140005580`, then cleanup
`0x140003e00`. Cleanup dispatches through DMA operation-table offset `0x18`,
the ARM64 slot for Microsoft's
[`FreeCommonBuffer`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ns-wdm-_dma_operations).
The native error cleanup is not adopted: Linux retains uncertain allocations.
The pinned instruction extractor is `scripts/analyze-dsp-shutdown.py` in the
P16 platform repository. Windows code is never executed by these checks.

`make check` runs actual-function tests for both ABIs, all prepublication and
exchange failures, noncanonical replies, all three successful uploads and
eight concurrent same-image callers. The shutdown tests independently check
that a published lease with no CPU/DMA allocation still prevents shutdown.
ABI v2 is an uninstalled candidate and has no hardware release qualification.
Verified DSP core shutdown, operating-memory/provider-lease release and a
fresh-boot hardware trial remain required.
