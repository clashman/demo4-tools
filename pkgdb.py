"""Offline package database.

Stores the parts of UT2004 packages that the demo parser needs (name, import
and export tables, generation counts and the parsed field metadata of
classes, properties, functions and enums) in a single gzip-compressed JSON
file. With it, demos can be parsed without a UT2004 installation.

Build or extend the database with a UT2004 installation:
    python pkgdb.py --ut UT2004 *.demo4          # packages used by the demos
    python pkgdb.py --ut UT2004 --all            # every package of the install
"""
import argparse
import gzip
import json
from pathlib import Path

from upkg import Export, Package, PackageIndex, PROPERTY_CLASSES

DEFAULT_DB = Path(__file__).resolve().with_name('ut2004_packages.json.gz')

# export classes whose field metadata is needed for replication
# (all UField subclasses: constants are part of the field chains, too)
FIELD_CLASSES = set(PROPERTY_CLASSES) | {'Class', 'Struct', 'Function',
                                         'State', 'Enum', 'Const'}


def package_to_record(pkg):
    """Serialize a loaded Package into a JSON-compatible dict."""
    name_ids = {n: i for i, n in enumerate(pkg.names)}
    exports = []
    for e in pkg.exports:
        row = [e.cls, e.outer, name_ids[e.name]]
        if pkg.export_class_name(e) in FIELD_CLASSES:
            pkg.parse_export(e)
            row += [e.field_super, e.field_next, e.children, e.array_dim,
                    e.prop_flags, e.inner, e.func_flags, e.enum_names]
        exports.append(row)
    return {
        'name': pkg.name,
        'guid': pkg.guid.hex(),
        'generations': pkg.generations,
        'names': pkg.names,
        'imports': pkg.imports,
        'exports': exports,
    }


class CachedPackage(Package):
    """A Package restored from the database (no file data)."""

    def __init__(self, rec):
        self.path = None
        self.data = None
        self.name = rec['name']
        self.guid = bytes.fromhex(rec['guid'])
        self.generations = [tuple(g) for g in rec['generations']]
        self.names = rec['names']
        self.imports = [tuple(i) for i in rec['imports']]
        self.exports = []
        for i, row in enumerate(rec['exports']):
            e = Export()
            e.index = i
            e.pkg = self
            e.cls, e.outer = row[0], row[1]
            e.name = self.names[row[2]]
            e.sup = e.flags = e.size = e.offset = 0
            e._parsed = True
            if len(row) > 3:
                (e.field_super, e.field_next, e.children, e.array_dim,
                 e.prop_flags, e.inner, e.func_flags, e.enum_names) = row[3:]
            else:
                e.field_super = e.field_next = e.children = 0
                e.array_dim = 1
                e.prop_flags = e.inner = e.func_flags = 0
                e.enum_names = None
            self.exports.append(e)

    def parse_export(self, e):
        return e  # metadata was restored from the database


class PackageDB:
    """Drop-in replacement for PackageIndex backed by the database file."""

    def __init__(self, path=DEFAULT_DB):
        self.path = path
        self.records = {}  # name lower -> {guid hex: record}
        if Path(path).exists():
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                for rec in json.load(f)['packages']:
                    self.add_record(rec)
        self.cache = {}

    def add_record(self, rec):
        self.records.setdefault(rec['name'].lower(), {})[rec['guid']] = rec

    def get(self, name, guid=None):
        versions = self.records.get(name.lower())
        if not versions:
            return None
        if guid is not None:
            rec = versions.get(guid.hex())
            if rec is None:
                return None
        else:
            rec = next(iter(versions.values()))
        key = (rec['name'].lower(), rec['guid'])
        if key not in self.cache:
            self.cache[key] = CachedPackage(rec)
        return self.cache[key]

    def save(self, path=None):
        recs = [r for versions in self.records.values() for r in versions.values()]
        recs.sort(key=lambda r: (r['name'].lower(), r['guid']))
        with gzip.open(path or self.path, 'wt', encoding='utf-8') as f:
            json.dump({'version': 1, 'packages': recs}, f, separators=(',', ':'))


def build_db(ut_dir, demo_paths=(), all_packages=False, db_path=DEFAULT_DB):
    """Add packages from a UT2004 installation to the database.

    With `demo_paths`, the packages referenced by these demos are added (plus
    the packages they import from, so that class hierarchies resolve);
    with `all_packages`, every package of the installation.
    """
    from demo import read_header
    idx = PackageIndex({Path(ut_dir)})
    db = PackageDB(db_path)
    if all_packages:
        todo = list(idx.files)
    else:
        todo = []
        for path in demo_paths:
            with Path(path).open('rb') as f:
                hdr = read_header(f.read())
            todo += [name for name, _, _ in hdr['packages']]
    added = []
    missing = set()
    seen = set()
    while todo:
        name = todo.pop()
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        try:
            pkg = idx.get(name)
        except Exception as e:  # damaged or foreign file
            print('skipping %s: %s' % (name, e))
            continue
        if pkg is None:
            missing.add(name)
            continue
        if pkg.guid.hex() not in db.records.get(name.lower(), {}):
            db.add_record(package_to_record(pkg))
            added.append(pkg.name)
        # imported packages (e.g. super classes) are needed as well
        for cpkg, cname, outer, oname in pkg.imports:
            if outer == 0 and cname == 'Package':
                todo.append(oname)
        idx.cache.pop(name.lower(), None)  # free the file data
    db.save(db_path)
    return added, sorted(missing)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('demos', nargs='*', help='demos whose packages are added')
    ap.add_argument('--ut', required=True, type=Path,
                    help='UT2004 installation directory')
    ap.add_argument('--db', default=DEFAULT_DB, type=Path, help='database file')
    ap.add_argument('--all', action='store_true', help='add every package')
    args = ap.parse_args(argv)
    if not args.ut.is_dir():
        ap.error('--ut must point to an existing directory: %s' % args.ut)
    if not args.demos and not args.all:
        ap.error('give demo files or --all')
    added, missing = build_db(args.ut, args.demos, args.all, args.db)
    print('added %d packages to %s (%.1f MB)' % (
        len(added), args.db, args.db.stat().st_size / 1e6))
    if missing:
        print('not in the installation: %s' % ', '.join(missing))


if __name__ == '__main__':
    main()
