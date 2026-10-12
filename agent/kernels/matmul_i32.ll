; Integer matrix multiply C = A x B for n x n int32 matrices, one thread per output element.
; Compiled to PTX by agent/kernels/build_ptx.py (LLVM NVPTX backend). The PTX is JIT-compiled
; by the NVIDIA driver at load time, so no CUDA toolkit is needed on provider machines.
target datalayout = "e-i64:64-i128:128-v16:16-v32:32-n16:32:64"
target triple = "nvptx64-nvidia-cuda"

declare i32 @llvm.nvvm.read.ptx.sreg.tid.x()
declare i32 @llvm.nvvm.read.ptx.sreg.tid.y()
declare i32 @llvm.nvvm.read.ptx.sreg.ctaid.x()
declare i32 @llvm.nvvm.read.ptx.sreg.ctaid.y()
declare i32 @llvm.nvvm.read.ptx.sreg.ntid.x()
declare i32 @llvm.nvvm.read.ptx.sreg.ntid.y()

define void @matmul_i32(i32 addrspace(1)* %a, i32 addrspace(1)* %b, i32 addrspace(1)* %c, i32 %n) {
entry:
  %tx = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
  %ty = call i32 @llvm.nvvm.read.ptx.sreg.tid.y()
  %bx = call i32 @llvm.nvvm.read.ptx.sreg.ctaid.x()
  %by = call i32 @llvm.nvvm.read.ptx.sreg.ctaid.y()
  %dx = call i32 @llvm.nvvm.read.ptx.sreg.ntid.x()
  %dy = call i32 @llvm.nvvm.read.ptx.sreg.ntid.y()
  %bxdx = mul i32 %bx, %dx
  %col = add i32 %bxdx, %tx
  %bydy = mul i32 %by, %dy
  %row = add i32 %bydy, %ty
  %colok = icmp slt i32 %col, %n
  %rowok = icmp slt i32 %row, %n
  %ok = and i1 %colok, %rowok
  br i1 %ok, label %pre, label %exit

pre:
  %rown = mul i32 %row, %n
  %nonempty = icmp sgt i32 %n, 0
  br i1 %nonempty, label %loop, label %store

loop:
  %k = phi i32 [ 0, %pre ], [ %knext, %loop ]
  %acc = phi i32 [ 0, %pre ], [ %accnext, %loop ]
  %ai = add i32 %rown, %k
  %ai64 = sext i32 %ai to i64
  %ap = getelementptr i32, i32 addrspace(1)* %a, i64 %ai64
  %av = load i32, i32 addrspace(1)* %ap, align 4
  %kn = mul i32 %k, %n
  %bi = add i32 %kn, %col
  %bi64 = sext i32 %bi to i64
  %bp = getelementptr i32, i32 addrspace(1)* %b, i64 %bi64
  %bv = load i32, i32 addrspace(1)* %bp, align 4
  %prod = mul i32 %av, %bv
  %accnext = add i32 %acc, %prod
  %knext = add i32 %k, 1
  %done = icmp eq i32 %knext, %n
  br i1 %done, label %store, label %loop

store:
  %res = phi i32 [ 0, %pre ], [ %accnext, %loop ]
  %ci = add i32 %rown, %col
  %ci64 = sext i32 %ci to i64
  %cp = getelementptr i32, i32 addrspace(1)* %c, i64 %ci64
  store i32 %res, i32 addrspace(1)* %cp, align 4
  br label %exit

exit:
  ret void
}

!nvvm.annotations = !{!0}
!0 = !{void (i32 addrspace(1)*, i32 addrspace(1)*, i32 addrspace(1)*, i32)* @matmul_i32, !"kernel", i32 1}
