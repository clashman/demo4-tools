"""Minimal Unreal Engine 2 (UT2004, package version 128) package reader.

Reads name/import/export tables and generation info, and extracts the bits of
class/property/function metadata needed to decode replicated network data.
"""
import struct
from pathlib import Path


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from('<H', self.d, self.p)[0]
        self.p += 2
        return v

    def i32(self):
        v = struct.unpack_from('<i', self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from('<I', self.d, self.p)[0]
        self.p += 4
        return v

    def ci(self):
        """Compact index."""
        b = self.u8()
        neg = b & 0x80
        v = b & 0x3F
        if b & 0x40:
            sh = 6
            while True:
                b = self.u8()
                v |= (b & 0x7F) << sh
                sh += 7
                if not (b & 0x80) or sh > 27:
                    break
        return -v if neg else v

    def fstring(self):
        n = self.ci()
        if n == 0:
            return ''
        if n > 0:
            s = self.d[self.p:self.p + n - 1].decode('latin1')
            self.p += n
        else:
            n = -n
            s = self.d[self.p:self.p + 2 * (n - 1)].decode('utf-16-le')
            self.p += 2 * n
        return s


# Property flags
CPF_Parm = 0x00000080
CPF_Net = 0x00000020
CPF_ReturnParm = 0x00000400
# Function flags
FUNC_Net = 0x00000040

PROPERTY_CLASSES = {
    'ByteProperty', 'IntProperty', 'BoolProperty', 'FloatProperty',
    'ObjectProperty', 'ClassProperty', 'NameProperty', 'StrProperty',
    'StructProperty', 'ArrayProperty', 'FixedArrayProperty', 'MapProperty',
    'DelegateProperty', 'PointerProperty', 'StringProperty',
}


class Export:
    __slots__ = ('index', 'cls', 'sup', 'outer', 'name', 'flags', 'size',
                 'offset', 'pkg', '_parsed', 'field_super', 'field_next',
                 'children', 'array_dim', 'prop_flags', 'inner', 'func_flags',
                 'enum_names')

    def __init__(self):
        self._parsed = False


class Package:
    def __init__(self, path):
        path = Path(path)
        self.path = path
        self.name = path.stem
        with path.open('rb') as f:
            self.data = f.read()
        r = Reader(self.data)
        tag = r.u32()
        assert tag == 0x9E2A83C1, path
        self.version = r.u16()
        self.licensee = r.u16()
        self.flags = r.u32()
        name_count, name_off = r.u32(), r.u32()
        exp_count, exp_off = r.u32(), r.u32()
        imp_count, imp_off = r.u32(), r.u32()
        self.guid = self.data[r.p:r.p + 16]
        r.p += 16
        gen_count = r.u32()
        self.generations = []
        for _ in range(gen_count):
            self.generations.append((r.u32(), r.u32()))  # (exports, names)

        r.p = name_off
        self.names = []
        for _ in range(name_count):
            self.names.append(r.fstring())
            r.u32()

        r.p = imp_off
        self.imports = []
        for _ in range(imp_count):
            cpkg = self.names[r.ci()]
            cname = self.names[r.ci()]
            outer = r.i32()
            oname = self.names[r.ci()]
            self.imports.append((cpkg, cname, outer, oname))

        r.p = exp_off
        self.exports = []
        for i in range(exp_count):
            e = Export()
            e.index = i
            e.pkg = self
            e.cls = r.ci()
            e.sup = r.ci()
            e.outer = r.i32()
            e.name = self.names[r.ci()]
            e.flags = r.u32()
            e.size = r.ci()
            e.offset = r.ci() if e.size > 0 else 0
            self.exports.append(e)

    # --- references -------------------------------------------------------
    def exports_by_name(self, name):
        if not hasattr(self, '_by_name'):
            self._by_name = {}
            for e in self.exports:
                self._by_name.setdefault(e.name.lower(), []).append(e)
        return self._by_name.get(name.lower(), [])

    def ref_name(self, ref):
        """Return (class_name, object_name, path) for an object reference."""
        if ref == 0:
            return None
        if ref > 0:
            e = self.exports[ref - 1]
            return self.export_class_name(e), e.name
        imp = self.imports[-ref - 1]
        return imp[1], imp[3]

    def export_class_name(self, e):
        if e.cls == 0:
            return 'Class'
        return self.ref_name(e.cls)[1]

    def ref_path(self, ref):
        """Full dotted path of a reference, e.g. 'Engine.Actor.Location'."""
        if ref == 0:
            return None
        if ref > 0:
            e = self.exports[ref - 1]
            parts = [e.name]
            o = e.outer
            while o:
                oe = self.exports[o - 1]
                parts.append(oe.name)
                o = oe.outer
            parts.append(self.name)
            return '.'.join(reversed(parts))
        parts = []
        idx = ref
        while idx:
            imp = self.imports[-idx - 1]
            parts.append(imp[3])
            idx = imp[2]
            if idx > 0:  # outer is an export (rare)
                parts.append(self.ref_path(idx))
                break
        return '.'.join(reversed(parts))

    def export_path(self, e):
        return self.ref_path(e.index + 1)

    # --- object data ------------------------------------------------------
    def _skip_tagged_props(self, r):
        """Skip a tagged property list (terminated by 'None')."""
        while True:
            ni = r.ci()
            name = self.names[ni]
            if name == 'None':
                return
            info = r.u8()
            ptype = info & 0x0F
            size_code = (info >> 4) & 0x07
            is_array = info & 0x80
            if ptype == 10:  # struct
                r.ci()
            size = [1, 2, 4, 12, 16, None, None, None][size_code]
            if size_code == 5:
                size = r.u8()
            elif size_code == 6:
                size = r.u16()
            elif size_code == 7:
                size = r.u32()
            if is_array and ptype != 3:
                b = r.u8()
                if b & 0x80:
                    if b & 0x40:
                        r.p += 3
                    else:
                        r.p += 1
            if ptype == 3:  # bool: value in is_array bit
                size = 0
            r.p += size

    def parse_export(self, e):
        """Parse field metadata for Struct/Function/Class/State/Enum/Property."""
        if e._parsed:
            return e
        e._parsed = True
        e.field_super = e.field_next = e.children = 0
        e.array_dim = 1
        e.prop_flags = 0
        e.inner = 0
        e.func_flags = 0
        e.enum_names = None
        cname = self.export_class_name(e)
        if e.size <= 0:
            return e
        r = Reader(self.data, e.offset)
        end = e.offset + e.size
        if e.flags & 0x02000000:  # RF_HasStack
            r.ci()
            r.ci()
            r.p += 8 + 4
            # Node != 0 -> latent action
            # (classes/functions never have stack state in practice)
        if cname in ('Class',):
            # UClass: UState->UStruct->UField, but the object has no tagged
            # properties before field data (defaults are at the end).
            pass
        else:
            self._skip_tagged_props(r)
        e.field_super = r.ci()
        e.field_next = r.ci()
        if cname in PROPERTY_CLASSES:
            e.array_dim = r.u16()
            r.u16()  # ElementSize
            e.prop_flags = r.u32()
            r.ci()  # Category
            if e.prop_flags & CPF_Net:
                r.u16()  # RepOffset
            if cname in ('ByteProperty', 'ObjectProperty', 'StructProperty',
                         'ArrayProperty', 'DelegateProperty',
                         'FixedArrayProperty'):
                e.inner = r.ci()
            elif cname == 'ClassProperty':
                e.inner = r.ci()  # PropertyClass (Class)
                r.ci()            # MetaClass
                # store meta in children slot for reference
            elif cname == 'MapProperty':
                e.inner = r.ci()
        elif cname == 'Enum':
            n = r.ci()
            e.enum_names = [self.names[r.ci()] for _ in range(n)]
        elif cname in ('Struct', 'Function', 'State', 'Class'):
            r.ci()  # ScriptText
            e.children = r.ci()
            if cname == 'Function':
                # Tail of UFunction: ... FunctionFlags [RepOffset if FUNC_Net]
                f6 = struct.unpack_from('<I', self.data, end - 6)[0]
                f4 = struct.unpack_from('<I', self.data, end - 4)[0]
                if f6 & FUNC_Net:
                    e.func_flags = f6
                else:
                    e.func_flags = f4 & ~FUNC_Net
        return e


class PackageIndex:
    """Locate packages in a UT2004 installation."""

    def __init__(self, root):
        self.files = {}
        for sub in ('System', 'Maps', 'Textures', 'Sounds', 'StaticMeshes',
                    'Animations', 'Music', 'Prefabs', 'KarmaData'):
            d = root / sub
            if not d.is_dir():
                continue
            for path in d.iterdir():
                if path.suffix.lower() in ('.u', '.ut2', '.utx', '.uax',
                                            '.usx', '.ukx', '.umx', '.uvx'):
                    self.files.setdefault(path.stem.lower(), path)
        self.cache = {}

    def get(self, name, guid=None):
        # guid is accepted for interface compatibility with pkgdb.PackageDB;
        # PackageMap compares it itself
        key = name.lower()
        if key in self.cache:
            return self.cache[key]
        path = self.files.get(key)
        pkg = Package(path) if path else None
        self.cache[key] = pkg
        return pkg
