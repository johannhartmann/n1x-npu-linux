CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
.PHONY: test test-vendor clean
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
	venv/bin/python scripts/emulate_bitbang_transfer.py
	venv/bin/python scripts/emulate_dma_transfer.py
	venv/bin/python scripts/emulate_core_control.py
	venv/bin/python scripts/test_word_upload.py
clean:
	rm -f tests/test_firmware
