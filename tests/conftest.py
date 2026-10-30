"""Windows 本机跑测试时的最小 fcntl 兼容层（生产环境始终是 Linux）。"""
import sys
import types

if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 2
    fcntl.LOCK_SH = 1
    fcntl.LOCK_UN = 8
    fcntl.flock = lambda *_args, **_kwargs: None
    sys.modules["fcntl"] = fcntl
