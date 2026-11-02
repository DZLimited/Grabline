# PyInstaller build for Grabline.exe. Run: pyinstaller grabline.spec
# Expects vendor/node.exe, which yt-dlp needs for YouTube.
import os

# PyInstaller's Django hook loads the settings while building.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "downloader.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "build-time-only")

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = [("core/templates", "core/templates")]
binaries = [("vendor/node.exe", ".")]
hiddenimports = (
    collect_submodules("core")
    + collect_submodules("downloader")
    + collect_submodules("django")
    + collect_submodules("yt_dlp")
)
datas += collect_data_files("django")
datas += collect_data_files("imageio_ffmpeg")

for package in ("curl_cffi", "yt_dlp_ejs"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

a = Analysis(
    ["grabline_app.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Grabline",
    console=True,
)
