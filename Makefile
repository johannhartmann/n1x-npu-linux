CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
KERNEL_RELEASE ?= $(shell uname -r)
PYTHON ?= venv/bin/python
CUDLA_PACKAGE ?= $(CURDIR)/sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive
CUDA_ROOT ?= /opt/cuda

.DEFAULT_GOAL := help
.PHONY: help check check-repo test test-portable module clean runtime compiler ort test-compiler cudla test-cudla

help:
	@echo 'make check           Repository consistency and portable driver/runtime unit tests'
	@echo 'make check-repo      Documentation links, Python syntax, JSON and demo provenance'
	@echo 'make test-portable   Portable driver/protocol/lifetime unit tests'
	@echo 'make runtime cudla   Build userspace compiler, ONNX EP, and cuDLA backend'
	@echo 'make test-compiler   Run native t254 compiler and ONNX Execution Provider tests'
	@echo 'make test-cudla      Run cuDLA backend, model, and CUDA staging tests'
	@echo 'make module          Build n1x_npu_bootstrap.ko against KERNEL_RELEASE'
	@echo 'make clean           Remove generated userspace build outputs'

check: check-repo test-portable
test: test-portable

check-repo:
	$(PYTHON) scripts/check_repo.py
	$(PYTHON) scripts/test_check_repo.py

test-portable:
	$(PYTHON) scripts/test_prepare_inputs.py
	$(PYTHON) scripts/test_hfrp_channel.py
	$(PYTHON) scripts/test_linux_bootstrap_guards.py
	$(PYTHON) scripts/test_linux_boot_delay.py
	$(PYTHON) scripts/test_linux_bootstrap_ping.py
	$(PYTHON) scripts/test_linux_hfrp_state.py
	$(PYTHON) scripts/test_linux_config_snapshot.py
	$(PYTHON) scripts/test_linux_config_apply.py
	$(PYTHON) scripts/test_linux_bypass_task.py
	$(PYTHON) scripts/test_linux_fence_order.py
	$(PYTHON) scripts/test_linux_accel.py
	$(PYTHON) scripts/test_linux_accel_backend.py
	$(PYTHON) scripts/test_linux_shutdown.py
	$(PYTHON) scripts/test_firmware_cache.py
	$(PYTHON) scripts/test_linux_binding.py
	$(PYTHON) scripts/test_linux_context.py
	$(PYTHON) scripts/test_shutdown_transport.py
	$(PYTHON) scripts/test_dla_runtime.py
	$(PYTHON) scripts/test_npu_reload.py
	$(PYTHON) scripts/test_sroot_cca_quirk.py
	$(PYTHON) scripts/test_sroot_dsp_broker.py

module:
	$(MAKE) -C "/usr/lib/modules/$(KERNEL_RELEASE)/build" M="$(CURDIR)/driver" modules

clean:
	rm -f runtime/libn1x_dla.so runtime/libn1x_compiler.so runtime/n1x-compile runtime/libn1x_ort.so runtime/n1x-ort-run runtime/cudla/libnvcudla.so runtime/cudla/cudla-smoke

runtime: runtime/libn1x_dla.so runtime/libn1x_compiler.so runtime/n1x-compile runtime/libn1x_ort.so runtime/n1x-ort-run
compiler: runtime/libn1x_compiler.so runtime/n1x-compile
ort: runtime/libn1x_ort.so runtime/n1x-ort-run

runtime/libn1x_dla.so: runtime/n1x_dla.c runtime/n1x_dla.h include/uapi/n1x_dla_accel.h
	$(CC) $(CFLAGS) -fPIC -shared -Iinclude runtime/n1x_dla.c -o $@

runtime/libn1x_compiler.so: runtime/n1x_compiler_shim.cpp runtime/n1x_compile.cpp include/n1x_compiler.h
	$(CXX) -std=c++17 $(CFLAGS) -ffixed-x18 -fPIC -shared -Iinclude runtime/n1x_compiler_shim.cpp runtime/n1x_compile.cpp -ldl -lpthread -lcrypto -o $@

runtime/n1x-compile: runtime/n1x_compile.cpp include/n1x_compiler.h runtime/libn1x_compiler.so
	$(CXX) -std=c++17 $(CFLAGS) -DN1X_COMPILE_CLI_MAIN -Iinclude runtime/n1x_compile.cpp -Lruntime -ln1x_compiler -Wl,-rpath,'$$ORIGIN' -o $@

runtime/libn1x_ort.so: runtime/n1x_ort.cpp runtime/cudla_model.cpp runtime/cudla_model.hpp include/n1x_ort.h runtime/n1x_dla.h include/uapi/n1x_dla_accel.h runtime/libn1x_dla.so $(CUDLA_PACKAGE)/include/cudla.h
	$(CXX) -std=c++17 $(CFLAGS) -fPIC -shared -Iinclude -Iruntime -I$(CUDLA_PACKAGE)/include runtime/n1x_ort.cpp runtime/cudla_model.cpp -Lruntime -ln1x_dla -Wl,-rpath,'$$ORIGIN' -o $@

runtime/n1x-ort-run: runtime/n1x_ort.cpp runtime/cudla_model.hpp include/n1x_ort.h runtime/n1x_dla.h runtime/libn1x_ort.so $(CUDLA_PACKAGE)/include/cudla.h
	$(CXX) -std=c++17 $(CFLAGS) -DN1X_ORT_CLI_MAIN -Iinclude -Iruntime -I$(CUDLA_PACKAGE)/include runtime/n1x_ort.cpp -Lruntime -ln1x_ort -Wl,-rpath,'$$ORIGIN' -o $@

test-compiler: compiler ort
	$(PYTHON) scripts/test_n1x_compiler.py
	$(PYTHON) scripts/test_n1x_ort.py
	$(PYTHON) scripts/test_superres_image.py

cudla: runtime/cudla/libnvcudla.so runtime/cudla/cudla-smoke

test-cudla: cudla compiler ort
	$(PYTHON) scripts/test_cudla_model.py
	$(PYTHON) scripts/test_qualify_cudla.py
	$(PYTHON) scripts/test_cudla_cuda_transport.py
	$(PYTHON) scripts/test_vision_examples.py
	$(PYTHON) scripts/test_n1x_compiler.py
	$(PYTHON) scripts/test_n1x_ort.py
	$(PYTHON) scripts/test_superres_image.py

runtime/cudla/libnvcudla.so: runtime/cudla_backend.cpp runtime/cudla_model.cpp runtime/cudla_model.hpp runtime/n1x_dla.h runtime/n1x_cudla_private.h include/uapi/n1x_dla_accel.h runtime/libn1x_dla.so $(CUDLA_PACKAGE)/include/cudla.h $(CUDA_ROOT)/include/cuda.h
	mkdir -p runtime/cudla
	$(CXX) -std=c++17 $(CFLAGS) -fPIC -shared -Iinclude -I$(CUDLA_PACKAGE)/include -I$(CUDA_ROOT)/include runtime/cudla_backend.cpp runtime/cudla_model.cpp -Lruntime -ln1x_dla -L$(CUDA_ROOT)/lib64/stubs -lcuda -Wl,-rpath,'$$ORIGIN/..' -Wl,-soname,libnvcudla.so -Wl,-z,defs -o $@

runtime/cudla/cudla-smoke: runtime/cudla_smoke.cpp $(CUDLA_PACKAGE)/include/cudla.h $(CUDA_ROOT)/include/cuda.h
	mkdir -p runtime/cudla
	$(CXX) -std=c++17 $(CFLAGS) -I$(CUDLA_PACKAGE)/include -I$(CUDA_ROOT)/include $< -L$(CUDLA_PACKAGE)/lib -lcudla -L$(CUDA_ROOT)/lib64/stubs -lcuda -Wl,-rpath,$(CUDLA_PACKAGE)/lib -o $@
