"""让测试在「没装成包」的仓库里也能 import 到顶层模块。

测试既可以在 `pip install -e .` 之后运行，也可以直接克隆下来跑；
这里把仓库根目录放进 sys.path，`werewolf` / `multi_agent` / `agents` 都能被导入。
"""
import pathlib
import sys

ROOT = str(pathlib.Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
