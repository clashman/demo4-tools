"""Infer network layouts for actor classes from packages that are missing.

For every unknown class referenced by a demo, all of its bunches are collected
and decoded under different hypotheses (known super class + number of extra
replicated fields + whether an initial rotation is sent). The hypothesis that
decodes the most bunches cleanly wins.
"""
import sys
from pathlib import Path

from lib.bits import BitReader, BitError
from lib.demo import (DemoParser, DecodeError, UnknownFieldError, PackageMap,
                      read_header)
from lib.netmodel import Model, VirtualNetClass

CANDIDATE_SUPERS = [
    'XGame.xPawn', 'XGame.xPlayer', 'Engine.PlayerController',
    'XGame.xPlayerReplicationInfo', 'Engine.PlayerReplicationInfo',
    'Engine.ReplicationInfo', 'Engine.Info', 'Engine.Actor',
    'Engine.LinkedReplicationInfo', 'Engine.Emitter', 'Engine.Projectile',
    'Engine.Effects', 'Engine.Inventory', 'Engine.Weapon',
    'Engine.Mutator', 'Engine.GameReplicationInfo', 'Engine.Pawn',
    'Engine.xEmitter', 'Engine.Pickup', 'XEffects.xEmitter',
    'Engine.WeaponAttachment', 'Engine.InventoryAttachment',
]


def find_class(model, path):
    pkgname, cname = path.split('.')
    pkg = model.idx.get(pkgname)
    if pkg is None:
        return None
    for e in pkg.exports_by_name(cname):
        if pkg.export_class_name(e) == 'Class':
            return model.net_class(pkg, e)
    return None


class _Null:
    def __init__(self, hdr):
        self.hdr = hdr

    def __getattr__(self, n):
        return lambda *a: None


def collect(data, hdr, pmap, model):
    """Return {key: [(is_open, BitReader-snapshot), ...]}."""
    store = {}
    chan_key = {}
    p = DemoParser(data, model, pmap, _Null(hdr))

    def sink(parser, ch, key, b):
        chan_key[ch.index] = (ch, key)
        store.setdefault(key, []).append((True, BitReader(b.buf, b.num, b.pos)))

    p.unknown_sink = sink
    orig = p.actor_bunch

    def actor_bunch(ch, b_open, b):
        ck = chan_key.get(ch.index)
        if ck is not None and ck[0] is ch and ch.broken and not b_open:
            store[ck[1]].append((False, BitReader(b.buf, b.num, b.pos)))
            return
        orig(ch, b_open, b)

    p.actor_bunch = actor_bunch
    p.run()
    return store


def score(parser, nc, withrot, bunches):
    full = partial = bad = 0
    for is_open, b0 in bunches:
        b = BitReader(b0.buf, b0.num, b0.pos)
        try:
            if is_open:
                parser.read_vector(b)
                if withrot:
                    parser.read_rotator(b)
            parser._decode_props(nc, b, [])
            full += 1
        except UnknownFieldError:
            partial += 1
        except (BitError, DecodeError):
            bad += 1
    return full, partial, bad


def infer(data, hdr, pmap, model, max_extra=48, verbose=True):
    store = collect(data, hdr, pmap, model)
    parser = DemoParser(data, model, pmap, _Null(hdr))
    supers = [c for c in (find_class(model, s) for s in CANDIDATE_SUPERS) if c]
    result = {}
    for key, all_bunches in sorted(store.items(), key=lambda kv: -len(kv[1])):
        opens = [x for x in all_bunches if x[0]][:60]
        rest = [x for x in all_bunches if not x[0]]
        step = max(1, len(rest) // 240)
        bunches = opens + rest[::step]
        best = None
        for sc in supers:
            for extra in range(0, max_extra + 1):
                vc = VirtualNetClass('?' + key, sc, extra)
                for withrot in (False, True):
                    full, partial, bad = score(parser, vc, withrot, bunches)
                    # prefer: few bad, many full, fewer extra fields
                    rank = (full - 4 * bad, -bad, -extra)
                    if best is None or rank > best[0]:
                        best = (rank, sc, extra, withrot, full, partial, bad)
        rank, sc, extra, withrot, full, partial, bad = best
        if verbose:
            print('%-22s n=%5d -> %s +%d rot=%s  full=%d partial=%d bad=%d' % (
                key, len(all_bunches), sc.path, extra, withrot, full, partial, bad))
        result[key] = (sc.path, extra, withrot, full, partial, bad, len(all_bunches))
    return result


if __name__ == '__main__':
    from lib.upkg import PackageIndex
    fname = sys.argv[1]
    unk = eval(sys.argv[2]) if len(sys.argv) > 2 else {}
    data = Path(fname).read_bytes()
    hdr = read_header(data)
    idx = PackageIndex({Path('UT2004')})
    model = Model(idx)
    pmap = PackageMap(hdr, idx, unk)
    infer(data, hdr, pmap, model)
