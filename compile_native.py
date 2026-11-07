#!/usr/bin/env python3
"""
Compiles proprietary Python source files into native machine code (C extensions)
using Cython. Produces .pyd (Windows) or .so (Linux) binaries that cannot be
decompiled back into Python source code by standard decompilers.
"""

import glob
import os
import shutil
import sys
from setuptools import Extension, setup
from Cython.Build import cythonize


def compile_modules():
    print("=" * 65)
    print(" Compiling Python Source Code to Native Machine Code (Cython)")
    print("=" * 65)

    modules_to_compile = ["facebook_scraper.py", "gui.py", "main.py"]

    for mod in modules_to_compile:
        if not os.path.exists(mod):
            print(f"[ERROR] Source file not found: {mod}")
            sys.exit(1)

    extensions = [
        Extension(
            name=os.path.splitext(mod)[0],
            sources=[mod],
            extra_compile_args=["-O3"] if sys.platform != "win32" else ["/O2"],
        )
        for mod in modules_to_compile
    ]

    # Cythonize with performance & stripping optimizations
    ext_modules = cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "always_allow_keywords": True,
            "cdivision": True,
            "embedsignature": False,  # Strip Python function signatures from docstrings
        },
        quiet=False,
    )

    # Build in-place
    sys.argv = [sys.argv[0], "build_ext", "--inplace"]
    setup(ext_modules=ext_modules)

    print("\n[OK] Native C modules built successfully.")

    # Create launch stubs if they don't exist
    os.makedirs("build_stubs", exist_ok=True)

    with open(os.path.join("build_stubs", "launch_gui.py"), "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\nimport gui\nif __name__ == '__main__':\n    gui.main()\n")

    with open(os.path.join("build_stubs", "launch_cli.py"), "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\nimport main\nif __name__ == '__main__':\n    main.main()\n")

    print("[OK] Binary launcher stubs prepared in 'build_stubs/'.")
    print("=" * 65)


if __name__ == "__main__":
    compile_modules()
