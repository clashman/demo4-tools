"""UT2004 .demo4 parser (UE2 network demo format)."""
import struct

from .bits import BitReader, BitError, packet_bits
from .upkg import Reader
from .netmodel import ObjRef, UnknownObject

MAX_PACKETID = 16384
MAX_CHANNELS = 1023
MAX_CHSEQUENCE = 1024
CHTYPE_MAX = 8
MAX_PACKET = 512

CHTYPE_CONTROL = 1
CHTYPE_ACTOR = 2


class DecodeError(Exception):
    pass


class UnknownFieldError(DecodeError):
    """Reached a replicated field of a class we have no layout for."""


def read_header(data):
    r = Reader(data, 17)
    hdr = {}
    hdr['version'] = r.i32()
    hdr['map'] = r.fstring()
    hdr['gametype'] = r.fstring()
    hdr['unk1'] = r.i32()
    hdr['unk2'] = r.i32()
    hdr['unk3'] = r.i32()
    hdr['recorder'] = r.fstring()
    hdr['date'] = r.fstring()
    n = r.i32()
    pkgs = []
    for _ in range(n):
        name = r.fstring()
        guid = data[r.p:r.p + 16]
        r.p += 16
        gen = r.i32()
        pkgs.append((name, guid, gen))
    hdr['packages'] = pkgs
    hdr['data_start'] = r.p
    return hdr


def iter_frames(data, start):
    p = start
    n = len(data)
    while p + 12 <= n:
        t, fn, ln = struct.unpack_from('<fiI', data, p)
        pk = data[p + 12:p + 12 + ln]
        p += 12 + ln
        yield t, fn, pk


class PackageMap:
    def __init__(self, hdr, pkgindex, unknown_counts=None):
        """unknown_counts: {pkgname_lower: (export_count, name_count)} for
        missing packages, and {'pkgname_lower#gen': (...)} for packages whose
        local copy is older than the generation the server used."""
        self.entries = []  # (name, pkg_or_None, obj_base, obj_count, name_base, name_count)
        ob = nb = 0
        unknown_counts = unknown_counts or {}
        self.missing = []
        self.newer = []  # packages the server had in a newer generation
        for name, guid, gen in hdr['packages']:
            pkg = pkgindex.get(name, guid)
            if pkg is not None and pkg.guid != guid:
                pkg = None
            if pkg is not None:
                gi = min(gen, len(pkg.generations)) - 1
                oc, nc = pkg.generations[gi]
                if gen > len(pkg.generations):
                    key = '%s#%d' % (name.lower(), gen)
                    if key in unknown_counts:
                        oc, nc = unknown_counts[key]
                    else:
                        self.newer.append(key)
            else:
                self.missing.append(name)
                oc, nc = unknown_counts.get(name.lower(), (0, 0))
            self.entries.append((name, pkg, ob, oc, nb, nc))
            ob += oc
            nb += nc
        self.max_object = ob
        self.max_name = nb

    def index_to_object(self, index):
        for name, pkg, ob, oc, nb, nc in self.entries:
            if index < ob + oc:
                if pkg is None or index - ob >= len(pkg.exports):
                    return UnknownObject(name, index - ob)
                return ObjRef(pkg, pkg.exports[index - ob])
        return UnknownObject('?', index)

    def index_to_name(self, index):
        for name, pkg, ob, oc, nb, nc in self.entries:
            if index < nb + nc:
                if pkg is None or index - nb >= len(pkg.names):
                    return '<?name %s:%d>' % (name, index - nb)
                return pkg.names[index - nb]
        return None


class Actor:
    __slots__ = ('chan', 'netclass', 'cls_name', 'props', 'static_ref',
                 'alive', 'id', 'spawn_loc', 'open_time', 'close_time')

    def __init__(self):
        self.props = {}
        self.alive = True
        self.static_ref = None

    def __repr__(self):
        return '<%s#%d ch%d>' % (self.cls_name, self.id, self.chan)


class Channel:
    __slots__ = ('index', 'chtype', 'actor', 'broken')

    def __init__(self, index, chtype):
        self.index = index
        self.chtype = chtype
        self.actor = None
        self.broken = False


def make_relative(value, reference, maxv):
    if value - reference > maxv // 2:
        return value - maxv
    if reference - value > maxv // 2:
        return value + maxv
    return value


class DemoParser:
    def __init__(self, data, model, pmap, listener, rot_classes=None):
        self.data = data
        self.model = model
        self.pmap = pmap
        self.listener = listener
        self.channels = {}
        self.in_reliable = {}
        self.rel_queue = {}
        self.time = 0.0
        self.frame = 0
        self.actor_counter = 0
        self.stats = {'bunches': 0, 'actor_ok': 0, 'actor_fail': 0,
                      'unknown_class': 0, 'skipped': 0}
        self.fail_log = []
        # unknown class key -> (VirtualNetClass, initial rotation sent)
        self.virtual = {}
        self.unknown_sink = None
        # per class decision whether initial rotation is serialized
        self.rot_classes = rot_classes if rot_classes is not None else {}

    @staticmethod
    def virtual_key(obj):
        return '%s:%d' % (obj.pkgname, obj.local_index)

    # --- low level value decoding ----------------------------------------
    def read_object(self, b):
        if b.bit():
            idx = b.read_int(MAX_CHANNELS)
            if idx == 0:
                return None
            ch = self.channels.get(idx)
            if ch is not None and ch.actor is not None:
                return ch.actor
            return ('chan', idx)
        idx = b.read_int(self.pmap.max_object)
        return self.pmap.index_to_object(idx)

    def read_name(self, b):
        idx = b.read_int(self.pmap.max_name + 1)
        if idx < self.pmap.max_name:
            return self.pmap.index_to_name(idx)
        return None

    # Spawn location/rotation of a new actor channel use the compressed
    # FVector/FRotator serialization; replicated vector/rotator properties
    # are plain structs (see read_value).
    @staticmethod
    def read_vector(b):
        bits = b.read_int(20)
        bias = 1 << (bits + 1)
        mx = 1 << (bits + 2)
        x = b.read_int(mx) - bias
        y = b.read_int(mx) - bias
        z = b.read_int(mx) - bias
        return (x, y, z)

    @staticmethod
    def read_rotator(b):
        out = []
        for _ in range(3):
            out.append((b.byte() << 8) if b.bit() else 0)
        return tuple(out)

    def read_value(self, b, t):
        k = t.kind
        if k == 'BoolProperty':
            return bool(b.bit())
        if k == 'ByteProperty':
            return b.bits(t.enum_bits)
        if k == 'IntProperty':
            return b.i32()
        if k == 'FloatProperty':
            return b.f32()
        if k in ('ObjectProperty', 'ClassProperty'):
            return self.read_object(b)
        if k == 'NameProperty':
            return self.read_name(b)
        if k in ('StrProperty', 'StringProperty'):
            return b.fstring()
        if k == 'StructProperty':
            sn = t.struct_name
            if sn == 'Vector':
                return (b.f32(), b.f32(), b.f32())
            if sn == 'Rotator':
                return (b.i32(), b.i32(), b.i32())
            if t.struct_fields is None:
                raise DecodeError('unknown struct %s' % sn)
            out = {}
            for ft in t.struct_fields:
                for i in range(ft.array_dim):
                    v = self.read_value(b, ft)
                    out[ft.name if ft.array_dim == 1 else '%s[%d]' % (ft.name, i)] = v
            return out
        raise DecodeError('cannot decode %s %s' % (k, t.name))

    # --- packet handling ---------------------------------------------------
    def run(self):
        hdr_start = self.listener.hdr['data_start']
        last_fn = None
        for t, fn, pk in iter_frames(self.data, hdr_start):
            # every packet of a frame repeats the frame's delta time
            if fn != last_fn:
                self.time += t
                last_fn = fn
            self.frame = fn
            if not pk:
                continue
            self.listener.on_frame(self)
            self.received_packet(pk)
        self.listener.on_end(self)

    def received_packet(self, pk):
        try:
            r = BitReader(pk, packet_bits(pk))
            r.read_int(MAX_PACKETID)
            while not r.at_end():
                if r.bit():
                    r.read_int(MAX_PACKETID)
                    continue
                ctl = r.bit()
                b_open = b_close = 0
                if ctl:
                    b_open = r.bit()
                    b_close = r.bit()
                rel = r.bit()
                chi = r.read_int(MAX_CHANNELS)
                seq = r.read_int(MAX_CHSEQUENCE) if rel else None
                chtype = r.read_int(CHTYPE_MAX) if (rel or b_open) else None
                nbits = r.read_int(MAX_PACKET * 8)
                bunch = r.sub(nbits)
                self.received_bunch(chi, b_open, b_close, rel, seq, chtype, bunch)
        except BitError:
            self.stats['bad_packets'] = self.stats.get('bad_packets', 0) + 1

    def received_bunch(self, chi, b_open, b_close, rel, seq, chtype, bunch):
        self.stats['bunches'] += 1
        if rel:
            # Reliable sequence numbers are per channel index and survive
            # channel close/reopen.
            inrel = self.in_reliable.get(chi, 0)
            s = make_relative(seq, inrel, MAX_CHSEQUENCE)
            if s <= inrel:
                return  # duplicate
            queue = self.rel_queue.setdefault(chi, {})
            if s != inrel + 1:
                queue[s] = (b_open, b_close, chtype, bunch)
                return
            self.in_reliable[chi] = s
            self.deliver(chi, b_open, b_close, chtype, bunch)
            while (self.in_reliable[chi] + 1) in queue:
                s2 = self.in_reliable[chi] + 1
                bo, bc, ct, bb = queue.pop(s2)
                self.in_reliable[chi] = s2
                self.deliver(chi, bo, bc, ct, bb)
        else:
            self.deliver(chi, b_open, b_close, chtype, bunch)

    def deliver(self, chi, b_open, b_close, chtype, bunch):
        ch = self.channels.get(chi)
        if ch is None:
            if not b_open:
                return  # data for a channel we do not have (yet)
            ch = Channel(chi, chtype)
            self.channels[chi] = ch
        self.dispatch(ch, b_open, b_close, bunch)

    def dispatch(self, ch, b_open, b_close, bunch):
        if ch.chtype == CHTYPE_ACTOR:
            self.actor_bunch(ch, b_open, bunch)
        elif ch.chtype == CHTYPE_CONTROL:
            try:
                while not bunch.at_end():
                    self.listener.on_control(self, bunch.fstring())
            except BitError:
                pass
        if b_close:
            if ch.actor is not None:
                ch.actor.alive = False
                ch.actor.close_time = self.time
                self.listener.on_actor_close(self, ch.actor)
            if self.channels.get(ch.index) is ch:
                del self.channels[ch.index]

    def actor_bunch(self, ch, b_open, b):
        start = b.pos
        if ch.actor is None:
            if ch.broken or not b_open:
                self.stats['skipped'] += 1
                return
            try:
                obj = self.read_object(b)
            except BitError:
                ch.broken = True
                return
            a = Actor()
            a.chan = ch.index
            a.id = self.actor_counter
            self.actor_counter += 1
            a.open_time = a.close_time = self.time
            a.spawn_loc = None
            if isinstance(obj, ObjRef) and obj.class_name == 'Class':
                nc = self.model.net_class(obj.pkg, obj.exp)
                a.netclass = nc
                a.cls_name = nc.name
                pos = b.pos
                choice = self.rot_classes.get(nc.path)
                if choice is None:
                    # projectiles replicate their initial rotation
                    choice = nc.is_a('Projectile')
                options = [choice, not choice]
                ok = False
                for withrot in options:
                    b.pos = pos
                    try:
                        a.spawn_loc = self.read_vector(b)
                        if withrot:
                            self.read_rotator(b)
                        props = self.decode_props(nc, b, a, dry=True)
                    except (BitError, DecodeError):
                        continue
                    if props is not None:
                        b.pos = pos
                        a.spawn_loc = self.read_vector(b)
                        if withrot:
                            self.read_rotator(b)
                        self.rot_classes[nc.path] = withrot
                        ok = True
                        break
                if not ok:
                    ch.broken = True
                    self.stats['actor_fail'] += 1
                    self.fail_log.append((self.time, 'spawn', nc.path))
                    return
            elif isinstance(obj, ObjRef):
                # static (level placed) actor
                cref = self.model.resolve_ref(obj.pkg, obj.exp.cls)
                if cref is None:
                    ch.broken = True
                    self.stats['unknown_class'] += 1
                    return
                nc = self.model.net_class(cref.pkg, cref.exp)
                a.netclass = nc
                a.cls_name = nc.name
                a.static_ref = obj
            elif isinstance(obj, UnknownObject) and                     self.virtual_key(obj) in self.virtual:
                key = self.virtual_key(obj)
                nc, withrot = self.virtual[key]
                a.netclass = nc
                a.cls_name = nc.name
                try:
                    a.spawn_loc = self.read_vector(b)
                    if withrot:
                        self.read_rotator(b)
                except BitError:
                    ch.broken = True
                    return
            else:
                ch.broken = True
                self.stats['unknown_class'] += 1
                if self.unknown_sink is not None and isinstance(obj, UnknownObject):
                    self.unknown_sink(self, ch, self.virtual_key(obj), b)
                self.fail_log.append((self.time, 'unknown', repr(obj)))
                return
            ch.actor = a
            self.listener.on_actor_open(self, a)
        a = ch.actor
        try:
            self.decode_props(a.netclass, b, a, dry=False)
            self.stats['actor_ok'] += 1
        except UnknownFieldError:
            self.stats['actor_partial'] = self.stats.get('actor_partial', 0) + 1
        except (BitError, DecodeError) as e:
            self.stats['actor_fail'] += 1
            if len(self.fail_log) < 2000:
                self.fail_log.append((self.time, a.cls_name, str(e),
                                      b.num - start, self.last_partial,
                                      self.last_field))

    def decode_props(self, nc, b, actor, dry):
        """Decode property/RPC stream until end of bunch."""
        updates = []
        self.last_partial = updates
        try:
            self._decode_props(nc, b, updates)
        except UnknownFieldError:
            if not dry:
                self.apply_updates(actor, updates)
            raise
        if not dry:
            self.apply_updates(actor, updates)
        return updates

    def _decode_props(self, nc, b, updates):
        while not b.at_end():
            idx = b.read_int(nc.max_index)
            f = nc.field(idx)
            self.last_field = (idx, f.name if f else None, b.num - b.pos)
            if f is None:
                raise DecodeError('bad field index %d/%d' % (idx, nc.max_index))
            if f.is_func is None:
                if f.opaque_bits is None:
                    raise UnknownFieldError(f.name)
                b.pos += f.opaque_bits  # known size, unknown meaning
                if b.pos > b.num:
                    raise BitError('read past end')
                continue
            if not f.is_func:
                t = f.ptype
                elem = b.byte() if t.array_dim != 1 else 0
                v = self.read_value(b, t)
                updates.append((f.name, elem, v))
            else:
                args = {}
                for pt in f.params:
                    if pt.kind == 'BoolProperty' or b.bit():
                        if pt.array_dim == 1:
                            args[pt.name] = self.read_value(b, pt)
                        else:
                            args[pt.name] = [self.read_value(b, pt) for _ in range(pt.array_dim)]
                updates.append((f.name, None, args))

    def apply_updates(self, actor, updates):
        for name, elem, v in updates:
            if elem is None:
                self.listener.on_rpc(self, actor, name, v)
            else:
                key = name if elem == 0 else '%s[%d]' % (name, elem)
                old = actor.props.get(key)
                actor.props[key] = v
                self.listener.on_prop(self, actor, key, v, old)
