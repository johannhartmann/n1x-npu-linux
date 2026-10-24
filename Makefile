CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
.PHONY: test test-vendor kernel-check clean
tests/test_firmware: tests/test_firmware.c driver/n1x_dla_firmware.c include/n1x_dla_firmware.h
	$(CC) $(CFLAGS) -Iinclude tests/test_firmware.c driver/n1x_dla_firmware.c -o $@
test: tests/test_firmware
	./tests/test_firmware
# Requires the exact ignored vendor inputs and the analysis venv.
test-vendor: test
	./tests/test_firmware sources/dla-t35x-verified.bin
	venv/bin/python scripts/emulate_firmware_parser.py
	venv/bin/python scripts/decode_transfer_registers.py
	venv/bin/python scripts/emulate_address_translation.py
	venv/bin/python scripts/inspect_windows_profiles.py
	venv/bin/python scripts/emulate_windows_device_id.py
	venv/bin/python scripts/test_profile1_firmware.py
	venv/bin/python scripts/emulate_profile1_commands.py
	venv/bin/python scripts/emulate_bitbang_transfer.py
	venv/bin/python scripts/emulate_dma_transfer.py
	venv/bin/python scripts/emulate_core_control.py
	venv/bin/python scripts/test_word_upload.py
	venv/bin/python scripts/test_core_control.py
	venv/bin/python scripts/test_command.py
	venv/bin/python scripts/emulate_task_header_checks.py
	venv/bin/python scripts/test_task_descriptor.py
	venv/bin/python scripts/emulate_firmware_commands.py
	venv/bin/python scripts/test_response.py
	venv/bin/python scripts/emulate_postactions.py
	venv/bin/python scripts/emulate_firmware_setup.py
	venv/bin/python scripts/emulate_windows_region.py
	venv/bin/python scripts/emulate_address_list.py
	venv/bin/python scripts/test_address_list.py
	venv/bin/python scripts/emulate_il_reads.py
	venv/bin/python scripts/test_il_slice.py
	venv/bin/python scripts/emulate_init_il.py
kernel-check:
	$(MAKE) -C /usr/lib/modules/$(shell uname -r)/build M=$(CURDIR)/driver n1x_dla_firmware.o n1x_dla_registers.o n1x_dla_transfer.o n1x_dla_core.o n1x_dla_command.o n1x_dla_task.o n1x_dla_response.o
clean:
	rm -f tests/test_firmware
