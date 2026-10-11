"""Find a UT2004 installation in the platform's standard location."""
import sys
from pathlib import Path


def find_ut2004():
    """Return the detected UT2004 installation path, or None if not found."""
    if sys.platform == 'win32':
        import winreg

        key_path = r'SOFTWARE\Unreal Technology\Installed Apps\UT2004'
        views = (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
        for view in views:
            try:
                with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, key_path,
                        0, winreg.KEY_READ | view) as key:
                    install_dir, _ = winreg.QueryValueEx(key, 'Folder')
            except (OSError, FileNotFoundError):
                continue
            path = Path(install_dir)
            if path.is_dir():
                return path

        # TODO: Also detect UT2004 installed through Steam.
        return None

    if sys.platform.startswith('linux'):
        path = Path('/opt/ut2004')
        if path.is_dir():
            return path
        return None

    return None


def find_ut2004_userdir():
    """Return UT2004's user directory for the current platform."""
    if sys.platform != 'win32':
        return Path.home() / '.ut2004'
    return find_ut2004()


if __name__ == '__main__':
    installation = find_ut2004()
    if installation is None:
        print('UT2004 installation not found', file=sys.stderr)
        raise SystemExit(1)
    print(installation)
