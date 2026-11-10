# Shared SROOT DSP clients

The kernel-only interface in
[`n1x_sroot_client.h`](../include/n1x_sroot_client.h) serializes DSP image
uploads with NPU bootstrap under the selected provider context mutex. It accepts MT8901 signed containers with supported image IDs and valid section
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

## Device-bound ABI v3

`n1x_sroot_client_ops_v3()` supplies a probe-time referenced handle. The caller
holds the exported symbol while using its operation table and serializes each
handle. `get()` matches the consumer's firmware parent against registered
SROOT contexts and rejects ambiguity. It references the context, consumer,
DLA supplier and module and creates a stateless DSP → DLA PM link. Unavailable
or uninitialized providers defer probe; no firmware is started by lookup.

`prepare()` checks loader health and reserves the provider before DSP power or
DRAM allocation. Reserved clients block checked NPU stop and system-PM prepare.
`publish()` makes the reservation irrevocable before the consumer writes a DSP
DMA address. `cancel()` unwinds only an unpublished reservation. `put()` frees
an unpublished handle and its link/references, or returns `EBUSY` while retaining
a published handle. `upload()` uses the handle's exact context and ABI v2's
staging rules. A failed upload cannot undo an earlier DSP address publication.

The captured firmware has no `_DEP`/`_DSD` association for these devices. Unique
siblings are therefore a checked platform fallback, not a universal firmware
binding. Whole-core DSP shutdown remains unverified. ABI v3 does not make
published DSPs removable or suspendable, and has not yet been loaded for a
fresh-boot hardware trial.

## Retained system sleep (ABI v4 candidate)

`n1x_sroot_client_ops_v4()` adds a separate sleep operation table; v1/v2/v3
interfaces and their fail-closed stop behavior remain intact. Its client
`get()` marks a retention-aware handle. Reservations count both total and
retention-aware clients. Successful v4 uploads mark only canonically completed,
staging-released images as retention-capable. Legacy or uncertain publications
continue to block system sleep in `prepare`.

Consumers freeze new work in PM `prepare`. After an acknowledged DSP D3
transition, IRQ synchronization and power-worker draining, each calls
`suspend()`. Duplicate votes fail. The provider's prepare also requires a canonical completed upload for every
reservation, so in-flight boot/publication cannot be admitted as retained idle.
The provider's suspend requires every
reservation to have voted, then uses checked DLA stop without dropping the
shared publication reference or DSP buffers/leases. Ordinary shutdown/unload
still returns `EBUSY`, including while all clients are asleep.

DLA restarts before consumers resume through their device links. Its validated
CPU firmware image is retained in the device context until final release, so
resume never needs to read a firmware file from the suspended filesystem. Startup failure
leaves the provider suspended; consumers cannot clear their votes or report a
working supplier. After acknowledged D0 and retained-state checks, each calls
`resume()`, which clears its vote without releasing references. An abort before
DLA suspension can use the same vote restoration. No operation here releases
DSP operating memory, repairs poisoned firmware, or allows image reload.

The matching platform DSP driver limits this to ASoC-ordered DSP s2idle with retained AFE SRAM and defaults
its experimental `enable_retention_sleep` option off. Legacy active clients without ASoC ordering, Hibernate and deep sleep remain
unsupported. Runtime autosuspend and full DSP release are still unavailable. SET acknowledgement and
matching SRAM descriptors alone do not prove firmware liveness or safe DMA
reclamation. Hardware sleep/audio/NPU qualification is still pending; this is
not a claim that published DSP suspend now works on the running machine.
