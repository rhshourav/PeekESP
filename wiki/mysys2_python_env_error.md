# quickstart.py fails with `FileNotFoundError: [WinError 2]` on MSYS2 Python

## Symptom

Running `python .\quickstart.py` on Windows fails in both steps. The app build and the board flash each crash with the same error right after `creating .venv ...`:

```
creating .venv ...
Traceback (most recent call last):
  File "D:\GITHUB\PeekESP\windows\build.py", line 117, in main
    ensure_pyinstaller(py)
  File "D:\GITHUB\PeekESP\windows\build.py", line 66, in ensure_pyinstaller
    r = subprocess.run([str(py), "-c", "import PyInstaller"], capture_output=True)
  ...
  File "C:\msys64\ucrt64\lib\python3.14\subprocess.py", line 1553, in _execute_child
    hp, ht, pid, tid = _winapi.CreateProcess(executable, args,
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

`tools\flash.py` fails the same way at `subprocess.run([str(py), "-c", "import serial.tools.list_ports"], ...)`.

Telltale sign: the traceback paths point at `C:\msys64\ucrt64\lib\python3.x`.

## Cause

The `python` on PATH was MSYS2's Python (`C:\msys64\ucrt64\bin\python.exe`), not a native Windows build.

Confirm with:

```powershell
python -c "import sys; print(sys.executable, sys.base_prefix)"
# C:\msys64\ucrt64\bin\python.exe C:\msys64\ucrt64
```

`build.py` and `flash.py` create a virtual environment and then launch the interpreter inside it, expecting the standard Windows layout (`.venv\Scripts\python.exe`). MSYS2's Python builds venvs POSIX-style (`.venv/bin/python.exe`), so the path the scripts launch doesn't exist and `CreateProcess` raises `WinError 2`.

MSYS2 Python is also a poor base for PyInstaller and the tray-app dependencies, so a native Python is the right interpreter for this project regardless.

## Fix

1. Install a native Windows Python (python.org installer, or winget):

   ```powershell
   winget install Python.Python.3.12
   ```

2. Open a **new** terminal so the `py` launcher is on PATH.

3. Delete the broken venvs left by the failed run:

   ```powershell
   Remove-Item -Recurse -Force .\windows\.venv, .\tools\.venv
   ```

4. Run quickstart through the launcher so it can't pick up MSYS2:

   ```powershell
   py -3.12 .\quickstart.py
   ```

Result: the venvs are created with `Scripts\python.exe`, and both steps run. This resolved the issue.

## Notes

- The Python version (3.14) was not the cause. The problem was which Python distribution ran the scripts.
- `C:\msys64\ucrt64\bin` was ahead of the python.org install in PATH, which is why plain `python` resolved to MSYS2. Using `py -3.12` avoids depending on PATH order.

## Optional hardening

Make the scripts tolerate either venv layout. In `build.py` and `flash.py`:

```python
from pathlib import Path

def venv_python(venv: Path) -> Path:
    for sub in ("Scripts", "bin"):
        for name in ("python.exe", "python"):
            p = venv / sub / name
            if p.exists():
                return p
    raise SystemExit(f"no python found inside {venv} - delete it and retry")
```

And fail early with a clear message in `quickstart.py`:

```python
if "msys" in sys.base_prefix.lower() or "mingw" in sys.version.lower():
    sys.exit("This looks like MSYS2 Python. Use python.org Python (py -3.12 quickstart.py).")
```
