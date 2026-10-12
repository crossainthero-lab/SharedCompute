"""In-process stand-in for libcuda, for tests only.

It receives exactly the ctypes arguments the real binding passes (byref objects, string buffers, device
pointers, the kernel-parameter pointer array) and implements the matmul kernel's *semantics* in Python.
It does NOT interpret the PTX, so it validates marshalling, memory handling, error paths and
orchestration, not the kernel code itself. Only a real NVIDIA GPU can validate the PTX.
"""
from __future__ import annotations

import ctypes
from ctypes import c_int32, c_uint64

CUDA_ERROR_INVALID_PTX = 218
CUDA_ERROR_OUT_OF_MEMORY = 2
ERROR_NAMES = {CUDA_ERROR_INVALID_PTX: b"CUDA_ERROR_INVALID_PTX", CUDA_ERROR_OUT_OF_MEMORY: b"CUDA_ERROR_OUT_OF_MEMORY",
               100: b"CUDA_ERROR_NO_DEVICE", 999: b"CUDA_ERROR_UNKNOWN"}


class FakeCuda:
    def __init__(self, devices=None, driver_version=12020, free_mb=None, fail=None, jit_error=None,
                 corrupt=False, legacy_only=False):
        self.devices = devices if devices is not None else [
            {"name": "Fake GeForce GTX 1050 Ti", "cc": (6, 1), "total_mb": 4096, "pci": "0000:01:00.0"}]
        self.driver_version = driver_version
        self.free_mb = free_mb
        self.fail = dict(fail or {})
        self.jit_error = jit_error
        self.corrupt = corrupt  # flip one output element, as a broken GPU or driver might
        self.memory = {}
        self.next_ptr = 0x10000
        self.contexts = 0
        self.modules = 0
        self.launches = 0
        self.calls = []
        if legacy_only:  # simulate an old driver without the _v2 entry points
            for name in [n for n in dir(self) if n.startswith("cu") and n.endswith("_v2")]:
                setattr(self, "_impl_" + name, getattr(self, name))
                setattr(self, name, None)

    def _impl(self, name):
        return self.__dict__.get("_impl_" + name) or getattr(self, name)

    def _rc(self, name):
        self.calls.append(name)
        return self.fail.get(name, 0)

    # discovery
    def cuInit(self, flags):
        return self._rc("cuInit")

    def cuDriverGetVersion(self, out):
        out._obj.value = self.driver_version
        return self._rc("cuDriverGetVersion")

    def cuDeviceGetCount(self, out):
        out._obj.value = len(self.devices)
        return self._rc("cuDeviceGetCount")

    def cuDeviceGet(self, out, ordinal):
        if ordinal.value >= len(self.devices):
            return 101
        out._obj.value = ordinal.value
        return self._rc("cuDeviceGet")

    def cuDeviceGetName(self, buf, length, dev):
        buf.value = self.devices[dev.value]["name"].encode()[: length.value - 1]
        return self._rc("cuDeviceGetName")

    def cuDeviceGetAttribute(self, out, attr, dev):
        major, minor = self.devices[dev.value]["cc"]
        out._obj.value = {75: major, 76: minor}[attr.value]
        return self._rc("cuDeviceGetAttribute")

    def cuDeviceTotalMem_v2(self, out, dev):
        out._obj.value = self.devices[dev.value]["total_mb"] * 1024 * 1024
        return self._rc("cuDeviceTotalMem_v2")

    def cuDeviceTotalMem(self, out, dev):
        return self._impl("cuDeviceTotalMem_v2")(out, dev)

    def cuDeviceGetPCIBusId(self, buf, length, dev):
        buf.value = self.devices[dev.value]["pci"].encode()
        return self._rc("cuDeviceGetPCIBusId")

    def cuGetErrorName(self, rc, out):
        out._obj.value = ERROR_NAMES.get(rc, b"CUDA_ERROR_UNKNOWN")
        return 0

    # contexts and memory
    def cuCtxCreate_v2(self, out, flags, dev):
        rc = self._rc("cuCtxCreate_v2")
        if rc == 0:
            self.contexts += 1
            out._obj.value = 0xC0
        return rc

    def cuCtxCreate(self, out, flags, dev):
        return self._impl("cuCtxCreate_v2")(out, flags, dev)

    def cuCtxDestroy_v2(self, ctx):
        self.contexts -= 1
        return self._rc("cuCtxDestroy_v2")

    def cuCtxDestroy(self, ctx):
        return self._impl("cuCtxDestroy_v2")(ctx)

    def cuMemGetInfo_v2(self, free, total):
        total_mb = self.devices[0]["total_mb"]
        free._obj.value = (self.free_mb if self.free_mb is not None else total_mb - 100) * 1024 * 1024
        total._obj.value = total_mb * 1024 * 1024
        return self._rc("cuMemGetInfo_v2")

    def cuMemGetInfo(self, free, total):
        return self._impl("cuMemGetInfo_v2")(free, total)

    def cuMemAlloc_v2(self, out, size):
        rc = self._rc("cuMemAlloc_v2")
        if rc:
            return rc
        ptr = self.next_ptr
        self.next_ptr += size.value + 256
        self.memory[ptr] = bytearray(size.value)
        out._obj.value = ptr
        return 0

    def cuMemAlloc(self, out, size):
        return self._impl("cuMemAlloc_v2")(out, size)

    def cuMemFree_v2(self, ptr):
        del self.memory[ptr.value]
        return self._rc("cuMemFree_v2")

    def cuMemFree(self, ptr):
        return self._impl("cuMemFree_v2")(ptr)

    def cuMemcpyHtoD_v2(self, dst, src, size):
        self.memory[dst.value][:] = ctypes.string_at(ctypes.addressof(src), size.value)
        return self._rc("cuMemcpyHtoD_v2")

    def cuMemcpyHtoD(self, dst, src, size):
        return self._impl("cuMemcpyHtoD_v2")(dst, src, size)

    def cuMemcpyDtoH_v2(self, dst, src, size):
        data = bytes(self.memory[src.value])
        ctypes.memmove(dst, data, size.value)
        return self._rc("cuMemcpyDtoH_v2")

    def cuMemcpyDtoH(self, dst, src, size):
        return self._impl("cuMemcpyDtoH_v2")(dst, src, size)

    # modules and launches
    def cuModuleLoadDataEx(self, out, image, n_options, options, values):
        if self.jit_error:
            msg = self.jit_error.encode()
            ctypes.memmove(values[0], msg, len(msg) + 1)
            return CUDA_ERROR_INVALID_PTX
        assert b".entry matmul_i32" in image.value and b".target sm_30" in image.value
        self.modules += 1
        out._obj.value = 0xD0
        return self._rc("cuModuleLoadDataEx")

    def cuModuleUnload(self, module):
        self.modules -= 1
        return 0

    def cuModuleGetFunction(self, out, module, name):
        assert name == b"matmul_i32"
        out._obj.value = 0xF0
        return self._rc("cuModuleGetFunction")

    def cuLaunchKernel(self, fn, gx, gy, gz, bx, by, bz, shared, stream, params, extra):
        rc = self._rc("cuLaunchKernel")
        if rc:
            return rc
        a_ptr = c_uint64.from_address(params[0]).value
        b_ptr = c_uint64.from_address(params[1]).value
        c_ptr = c_uint64.from_address(params[2]).value
        n = c_int32.from_address(params[3]).value
        assert gx.value * bx.value >= n and gy.value * by.value >= n, "grid does not cover the matrix"
        a = (c_int32 * (n * n)).from_buffer_copy(bytes(self.memory[a_ptr]))
        b = (c_int32 * (n * n)).from_buffer_copy(bytes(self.memory[b_ptr]))
        c = (c_int32 * (n * n))()
        for i in range(n):
            row = [0] * n
            for k in range(n):
                aik = a[i * n + k]
                if aik:
                    row = [r + aik * b[k * n + j] for j, r in enumerate(row)]
            c[i * n: (i + 1) * n] = row
        if self.corrupt:
            c[n + 1] += 1
        self.memory[c_ptr][:] = bytes(c)
        self.launches += 1
        return 0

    def cuCtxSynchronize(self):
        return self._rc("cuCtxSynchronize")

    def cuEventCreate(self, out, flags):
        out._obj.value = 0xE0
        return self._rc("cuEventCreate")

    def cuEventRecord(self, ev, stream):
        return self._rc("cuEventRecord")

    def cuEventSynchronize(self, ev):
        return self._rc("cuEventSynchronize")

    def cuEventElapsedTime(self, out, start, stop):
        out._obj.value = 0.25 * max(1, self.launches - 1)
        return self._rc("cuEventElapsedTime")

    def cuEventDestroy_v2(self, ev):
        return self._rc("cuEventDestroy_v2")

    def cuEventDestroy(self, ev):
        return self._impl("cuEventDestroy_v2")(ev)
