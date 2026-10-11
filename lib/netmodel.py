"""Class / property network layout for UE2 (UT2004) replication."""
from .upkg import CPF_Net, CPF_Parm, CPF_ReturnParm, FUNC_Net, PROPERTY_CLASSES


def ceil_log2(n):
    return (n - 1).bit_length() if n > 1 else 0


class UnknownObject:
    def __init__(self, pkgname, local_index):
        self.pkgname = pkgname
        self.local_index = local_index

    def __repr__(self):
        return '<?%s:%d>' % (self.pkgname, self.local_index)


class ObjRef:
    """A reference to an export of a known package."""
    __slots__ = ('pkg', 'exp')

    def __init__(self, pkg, exp):
        self.pkg = pkg
        self.exp = exp

    @property
    def name(self):
        return self.exp.name

    @property
    def class_name(self):
        return self.pkg.export_class_name(self.exp)

    @property
    def path(self):
        return self.pkg.export_path(self.exp)

    def __repr__(self):
        return self.path


class NetField:
    __slots__ = ('index', 'pkg', 'exp', 'is_func', 'name', 'params', 'ptype',
                 'opaque_bits')

    def __init__(self):
        self.opaque_bits = None


class NetClass:
    def __init__(self, model, pkg, exp):
        self.pkg = pkg
        self.exp = exp
        self.name = exp.name
        self.path = pkg.export_path(exp)
        self.super = None
        self.fields_base = 0
        self.fields = []
        pkg.parse_export(exp)
        if exp.field_super:
            sref = model.resolve_ref(pkg, exp.field_super)
            if sref is not None:
                self.super = model.net_class(sref.pkg, sref.exp)
                self.fields_base = self.super.max_index
        own = []
        c = exp.children
        guard = 0
        while c and guard < 5000:
            fe = pkg.exports[c - 1]
            pkg.parse_export(fe)
            cn = pkg.export_class_name(fe)
            if cn in PROPERTY_CLASSES:
                if fe.prop_flags & CPF_Net:
                    own.append((fe, False))
            elif cn == 'Function':
                if (fe.func_flags & FUNC_Net) and not fe.field_super:
                    own.append((fe, True))
            c = fe.field_next
            guard += 1
        if model.sort_mode == 'index':
            own.sort(key=lambda t: t[0].index)
        elif model.sort_mode == 'name':
            own.sort(key=lambda t: t[0].name.lower())
        for i, (fe, is_func) in enumerate(own):
            f = NetField()
            f.index = self.fields_base + i
            f.pkg = pkg
            f.exp = fe
            f.is_func = is_func
            f.name = fe.name
            f.params = model.func_params(pkg, fe) if is_func else None
            f.ptype = None if is_func else model.prop_type(pkg, fe)
            self.fields.append(f)
        self.max_index = self.fields_base + len(self.fields)

    def field(self, idx):
        c = self
        while c is not None:
            if idx >= c.fields_base:
                i = idx - c.fields_base
                return c.fields[i] if i < len(c.fields) else None
            c = c.super
        return None

    def is_a(self, name):
        c = self
        while c is not None:
            if c.name == name:
                return True
            c = c.super
        return False

    def __repr__(self):
        return '<NetClass %s>' % self.path


class PropType:
    """Decoded type information for one property."""
    __slots__ = ('kind', 'name', 'array_dim', 'enum_bits', 'struct_name',
                 'struct_fields')

    def __init__(self):
        self.enum_bits = 8
        self.struct_name = None
        self.struct_fields = None


class Model:
    def __init__(self, pkgindex, sort_mode='index'):
        self.idx = pkgindex
        self.sort_mode = sort_mode
        self._classes = {}
        self._ptypes = {}

    def resolve_ref(self, pkg, ref):
        """Resolve a package-local reference to an ObjRef in a known package."""
        if ref == 0:
            return None
        if ref > 0:
            return ObjRef(pkg, pkg.exports[ref - 1])
        # import: find the owning package and the export
        chain = []
        i = ref
        while i < 0:
            imp = pkg.imports[-i - 1]
            chain.append(imp)
            i = imp[2]
        chain.reverse()  # [package, ..., object]
        tpkg = self.idx.get(chain[0][3])
        if tpkg is None:
            return None
        outer = 0
        exp = None
        for imp in chain[1:]:
            cname, oname = imp[1], imp[3]
            exp = None
            for e in tpkg.exports_by_name(oname):
                if e.outer == outer and tpkg.export_class_name(e) == cname:
                    exp = e
                    break
            if exp is None:
                for e in tpkg.exports_by_name(oname):
                    if e.outer == outer:
                        exp = e
                        break
            if exp is None:
                return None
            outer = exp.index + 1
        return ObjRef(tpkg, exp) if exp is not None else None

    def net_class(self, pkg, exp):
        key = (pkg.name, exp.index)
        nc = self._classes.get(key)
        if nc is None:
            nc = NetClass(self, pkg, exp)
            self._classes[key] = nc
        return nc

    def prop_type(self, pkg, fe):
        key = (pkg.name, fe.index)
        t = self._ptypes.get(key)
        if t is not None:
            return t
        pkg.parse_export(fe)
        t = PropType()
        t.kind = pkg.export_class_name(fe)
        t.name = fe.name
        t.array_dim = fe.array_dim
        if t.kind == 'ByteProperty':
            if fe.inner:
                er = self.resolve_ref(pkg, fe.inner)
                if er is not None:
                    er.pkg.parse_export(er.exp)
                    t.enum_bits = ceil_log2(len(er.exp.enum_names))
        elif t.kind == 'StructProperty':
            sr = self.resolve_ref(pkg, fe.inner)
            if sr is not None:
                t.struct_name = sr.exp.name
                if t.struct_name not in ('Vector', 'Rotator'):
                    t.struct_fields = self.struct_props(sr.pkg, sr.exp)
        self._ptypes[key] = t
        return t

    def struct_props(self, pkg, sexp):
        """All properties of a struct in TFieldIterator order."""
        out = []
        pkg.parse_export(sexp)
        c = sexp.children
        while c:
            fe = pkg.exports[c - 1]
            pkg.parse_export(fe)
            if pkg.export_class_name(fe) in PROPERTY_CLASSES:
                out.append(self.prop_type(pkg, fe))
            c = fe.field_next
        if sexp.field_super:
            sr = self.resolve_ref(pkg, sexp.field_super)
            if sr is not None:
                out.extend(self.struct_props(sr.pkg, sr.exp))
        return out

    def func_params(self, pkg, fexp):
        out = []
        c = fexp.children
        while c:
            fe = pkg.exports[c - 1]
            pkg.parse_export(fe)
            if pkg.export_class_name(fe) in PROPERTY_CLASSES:
                if (fe.prop_flags & (CPF_Parm | CPF_ReturnParm)) != CPF_Parm:
                    break
                out.append(self.prop_type(pkg, fe))
            c = fe.field_next
        return out


class VirtualNetClass:
    """Stand-in for a class from a package we do not have.

    It inherits the network layout of a known super class and adds `extra`
    fields whose types are unknown. `opaque` describes some of them:
    {i: nbits} for a field of known fixed size, or {i: (name, [kinds])} for an
    RPC with the given parameter property kinds (e.g. 'IntProperty').
    """

    def __init__(self, name, super_class, extra, opaque=None):
        self.name = name
        self.path = name
        self.super = super_class
        self.fields_base = super_class.max_index
        self.fields = []
        for i in range(extra):
            f = NetField()
            f.index = self.fields_base + i
            f.pkg = f.exp = None
            f.is_func = None
            f.name = '<unknown field %d>' % i
            f.params = f.ptype = None
            spec = opaque.get(i) if opaque else None
            if isinstance(spec, int):
                f.opaque_bits = spec
            elif spec is not None:
                f.name, kinds = spec
                f.is_func = True
                f.params = []
                for n, kind in enumerate(kinds):
                    t = PropType()
                    t.kind = kind
                    t.name = 'Param%d' % n
                    t.array_dim = 1
                    f.params.append(t)
            self.fields.append(f)
        self.max_index = self.fields_base + extra

    field = NetClass.field
    is_a = NetClass.is_a

    def __repr__(self):
        return '<VirtualNetClass %s : %s +%d>' % (self.name, self.super.path,
                                                  len(self.fields))
