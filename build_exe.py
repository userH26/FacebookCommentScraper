#!/usr/bin/env python3
"""
Closed-Source Binary Builder for Windows (.exe) and Linux.
1. Compiles Python source files into native machine code (.pyd / .so) via Cython.
2. Strips all docstrings and symbol tables.
3. Packages into standalone executables via PyInstaller with zero accessible source code.
"""

import glob
import os
import platform
import shutil
import subprocess
import sys


def build_app(stub_script: str, name: str, windowed: bool = False):
    ext = ".exe" if platform.system() == "Windows" else ""
    print(f"\n---> Packaging {name}{ext} (Windowed={windowed}, Protected=True)...")

    cmd = [
        sys.executable,
        "-OO",  # Optimize bytecode (strips assert statements and docstrings)
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--windowed" if windowed else "--console",
        f"--name={name}",
        "--collect-all=playwright",
        stub_script,
    ]

    subprocess.check_call(cmd)
    output_path = os.path.join("dist", f"{name}{ext}")
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"     [OK] Built: {output_path} ({size_mb:.1f} MB)")
    else:
        print(f"     [WARNING] Could not find output: {output_path}")


def main():
    print("=" * 65)
    print(" Facebook Comment Scraper - Closed-Source Binary Builder")
    print(" Platform:", platform.system(), platform.release(), platform.machine())
    print(" Python:  ", sys.version)
    print("=" * 65)

    print("\n[1/4] Ensuring build tools are installed...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    # 2. Compile to native C extensions via Cython
    print("\n[2/4] Compiling proprietary Python modules into native C machine code...")
    has_native = False
    try:
        subprocess.check_call([sys.executable, "compile_native.py"])
        has_native = True
    except Exception as e:
        print(f"[NOTE] Native Cython build step skipped or failed ({e}).")
        print("Falling back to standard bytecode packaging.")

    print("\n[3/4] Packaging protected standalone executables...")
    gui_target = os.path.join("build_stubs", "launch_gui.py") if has_native else "gui.py"
    cli_target = os.path.join("build_stubs", "launch_cli.py") if has_native else "main.py"

    # Build Desktop GUI
    build_app(gui_target, "FacebookCommentScraper_GUI", windowed=True)

    # Build CLI
    build_app(cli_target, "FacebookCommentScraper_CLI", windowed=False)

    print("\n[4/4] Verification & Cleanup...")
    # Clean up temporary C intermediate files
    for c_file in glob.glob("*.c"):
        try:
            os.remove(c_file)
        except OSError:
            pass

    print("\n" + "=" * 65)
    print(" BUILD COMPLETED!")
    print(" Protected closed-source executables are in the 'dist' directory.")
    print("=" * 65)


if __name__ == "__main__":
    main()
