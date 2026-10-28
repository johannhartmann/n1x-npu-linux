CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
# Explicit installed-kernel target; builds only, never installs/loads modules.
KERNEL_RELEASE ?= $(shell uname -r)
.PHONY: test test-vendor test-relu test-conv test-conv-relu test-p16-start kernel-check module clean generate-probe
# Requires the exact ignored physical BIOS-300 capture and pinned vendor KMD.
# Offline: no root, module load, hardware access or firmware installation.
test-p16-start:
	venv/bin/python scripts/verify_p16_board_policy.py results/p16-bios300-physical.rom
	venv/bin/python scripts/test_linux_hfrp_prepare.py
	venv/bin/python scripts/test_linux_hfrp_state.py
	venv/bin/python scripts/test_linux_fence_order.py
	venv/bin/python scripts/test_linux_bootstrap.py
	venv/bin/python scripts/test_linux_bootstrap_guards.py
	venv/bin/python scripts/test_hfrp_setup.py
	venv/bin/python scripts/test_npu_dma_domains.py
	venv/bin/python scripts/test_qualify_p16_start.py
	venv/bin/python scripts/test_linux_lpwr_config.py
	venv/bin/python scripts/test_native_loadable.py
	venv/bin/python scripts/test_dla_runtime.py
	venv/bin/python scripts/test_linux_accel_backend.py
	venv/bin/python scripts/test_linux_accel.py
	venv/bin/python scripts/test_linux_bypass_task.py
	venv/bin/python scripts/test_linux_bypass_firmware.py
	venv/bin/python scripts/test_response.py
	venv/bin/python scripts/test_response_decode.py
	venv/bin/python scripts/test_profile1_lpwr_consumer.py
# Device-free convolution compiler bridge; preserves the pinned ReLU directory.
test-conv:
	venv/bin/python scripts/probe_native_graph.py --model conv1x1
	venv/bin/python scripts/probe_native_graph.py --model conv1x1 --target t254 --capture-mtl
	venv/bin/python scripts/inspect_conv_pretranslation.py
	venv/bin/python scripts/probe_native_graph.py --model conv1x1 --target t254 --capture-translation-input
	venv/bin/python scripts/inspect_conv_pretranslation.py --translate
	venv/bin/python scripts/probe_native_graph.py --model conv1x1 --target t254 --package-windows-output
	venv/bin/python scripts/verify_conv_loadable.py
	venv/bin/python scripts/inspect_conv_memory_allocation.py
	venv/bin/python scripts/test_linux_accel.py --conv
	venv/bin/python scripts/create_conv_probe.py
	venv/bin/python scripts/test_conv_acceptance.py
	venv/bin/python scripts/inspect_fp19_conversion.py

# Two source layers, including their local DRAM intermediate; offline only.
test-conv-relu:
	venv/bin/python scripts/probe_native_graph.py --model conv1x1_relu
	venv/bin/python scripts/probe_native_graph.py --model conv1x1_relu --target t254 --capture-mtl
	venv/bin/python scripts/inspect_conv_pretranslation.py --model conv1x1_relu
	venv/bin/python scripts/probe_native_graph.py --model conv1x1_relu --target t254 --capture-translation-input
	venv/bin/python scripts/inspect_conv_pretranslation.py --model conv1x1_relu --translate
	venv/bin/python scripts/probe_native_graph.py --model conv1x1_relu --target t254 --package-windows-output
	venv/bin/python scripts/verify_conv_loadable.py --model conv1x1_relu
	venv/bin/python scripts/inspect_conv_memory_allocation.py --model conv1x1_relu
	venv/bin/python scripts/test_linux_accel.py --conv-relu
	venv/bin/python scripts/create_conv_probe.py --relu
	venv/bin/python scripts/test_conv_acceptance.py --relu

generate-probe:
	venv/bin/python scripts/create_relu_probe.py
# Requires the generated ReLU source, checked Windows outputs and serialized
# artifact from docs/model-probe.md. Offline only; no module or device access.
test-relu:
	venv/bin/python scripts/verify_relu_loadable.py
	venv/bin/python scripts/inspect_loadable_version_gate.py --relu
	venv/bin/python scripts/inspect_content_materialization.py --relu
	venv/bin/python scripts/inspect_full_deserialization.py
	venv/bin/python scripts/inspect_loadable_extensions.py
	venv/bin/python scripts/inspect_memory_initialization.py
	venv/bin/python scripts/test_statistics_v2.py
	venv/bin/python scripts/test_model_addresses.py
	venv/bin/python scripts/test_model_contents.py
	venv/bin/python scripts/test_relu_scheduler.py
tests/test_firmware: tests/test_firmware.c driver/n1x_dla_firmware.c include/n1x_dla_firmware.h
	$(CC) $(CFLAGS) -Iinclude tests/test_firmware.c driver/n1x_dla_firmware.c -o $@
test: tests/test_firmware
	./tests/test_firmware
# Requires the exact ignored vendor inputs, analysis venv and generated ReLU
# artifacts described in docs/model-probe.md.
test-vendor: test
	venv/bin/python scripts/inspect_windows_submission.py
	venv/bin/python scripts/inspect_runtime_command.py
	venv/bin/python scripts/inspect_kmd_command_descriptor.py
	venv/bin/python scripts/inspect_kmd_task_submission.py
	venv/bin/python scripts/inspect_kmd_virtual_submit.py
	venv/bin/python scripts/inspect_kmd_completion.py
	venv/bin/python scripts/test_linux_fence_order.py
	venv/bin/python scripts/inspect_kmd_cleanup.py
	venv/bin/python scripts/inspect_kmd_recovery.py
	venv/bin/python scripts/inspect_kmd_context_reset.py
	venv/bin/python scripts/inspect_kmd_recovery_descriptor.py
	venv/bin/python scripts/inspect_kmd_reset_order.py
	venv/bin/python scripts/inspect_kmd_reset_dispatch.py
	venv/bin/python scripts/inspect_kmd_reset_resources.py
	venv/bin/python scripts/inspect_kmd_reset_reload.py
	venv/bin/python scripts/inspect_kmd_reset_wakeup.py
	venv/bin/python scripts/inspect_cudla_release.py
	venv/bin/python scripts/test_acpi_collection.py
	venv/bin/python scripts/test_iort.py
	venv/bin/python scripts/test_linux_dma_allocation.py
	venv/bin/python scripts/test_linux_bootstrap.py
	venv/bin/python scripts/test_npu_trigger.py
	venv/bin/python scripts/test_linux_bootstrap_guards.py
	venv/bin/python scripts/test_linux_bootstrap_ping.py
	venv/bin/python scripts/test_control_command.py
	venv/bin/python scripts/test_linux_config_snapshot.py
	venv/bin/python scripts/test_default_config.py
	venv/bin/python scripts/test_linux_config_apply.py
	venv/bin/python scripts/test_linux_lpwr_config.py
	venv/bin/python scripts/emulate_region5_setup.py
	venv/bin/python scripts/test_linux_region5.py
	venv/bin/python scripts/test_region5_history.py
	venv/bin/python scripts/test_region5_producer.py
	venv/bin/python scripts/test_hfrp_irq.py
	venv/bin/python scripts/test_hfrp_ring.py
	venv/bin/python scripts/test_hfrp_channel.py
	venv/bin/python scripts/test_loader_prerequisites.py
	venv/bin/python scripts/test_profile1_preemption.py
	venv/bin/python scripts/inspect_kmd_preemption_request.py
	venv/bin/python scripts/test_profile1_active_preemption.py
	venv/bin/python scripts/test_profile1_context_save.py
	venv/bin/python scripts/inspect_engine_context.py
	venv/bin/python scripts/inspect_compiler_context.py
	venv/bin/python scripts/inspect_engine_register_context.py
	venv/bin/python scripts/inspect_engine_memory_context.py
	venv/bin/python scripts/inspect_firmware_dma_completion.py
	venv/bin/python scripts/test_native_context.py
	venv/bin/python scripts/test_native_preempt.py
	venv/bin/python scripts/test_job_preemption.py
	venv/bin/python scripts/test_hfrp_power_policy.py
	venv/bin/python scripts/test_hfrp_version.py
	venv/bin/python scripts/test_hfrp_startup.py
	venv/bin/python scripts/test_hfrp_power_state.py
	venv/bin/python scripts/test_linux_hfrp_state.py
	venv/bin/python scripts/test_hfrp_config.py
	venv/bin/python scripts/test_hfrp_setup.py
	venv/bin/python scripts/test_cold_start_dispatch.py
	venv/bin/python scripts/test_dla_power_state.py
	venv/bin/python scripts/test_hfrp_vbios.py
	venv/bin/python scripts/test_vbios_startup_gate.py
	venv/bin/python scripts/emulate_windows_ping.py
	venv/bin/python scripts/test_npu_dma_domains.py
	venv/bin/python scripts/test_sroot_uki_verifier.py
	venv/bin/python scripts/test_sroot_menu.py
	./tests/test_firmware sources/dla-t35x-verified.bin
	venv/bin/python scripts/emulate_firmware_parser.py
	venv/bin/python scripts/decode_transfer_registers.py
	venv/bin/python scripts/emulate_address_translation.py
	venv/bin/python scripts/inspect_windows_profiles.py
	venv/bin/python scripts/emulate_windows_device_id.py
	venv/bin/python scripts/test_profile1_firmware.py
	venv/bin/python scripts/emulate_profile1_commands.py
	venv/bin/python scripts/emulate_profile1_task_header.py
	venv/bin/python scripts/emulate_profile1_postactions.py
	venv/bin/python scripts/emulate_profile1_dma_chunks.py
	venv/bin/python scripts/emulate_profile1_address_list.py
	venv/bin/python scripts/emulate_profile1_il_reads.py
	venv/bin/python scripts/emulate_profile1_init_il.py
	venv/bin/python scripts/test_profile1_loader.py
	venv/bin/python scripts/emulate_boot_policy.py
	venv/bin/python scripts/emulate_boot_variants.py
	venv/bin/python scripts/emulate_boot_transport.py
	venv/bin/python scripts/inspect_auxiliary_drivers.py
	venv/bin/python scripts/emulate_sroot_resources.py
	venv/bin/python scripts/emulate_sroot_policy.py
	venv/bin/python scripts/emulate_sroot_dma.py
	venv/bin/python scripts/emulate_sroot_image.py
	venv/bin/python scripts/test_sroot_image.py
	venv/bin/python scripts/inspect_sroot_envelope.py
	venv/bin/python scripts/inspect_sroot_variants.py
	venv/bin/python scripts/test_sroot_send.py
	venv/bin/python scripts/test_sroot_receive.py
	venv/bin/python scripts/test_sroot_prepare.py
	venv/bin/python scripts/test_sroot_exchange.py
	venv/bin/python scripts/test_sroot_full_upload.py
	venv/bin/python scripts/test_sroot_upload_phase.py
	venv/bin/python scripts/test_sroot_reinit.py
	venv/bin/python scripts/inspect_sroot_reinit_handler.py
	venv/bin/python scripts/emulate_method2_start.py
	venv/bin/python scripts/emulate_common_loader.py
	venv/bin/python scripts/emulate_postinit_commands.py
	venv/bin/python scripts/test_profile1_config.py
	venv/bin/python scripts/inspect_diagnostic_contract.py
	venv/bin/python scripts/inspect_runtime_selector.py
	venv/bin/python scripts/inspect_runtime_selector_setter.py
	venv/bin/python scripts/inspect_provider_record.py
	venv/bin/python scripts/inspect_kmd_private_selector.py
	venv/bin/python scripts/inspect_loadable_version_gate.py
	venv/bin/python scripts/inspect_content_materialization.py
	venv/bin/python scripts/test_diagnostic_output.py
	venv/bin/python scripts/test_diagnostic_scheduler_boundary.py
	venv/bin/python scripts/test_command23_builder.py
	venv/bin/python scripts/test_command23_provider.py
	venv/bin/python scripts/test_profile1_lpwr_consumer.py
	venv/bin/python scripts/test_profile1_regions.py
	venv/bin/python scripts/emulate_profile1_irq_init.py
	venv/bin/python scripts/emulate_profile1_boot.py
	venv/bin/python scripts/emulate_irq_mask.py
	venv/bin/python scripts/emulate_bitbang_transfer.py
	venv/bin/python scripts/emulate_dma_transfer.py
	venv/bin/python scripts/emulate_core_control.py
	venv/bin/python scripts/test_word_upload.py
	venv/bin/python scripts/test_core_control.py
	venv/bin/python scripts/test_command.py
	venv/bin/python scripts/emulate_task_header_checks.py
	venv/bin/python scripts/test_task_descriptor.py
	venv/bin/python scripts/test_bypass_task.py
	venv/bin/python scripts/test_native_loadable.py
	venv/bin/python scripts/test_dla_runtime.py
	venv/bin/python scripts/test_linux_accel_backend.py
	venv/bin/python scripts/test_linux_accel.py
	venv/bin/python scripts/test_linux_bypass_task.py
	venv/bin/python scripts/test_linux_bypass_firmware.py
	venv/bin/python scripts/emulate_firmware_commands.py
	venv/bin/python scripts/test_response.py
	venv/bin/python scripts/test_response_decode.py
	venv/bin/python scripts/emulate_postactions.py
	venv/bin/python scripts/emulate_firmware_setup.py
	venv/bin/python scripts/emulate_windows_region.py
	venv/bin/python scripts/emulate_address_list.py
	venv/bin/python scripts/test_address_list.py
	venv/bin/python scripts/emulate_il_reads.py
	venv/bin/python scripts/test_il_slice.py
	venv/bin/python scripts/emulate_init_il.py
kernel-check:
	$(MAKE) -C "/usr/lib/modules/$(KERNEL_RELEASE)/build" M="$(CURDIR)/driver" n1x_dla_vbios.o n1x_dla_ring.o n1x_hfrp.o n1x_hfrp_setup.o n1x_dla_control.o n1x_dla_boot.o n1x_dla_firmware.o n1x_dla_registers.o n1x_dla_transfer.o n1x_dla_core.o n1x_dla_command.o n1x_dla_task.o n1x_dla_job.o n1x_dla_preempt.o n1x_dla_job_control.o n1x_dla_response.o n1x_dla_statistics.o n1x_dla_model.o n1x_sroot.o
clean:
	rm -f tests/test_firmware runtime/libn1x_dla.so

module:
	$(MAKE) -C "/usr/lib/modules/$(KERNEL_RELEASE)/build" M="$(CURDIR)/driver" modules

.PHONY: runtime
runtime: runtime/libn1x_dla.so
runtime/libn1x_dla.so: runtime/n1x_dla.c runtime/n1x_dla.h include/uapi/n1x_dla_accel.h
	$(CC) $(CFLAGS) -fPIC -shared -Iinclude runtime/n1x_dla.c -o $@
