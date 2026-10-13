"""Write version_info.txt for PyInstaller (--version-file) from VERSION in lap_timer.py.

This is what shows in the Windows exe's Properties > Details tab. Run by build_windows.bat and by the GitHub Actions build."""
import re
from pathlib import Path

here = Path(__file__).resolve().parent
version = re.search(r'^VERSION\s*=\s*"([^"]+)"', (here / "lap_timer.py").read_text(encoding="utf-8"), re.M).group(1)
parts = [int(p) for p in re.findall(r"\d+", version)][:4]
nums = tuple(parts + [0] * (4 - len(parts)))
dotted = ".".join(map(str, nums))

(here / "version_info.txt").write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums},
    prodvers={nums},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', ''),
         StringStruct('FileDescription', 'FPV Lap Timer'),
         StringStruct('FileVersion', '{dotted}'),
         StringStruct('InternalName', 'FPVLapTimer'),
         StringStruct('OriginalFilename', 'FPVLapTimer.exe'),
         StringStruct('ProductName', 'FPV Lap Timer'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print(f"version_info.txt written for {version}")
