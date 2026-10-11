"""Launch UT2004 and play a demo through an EXEC command file."""
import argparse
import os
import subprocess
from pathlib import Path

from lib.utpath import find_ut2004, find_ut2004_userdir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('demo', type=Path, help='path to a demo file')
    args = parser.parse_args(argv)

    if not args.demo.is_file():
        parser.error('demo file does not exist: %s' % args.demo)

    install_dir = find_ut2004()
    user_dir = find_ut2004_userdir()
    if not install_dir or not user_dir:
        parser.error('UT2004 System directory not found')

    executable = install_dir / 'System' / 'UT2004'

    # if not executable.is_file():
    #     parser.error('UT2004 executable not found: %s' % executable)

    demo_path = Path(os.path.relpath(args.demo.resolve(), user_dir / 'Demos'))
    demo_path = demo_path.with_suffix('').as_posix()
    script = user_dir / 'System' / 'mwatch.txt'
    script.write_text('demoplay "%s"' % demo_path, encoding='utf-8')
    subprocess.run([executable, f'EXEC={script.name}', ], check=False)
    script.unlink()

if __name__ == '__main__':
    main()
