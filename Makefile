CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
# Explicit installed-kernel target; builds only, never installs/loads modules.
KERNEL_RELEASE ?= $(shell uname -r)
PYTHON ?= venv/bin/python
.DEFAULT_GOAL := help
.PHONY: help check check-repo
help:
	@echo 'make test            C-only portable firmware/parser checks'
	@echo 'make check           Repository consistency and portable protocol/lifetime tests'
	@echo 'make check-repo      Documentation links, Python syntax, JSON and demo provenance'
	@echo 'make test-portable   Portable tests; analysis Python environment required'
	@echo 'make runtime cudla   Build userspace; pinned cuDLA package and CUDA headers required'
	@echo 'make module          Build kernel modules against KERNEL_RELEASE; never install/load'
	@echo 'make test-p16-start  Vendor/board-fixture-backed startup checks; no live NPU'
	@echo 'make test-p16-stop   Vendor-backed shutdown comparisons; no live NPU'
	@echo 'make test-cudla      Backend/API checks; real CUDA, modeled NPU transport'
	@echo 'make clean           Remove generated userspace build outputs only'
	@echo 'See CONTRIBUTING.md and scripts/README.md for prerequisites and research suites.'

check: check-repo test-portable
check-repo:
	$(PYTHON) scripts/check_repo.py
	$(PYTHON) scripts/test_check_repo.py

.PHONY: test test-vendor test-relu test-conv test-conv-relu test-p16-start kernel-check module clean generate-probe
.PHONY: test-p16-stop
.PHONY: test-portable
# Requires the exact ignored physical BIOS-300 capture and pinned vendor KMD.
# Offline: no root, module load, hardware access or firmware installation.
test-p16-start:
	$(PYTHON) scripts/verify_p16_board_policy.py results/p16-bios300-physical.rom
	$(PYTHON) scripts/test_linux_hfrp_prepare.py
	$(PYTHON) scripts/test_linux_hfrp_state.py
	$(PYTHON) scripts/test_linux_fence_order.py
	$(PYTHON) scripts/test_linux_bootstrap.py
	$(PYTHON) scripts/test_linux_bootstrap_guards.py
	$(PYTHON) scripts/test_hfrp_setup.py
	$(PYTHON) scripts/test_npu_dma_domains.py
	$(PYTHON) scripts/test_qualify_p16_start.py
	$(PYTHON) scripts/test_linux_lpwr_config.py
	$(PYTHON) scripts/test_native_loadable.py
	$(PYTHON) scripts/test_dla_runtime.py
	$(PYTHON) scripts/test_linux_accel_backend.py
	$(PYTHON) scripts/test_linux_accel_open.py
	$(PYTHON) scripts/test_linux_accel.py
	$(PYTHON) scripts/test_linux_bypass_task.py
	$(PYTHON) scripts/test_linux_bypass_firmware.py
	$(PYTHON) scripts/test_response.py
	$(PYTHON) scripts/test_response_decode.py
	$(PYTHON) scripts/test_profile1_lpwr_consumer.py
test-p16-stop:
	$(PYTHON) scripts/inspect_kmd_shutdown.py
	$(PYTHON) scripts/test_hfrp_stop.py
	$(PYTHON) scripts/test_linux_shutdown.py
	$(PYTHON) scripts/test_shutdown_transport.py
	$(PYTHON) scripts/test_linux_accel.py
	$(PYTHON) scripts/test_linux_accel_backend.py
	$(PYTHON) scripts/test_p16_reload.py
test-portable: test
	$(PYTHON) scripts/test_prepare_inputs.py
	$(PYTHON) scripts/test_hfrp_channel.py
	$(PYTHON) scripts/test_linux_shutdown.py
	$(PYTHON) scripts/test_shutdown_transport.py
	$(PYTHON) scripts/test_linux_accel_backend.py
	$(PYTHON) scripts/test_p16_reload.py
	$(PYTHON) scripts/test_npu_trigger.py
	$(PYTHON) scripts/test_qualify_p16_start.py
# Device-free convolution compiler bridge; preserves the pinned ReLU directory.
test-conv:
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1 --target t254 --capture-mtl
	$(PYTHON) scripts/inspect_conv_pretranslation.py
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1 --target t254 --capture-translation-input
	$(PYTHON) scripts/inspect_conv_pretranslation.py --translate
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1 --target t254 --package-windows-output
	$(PYTHON) scripts/verify_conv_loadable.py
	$(PYTHON) scripts/inspect_conv_memory_allocation.py
	$(PYTHON) scripts/test_linux_accel.py --conv
	$(PYTHON) scripts/create_conv_probe.py
	$(PYTHON) scripts/test_conv_acceptance.py
	$(PYTHON) scripts/inspect_fp19_conversion.py

# Two source layers, including their local DRAM intermediate; offline only.
test-conv-relu:
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1_relu
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1_relu --target t254 --capture-mtl
	$(PYTHON) scripts/inspect_conv_pretranslation.py --model conv1x1_relu
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1_relu --target t254 --capture-translation-input
	$(PYTHON) scripts/inspect_conv_pretranslation.py --model conv1x1_relu --translate
	$(PYTHON) scripts/probe_native_graph.py --model conv1x1_relu --target t254 --package-windows-output
	$(PYTHON) scripts/verify_conv_loadable.py --model conv1x1_relu
	$(PYTHON) scripts/inspect_conv_memory_allocation.py --model conv1x1_relu
	$(PYTHON) scripts/test_linux_accel.py --conv-relu
	$(PYTHON) scripts/create_conv_probe.py --relu
	$(PYTHON) scripts/test_conv_acceptance.py --relu

generate-probe:
	$(PYTHON) scripts/create_relu_probe.py
# Requires the generated ReLU source, checked Windows outputs and serialized
# artifact from docs/model-probe.md. Offline only; no module or device access.
test-relu:
	$(PYTHON) scripts/verify_relu_loadable.py
	$(PYTHON) scripts/inspect_loadable_version_gate.py --relu
	$(PYTHON) scripts/inspect_content_materialization.py --relu
	$(PYTHON) scripts/inspect_full_deserialization.py
	$(PYTHON) scripts/inspect_loadable_extensions.py
	$(PYTHON) scripts/inspect_memory_initialization.py
	$(PYTHON) scripts/test_statistics_v2.py
	$(PYTHON) scripts/test_model_addresses.py
	$(PYTHON) scripts/test_model_contents.py
	$(PYTHON) scripts/test_relu_scheduler.py
tests/test_firmware: tests/test_firmware.c driver/n1x_dla_firmware.c include/n1x_dla_firmware.h
	$(CC) $(CFLAGS) -Iinclude tests/test_firmware.c driver/n1x_dla_firmware.c -o $@
test: tests/test_firmware
	./tests/test_firmware
# Requires the exact ignored vendor inputs, analysis venv and generated ReLU
# artifacts described in docs/model-probe.md.
test-vendor: test
	$(PYTHON) scripts/inspect_windows_submission.py
	$(PYTHON) scripts/inspect_runtime_command.py
	$(PYTHON) scripts/inspect_kmd_command_descriptor.py
	$(PYTHON) scripts/inspect_kmd_task_submission.py
	$(PYTHON) scripts/inspect_kmd_virtual_submit.py
	$(PYTHON) scripts/inspect_kmd_completion.py
	$(PYTHON) scripts/test_linux_fence_order.py
	$(PYTHON) scripts/inspect_kmd_cleanup.py
	$(PYTHON) scripts/inspect_kmd_recovery.py
	$(PYTHON) scripts/inspect_kmd_context_reset.py
	$(PYTHON) scripts/inspect_kmd_recovery_descriptor.py
	$(PYTHON) scripts/inspect_kmd_reset_order.py
	$(PYTHON) scripts/inspect_kmd_reset_dispatch.py
	$(PYTHON) scripts/inspect_kmd_reset_resources.py
	$(PYTHON) scripts/inspect_kmd_reset_reload.py
	$(PYTHON) scripts/inspect_kmd_reset_wakeup.py
	$(PYTHON) scripts/inspect_kmd_shutdown.py
	$(PYTHON) scripts/test_hfrp_stop.py
	$(PYTHON) scripts/test_linux_shutdown.py
	$(PYTHON) scripts/test_shutdown_transport.py
	$(PYTHON) scripts/inspect_cudla_release.py
	$(PYTHON) scripts/test_acpi_collection.py
	$(PYTHON) scripts/test_iort.py
	$(PYTHON) scripts/test_linux_dma_allocation.py
	$(PYTHON) scripts/test_linux_bootstrap.py
	$(PYTHON) scripts/test_npu_trigger.py
	$(PYTHON) scripts/test_linux_bootstrap_guards.py
	$(PYTHON) scripts/test_linux_bootstrap_ping.py
	$(PYTHON) scripts/test_control_command.py
	$(PYTHON) scripts/test_linux_config_snapshot.py
	$(PYTHON) scripts/test_default_config.py
	$(PYTHON) scripts/test_linux_config_apply.py
	$(PYTHON) scripts/test_linux_lpwr_config.py
	$(PYTHON) scripts/emulate_region5_setup.py
	$(PYTHON) scripts/test_linux_region5.py
	$(PYTHON) scripts/test_region5_history.py
	$(PYTHON) scripts/test_region5_producer.py
	$(PYTHON) scripts/test_hfrp_irq.py
	$(PYTHON) scripts/test_hfrp_ring.py
	$(PYTHON) scripts/test_hfrp_channel.py
	$(PYTHON) scripts/test_loader_prerequisites.py
	$(PYTHON) scripts/test_profile1_preemption.py
	$(PYTHON) scripts/inspect_kmd_preemption_request.py
	$(PYTHON) scripts/test_profile1_active_preemption.py
	$(PYTHON) scripts/test_profile1_context_save.py
	$(PYTHON) scripts/inspect_engine_context.py
	$(PYTHON) scripts/inspect_compiler_context.py
	$(PYTHON) scripts/inspect_engine_register_context.py
	$(PYTHON) scripts/inspect_engine_memory_context.py
	$(PYTHON) scripts/inspect_firmware_dma_completion.py
	$(PYTHON) scripts/test_native_context.py
	$(PYTHON) scripts/test_native_preempt.py
	$(PYTHON) scripts/test_job_preemption.py
	$(PYTHON) scripts/test_hfrp_power_policy.py
	$(PYTHON) scripts/test_hfrp_version.py
	$(PYTHON) scripts/test_hfrp_startup.py
	$(PYTHON) scripts/test_hfrp_power_state.py
	$(PYTHON) scripts/test_linux_hfrp_state.py
	$(PYTHON) scripts/test_hfrp_config.py
	$(PYTHON) scripts/test_hfrp_setup.py
	$(PYTHON) scripts/test_cold_start_dispatch.py
	$(PYTHON) scripts/test_dla_power_state.py
	$(PYTHON) scripts/test_hfrp_vbios.py
	$(PYTHON) scripts/test_vbios_startup_gate.py
	$(PYTHON) scripts/emulate_windows_ping.py
	$(PYTHON) scripts/test_npu_dma_domains.py
	$(PYTHON) scripts/test_sroot_uki_verifier.py
	$(PYTHON) scripts/test_sroot_menu.py
	./tests/test_firmware sources/dla-t35x-verified.bin
	$(PYTHON) scripts/emulate_firmware_parser.py
	$(PYTHON) scripts/decode_transfer_registers.py
	$(PYTHON) scripts/emulate_address_translation.py
	$(PYTHON) scripts/inspect_windows_profiles.py
	$(PYTHON) scripts/emulate_windows_device_id.py
	$(PYTHON) scripts/test_profile1_firmware.py
	$(PYTHON) scripts/emulate_profile1_commands.py
	$(PYTHON) scripts/emulate_profile1_task_header.py
	$(PYTHON) scripts/emulate_profile1_postactions.py
	$(PYTHON) scripts/emulate_profile1_dma_chunks.py
	$(PYTHON) scripts/emulate_profile1_address_list.py
	$(PYTHON) scripts/emulate_profile1_il_reads.py
	$(PYTHON) scripts/emulate_profile1_init_il.py
	$(PYTHON) scripts/test_profile1_loader.py
	$(PYTHON) scripts/emulate_boot_policy.py
	$(PYTHON) scripts/emulate_boot_variants.py
	$(PYTHON) scripts/emulate_boot_transport.py
	$(PYTHON) scripts/inspect_auxiliary_drivers.py
	$(PYTHON) scripts/emulate_sroot_resources.py
	$(PYTHON) scripts/emulate_sroot_policy.py
	$(PYTHON) scripts/emulate_sroot_dma.py
	$(PYTHON) scripts/emulate_sroot_image.py
	$(PYTHON) scripts/test_sroot_image.py
	$(PYTHON) scripts/inspect_sroot_envelope.py
	$(PYTHON) scripts/inspect_sroot_variants.py
	$(PYTHON) scripts/test_sroot_send.py
	$(PYTHON) scripts/test_sroot_receive.py
	$(PYTHON) scripts/test_sroot_prepare.py
	$(PYTHON) scripts/test_sroot_exchange.py
	$(PYTHON) scripts/test_sroot_full_upload.py
	$(PYTHON) scripts/test_sroot_upload_phase.py
	$(PYTHON) scripts/test_sroot_reinit.py
	$(PYTHON) scripts/inspect_sroot_reinit_handler.py
	$(PYTHON) scripts/emulate_method2_start.py
	$(PYTHON) scripts/emulate_common_loader.py
	$(PYTHON) scripts/emulate_postinit_commands.py
	$(PYTHON) scripts/test_profile1_config.py
	$(PYTHON) scripts/inspect_diagnostic_contract.py
	$(PYTHON) scripts/inspect_runtime_selector.py
	$(PYTHON) scripts/inspect_runtime_selector_setter.py
	$(PYTHON) scripts/inspect_provider_record.py
	$(PYTHON) scripts/inspect_kmd_private_selector.py
	$(PYTHON) scripts/inspect_loadable_version_gate.py
	$(PYTHON) scripts/inspect_content_materialization.py
	$(PYTHON) scripts/test_diagnostic_output.py
	$(PYTHON) scripts/test_diagnostic_scheduler_boundary.py
	$(PYTHON) scripts/test_command23_builder.py
	$(PYTHON) scripts/test_command23_provider.py
	$(PYTHON) scripts/test_profile1_lpwr_consumer.py
	$(PYTHON) scripts/test_profile1_regions.py
	$(PYTHON) scripts/emulate_profile1_irq_init.py
	$(PYTHON) scripts/emulate_profile1_boot.py
	$(PYTHON) scripts/emulate_irq_mask.py
	$(PYTHON) scripts/emulate_bitbang_transfer.py
	$(PYTHON) scripts/emulate_dma_transfer.py
	$(PYTHON) scripts/emulate_core_control.py
	$(PYTHON) scripts/test_word_upload.py
	$(PYTHON) scripts/test_core_control.py
	$(PYTHON) scripts/test_command.py
	$(PYTHON) scripts/emulate_task_header_checks.py
	$(PYTHON) scripts/test_task_descriptor.py
	$(PYTHON) scripts/test_bypass_task.py
	$(PYTHON) scripts/test_native_loadable.py
	$(PYTHON) scripts/test_dla_runtime.py
	$(PYTHON) scripts/test_linux_accel_backend.py
	$(PYTHON) scripts/test_linux_accel.py
	$(PYTHON) scripts/test_linux_bypass_task.py
	$(PYTHON) scripts/test_linux_bypass_firmware.py
	$(PYTHON) scripts/emulate_firmware_commands.py
	$(PYTHON) scripts/test_response.py
	$(PYTHON) scripts/test_response_decode.py
	$(PYTHON) scripts/emulate_postactions.py
	$(PYTHON) scripts/emulate_firmware_setup.py
	$(PYTHON) scripts/emulate_windows_region.py
	$(PYTHON) scripts/emulate_address_list.py
	$(PYTHON) scripts/test_address_list.py
	$(PYTHON) scripts/emulate_il_reads.py
	$(PYTHON) scripts/test_il_slice.py
	$(PYTHON) scripts/emulate_init_il.py
kernel-check:
	$(MAKE) -C "/usr/lib/modules/$(KERNEL_RELEASE)/build" M="$(CURDIR)/driver" n1x_dla_vbios.o n1x_dla_ring.o n1x_hfrp.o n1x_hfrp_setup.o n1x_dla_control.o n1x_dla_boot.o n1x_dla_firmware.o n1x_dla_registers.o n1x_dla_transfer.o n1x_dla_core.o n1x_dla_command.o n1x_dla_task.o n1x_dla_job.o n1x_dla_preempt.o n1x_dla_job_control.o n1x_dla_response.o n1x_dla_statistics.o n1x_dla_model.o n1x_sroot.o
clean:
	rm -f tests/test_firmware runtime/libn1x_dla.so runtime/cudla/libnvcudla.so runtime/cudla/cudla-smoke runtime/cudla/n1x-dla-broker

module:
	$(MAKE) -C "/usr/lib/modules/$(KERNEL_RELEASE)/build" M="$(CURDIR)/driver" modules

.PHONY: runtime
runtime: runtime/libn1x_dla.so
runtime/libn1x_dla.so: runtime/n1x_dla.c runtime/n1x_dla.h include/uapi/n1x_dla_accel.h
	$(CC) $(CFLAGS) -fPIC -shared -Iinclude runtime/n1x_dla.c -o $@

# Locally acquired official headers/library; never installs a replacement for
# NVIDIA's public libcudla. The backend stays in this isolated directory.
CUDLA_PACKAGE ?= $(CURDIR)/sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive
CUDA_ROOT ?= /opt/cuda
.PHONY: cudla
cudla: runtime/cudla/libnvcudla.so runtime/cudla/cudla-smoke runtime/cudla/n1x-dla-broker
.PHONY: test-cudla
test-cudla: cudla
	$(PYTHON) scripts/test_cudla_model.py
	$(PYTHON) scripts/test_cudla_broker.py
	$(PYTHON) scripts/test_cudla_broker_security.py
	$(PYTHON) scripts/test_qualify_cudla.py
	$(PYTHON) scripts/test_cudla_cuda_transport.py
runtime/cudla/libnvcudla.so: runtime/cudla_backend.cpp runtime/cudla_model.cpp runtime/cudla_model.hpp runtime/n1x_dla.h runtime/n1x_cudla_private.h include/uapi/n1x_dla_accel.h runtime/libn1x_dla.so $(CUDLA_PACKAGE)/include/cudla.h $(CUDA_ROOT)/include/cuda.h
	mkdir -p runtime/cudla
	$(CXX) -std=c++17 $(CFLAGS) -fPIC -shared -Iinclude -I$(CUDLA_PACKAGE)/include -I$(CUDA_ROOT)/include runtime/cudla_backend.cpp runtime/cudla_model.cpp -Lruntime -ln1x_dla -L$(CUDA_ROOT)/lib64/stubs -lcuda -Wl,-rpath,'$$ORIGIN/..' -Wl,-soname,libnvcudla.so -Wl,-z,defs -o $@
runtime/cudla/cudla-smoke: runtime/cudla_smoke.cpp $(CUDLA_PACKAGE)/include/cudla.h $(CUDA_ROOT)/include/cuda.h
	mkdir -p runtime/cudla
	$(CXX) -std=c++17 $(CFLAGS) -I$(CUDLA_PACKAGE)/include -I$(CUDA_ROOT)/include $< -L$(CUDLA_PACKAGE)/lib -lcudla -L$(CUDA_ROOT)/lib64/stubs -lcuda -Wl,-rpath,$(CUDLA_PACKAGE)/lib -o $@
runtime/cudla/n1x-dla-broker: runtime/cudla_broker.cpp runtime/n1x_dla.h runtime/n1x_cudla_private.h include/uapi/n1x_dla_accel.h runtime/cudla/libnvcudla.so runtime/libn1x_dla.so $(CUDLA_PACKAGE)/include/cudla.h $(CUDA_ROOT)/include/cuda.h
	mkdir -p runtime/cudla
	$(CXX) -std=c++17 $(CFLAGS) -D_FORTIFY_SOURCE=3 -fstack-protector-strong -fPIE \
		-Iinclude -Iruntime -I$(CUDLA_PACKAGE)/include -I$(CUDA_ROOT)/include $< \
		-Wl,--no-as-needed -Lruntime/cudla -lnvcudla -Wl,--as-needed -L$(CUDLA_PACKAGE)/lib -lcudla \
		-Lruntime -ln1x_dla -L$(CUDA_ROOT)/lib64/stubs -lcuda -lcrypto \
		-pie -Wl,-rpath,/usr/lib/n1x-npu-linux/cudla:/usr/lib/n1x-npu-linux \
		-Wl,-z,relro,-z,now,-z,noexecstack -o $@
