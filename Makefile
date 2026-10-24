CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
.PHONY: test clean
tests/test_firmware: tests/test_firmware.c driver/n1x_dla_firmware.c include/n1x_dla_firmware.h
	$(CC) $(CFLAGS) -Iinclude tests/test_firmware.c driver/n1x_dla_firmware.c -o $@
test: tests/test_firmware
	./tests/test_firmware
clean:
	rm -f tests/test_firmware
