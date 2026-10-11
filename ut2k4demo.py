"""Extract kills, item pickups and scores from UT2004 .demo4 files.

Usage:
    python ut2k4demo.py <demo.demo4> [more demos ...] [--db file] [--out dir]

Package metadata is read from the package database (ut2004_packages.json.gz,
see pkgdb.py). Alternatively, --ut reads it directly from a UT2004
installation.
"""
import argparse
import bisect
import json
import os
import sys

from demo import Actor, DemoParser, PackageMap, read_header
from infer_virtual import find_class
from netmodel import Model, ObjRef, UnknownObject, VirtualNetClass
from pkgdb import DEFAULT_DB, PackageDB
from upkg import PackageIndex

# ---------------------------------------------------------------------------
# Packages that are referenced by demos but missing from the installation.
# 'exports'/'names' are the package's export and name counts (they shift the
# object/name index space of the package map). 'classes' describes the network
# layout of the mod's actor classes that we need: they inherit a known class
# and add 'extra' replicated fields; 'opaque' gives the bit size of extra
# fields whose content we do not need. All values were derived by decoding
# the demos under different hypotheses (see infer_virtual.py).
# ---------------------------------------------------------------------------
MISSING_PACKAGES = {
    'utcompv16a': {
        'exports': 5250, 'names': 4000,
        'classes': {
            # field 8 is a hit notification RPC:
            # (damage type, damage, byte, bool, bool, bool)
            0: ('BS_xPlayer', 'XGame.xPlayer', 28, False,
                {8: ('UTComp_HitNotify', ['ObjectProperty', 'IntProperty',
                                          'ByteProperty', 'BoolProperty',
                                          'BoolProperty', 'BoolProperty'])}),
            4: ('UTComp_xPawn', 'XGame.xPawn', 5, False, {0: 9}),
        },
    },
    'ta100': {'exports': 26, 'names': 0},
    'lockrolloff': {'exports': 10, 'names': 0},
    # AntiTCC 1.18j: same size class as the installed 1.18c (1462 exports);
    # 1477 puts the UTComp classes on their known local indices
    'antitcc118j': {'exports': 1477, 'names': 1480},
    # 3SPN: no objects of it are referenced in the demos; any count between
    # 0 and ~2500 decodes identically
    '3spnv257': {'exports': 1750, 'names': 0},
    '3spntexv240': {'exports': 50, 'names': 0},
}

# Standard packages that some servers had in a newer generation than the
# installation the package database was built from (same GUID, more
# exports). Key: '<package>#<generation>' -> (exports, names). The split of
# the extra exports between the two announcer packages is not determined by
# the demos; only their sum (853 incl. both) and their position matter.
NEWER_GENERATIONS = {
    'announcerassault#5': (255, 263),
    'tauntpack#2': (720, 728),
    'announcerfemale2k4#5': (198 + 427, 206 + 427),
    'announcermale2k4#5': (155 + 426, 163 + 426),
}

DAMAGE_TYPES = {
    'DamTypeShieldImpact': 'Shield Gun',
    'DamTypeAssaultBullet': 'Assault Rifle',
    'DamTypeAssaultGrenade': 'Assault Rifle (Grenade)',
    'DamTypeBioGlob': 'Bio Rifle',
    'DamTypeShockBeam': 'Shock Rifle (Beam)',
    'DamTypeShockBall': 'Shock Rifle (Ball)',
    'DamTypeShockCombo': 'Shock Rifle (Combo)',
    'DamTypeLinkPlasma': 'Link Gun (Plasma)',
    'DamTypeLinkShaft': 'Link Gun (Shaft)',
    'DamTypeMinigunBullet': 'Minigun',
    'DamTypeMinigunAlt': 'Minigun (Alt)',
    'DamTypeFlakChunk': 'Flak Cannon (Chunks)',
    'DamTypeFlakShell': 'Flak Cannon (Shell)',
    'DamTypeRocket': 'Rocket Launcher',
    'DamTypeRocketHoming': 'Rocket Launcher (Homing)',
    'DamTypeSniperShot': 'Lightning Gun',
    'DamTypeSniperHeadShot': 'Lightning Gun (Headshot)',
    'DamTypeClassicSniper': 'Sniper Rifle',
    'DamTypeClassicHeadshot': 'Sniper Rifle (Headshot)',
    'DamTypeRedeemer': 'Redeemer',
    'DamTypeIonBlast': 'Ion Painter',
    'DamTypeTelefragged': 'Telefrag',
    'DamTypeSuperShockBeam': 'Super Shock Rifle',
    'Fell': 'Fall damage',
    'FellLava': 'Lava',
    'Crushed': 'Crushed',
    'Suicided': 'Suicide',
    'Gibbed': 'Gibbed',
    'Drowned': 'Drowned',
}

# Damage types from packages that are not installed, identified by the
# damage they deal and by the weapon the killer held (see README).
UNKNOWN_DAMAGE_TYPES = {
    '3spnv257:728': 'Flak Cannon (Chunks)',
    '3spnv257:723': 'Flak Cannon (Shell)',
    '3spnv257:745': 'Bio Rifle',
}

ATTACHMENTS = {
    'ShieldAttachment': 'Shield Gun',
    'AssaultAttachment': 'Assault Rifle',
    'BioAttachment': 'Bio Rifle',
    'ShockAttachment': 'Shock Rifle',
    'LinkAttachment': 'Link Gun',
    'MinigunAttachment': 'Minigun',
    'FlakAttachment': 'Flak Cannon',
    'RocketAttachment': 'Rocket Launcher',
    'SniperAttachment': 'Lightning Gun',
    'ClassicSniperAttachment': 'Sniper Rifle',
}

PICKUPS = {
    'MiniHealthPack': 'Health Vial (+5)',
    'HealthPack': 'Health Pack (+25)',
    'SuperHealthPack': 'Big Keg o\' Health (+100)',
    'ShieldPack': 'Shield Pack (+50)',
    'SuperShieldPack': 'Super Shield Pack (+100)',
    'UDamagePack': 'Double Damage',
    'AdrenalinePickup': 'Adrenaline',
    'ShieldGunPickup': 'Shield Gun',
    'AssaultRiflePickup': 'Assault Rifle',
    'BioRiflePickup': 'Bio Rifle',
    'ShockRiflePickup': 'Shock Rifle',
    'LinkGunPickup': 'Link Gun',
    'MinigunPickup': 'Minigun',
    'FlakCannonPickup': 'Flak Cannon',
    'RocketLauncherPickup': 'Rocket Launcher',
    'SniperRiflePickup': 'Lightning Gun',
    'ClassicSniperRiflePickup': 'Sniper Rifle',
    'RedeemerPickup': 'Redeemer',
    'PainterPickup': 'Ion Painter',
    'AssaultAmmoPickup': 'Assault Rifle Ammo',
    'BioAmmoPickup': 'Bio Rifle Ammo',
    'ShockAmmoPickup': 'Shock Rifle Ammo',
    'LinkAmmoPickup': 'Link Gun Ammo',
    'MinigunAmmoPickup': 'Minigun Ammo',
    'FlakAmmoPickup': 'Flak Cannon Ammo',
    'RocketAmmoPickup': 'Rocket Launcher Ammo',
    'SniperAmmoPickup': 'Lightning Gun Ammo',
    'ClassicSniperAmmoPickup': 'Sniper Rifle Ammo',
}

# Seconds within which a recorder pickup message and a pickup actor event are
# considered the same pickup.
MATCH_WINDOW = 1.0


def fmt_time(t):
    t = max(0.0, t)
    return '%02d:%04.1f' % (int(t // 60), t % 60)


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


class PickupState:
    __slots__ = ('actor', 'hidden', 'respawning', 'loc', 'opened', 'shown')

    def __init__(self):
        self.shown = []  # times at which the item (re)appeared


class Analyzer:
    def __init__(self, hdr):
        self.hdr = hdr
        self.pris = {}            # actor id -> PRI actor
        self.kills = []
        self.pickup_events = []   # from pickup actors
        self.pickup_msgs = []     # recorder pickup messages
        self.pickups = {}         # actor id -> PickupState
        self.pawns = {}           # actor id -> pawn actor
        self.pawn_track = {}      # pawn id -> [(time, loc), ...]
        self.recorder_ctrl = None
        self.rec_track = []       # recorder positions from ClientAdjustPosition
        self.first_open_time = None
        self.score_log = []       # (time, pri id, field, value)
        self.gri = None
        self.teams = {}           # actor id -> TeamInfo actor
        self.vol_hist = {}        # PRI id -> [(time, volume name), ...]
        self.end_time = 0.0
        self.match_end = None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def is_class(obj, name):
        return isinstance(obj, Actor) and obj.netclass.is_a(name)

    def pri_name(self, pri):
        if isinstance(pri, Actor):
            return pri.props.get('PlayerName') or '<PRI#%d>' % pri.id
        return None

    @staticmethod
    def class_name(obj):
        if isinstance(obj, ObjRef):
            return obj.name
        return repr(obj) if obj is not None else None

    # -- listener interface ------------------------------------------------
    def on_frame(self, p):
        self.end_time = p.time

    def on_control(self, p, msg):
        pass

    def on_end(self, p):
        self.end_time = p.time

    def on_actor_open(self, p, a):
        if self.first_open_time is None:
            self.first_open_time = p.time
        nc = a.netclass
        if nc.is_a('PlayerReplicationInfo'):
            self.pris[a.id] = a
        elif nc.is_a('Pickup'):
            st = PickupState()
            st.actor = a
            st.hidden = False  # Pickup default; updated by the open bunch
            st.loc = a.spawn_loc
            st.opened = p.time
            # pickups present right at the start are level placed / base
            # spawned and respawn; later ones are dropped weapons
            st.respawning = p.time - self.first_open_time < 3.0
            self.pickups[a.id] = st
        elif nc.is_a('Pawn'):
            self.pawns[a.id] = a
            self.pawn_track[a.id] = []
            if a.spawn_loc:
                self.pawn_track[a.id].append((p.time, a.spawn_loc))
        elif nc.is_a('GameReplicationInfo'):
            self.gri = a
        elif nc.is_a('TeamInfo'):
            self.teams[a.id] = a

    def on_actor_close(self, p, a):
        st = self.pickups.get(a.id)
        if st is not None and not st.respawning and not st.hidden:
            # a dropped pickup that disappears before it fades out was taken
            if p.time - st.opened < 15.0:
                self.pickup_events.append((p.time, a, st.loc, 'closed'))

    def on_prop(self, p, a, key, value, old):
        nc = a.netclass
        if a.id in self.pickups:
            st = self.pickups[a.id]
            if key == 'Location':
                st.loc = value
            elif key == 'bHidden':
                if p.time - st.opened < 1e-6:
                    pass  # initial state from the channel open bunch
                elif value and not st.hidden:
                    if st.respawning or p.time - st.opened < 15.0:
                        self.pickup_events.append((p.time, a, st.loc, 'hidden'))
                elif not value and st.hidden:
                    st.respawning = True
                    st.shown.append(p.time)
                st.hidden = value
            return
        if a.id in self.pawns:
            if key == 'Location':
                self.pawn_track[a.id].append((p.time, value))
            elif key == 'PawnPosition' and isinstance(value, dict):
                self.pawn_track[a.id].append((p.time, value.get('Location')))
            return
        if a.id in self.pris and key == 'PlayerVolume':
            # the area a player is in; replicated for every player
            name = value.name if isinstance(value, ObjRef) else None
            self.vol_hist.setdefault(a.id, []).append((p.time, name))
            return
        if a.id in self.pris and key in ('Score', 'Deaths', 'PlayerName'):
            self.score_log.append((p.time, a.id, key, value))
            return
        if a is self.gri and key == 'Winner' and value is not None:
            self.match_end = p.time

    def on_rpc(self, p, a, name, args):
        if name.endswith('ClientAdjustPosition') and 'NewLocX' in args:
            self.rec_track.append((p.time, (args.get('NewLocX', 0.0),
                                            args.get('NewLocY', 0.0),
                                            args.get('NewLocZ', 0.0))))
            return
        if name == 'ClientHearSound':
            # pickups play their pickup sound when taken (never on respawn)
            act = args.get('Actor')
            snd = args.get('S')
            if isinstance(act, Actor) and act.id in self.pickups:
                st = self.pickups[act.id]
                self.pickup_events.append((p.time, act, st.loc, 'sound'))
            elif act is None and isinstance(snd, ObjRef) and                     snd.path.startswith('PickupSounds.'):
                self.pickup_events.append((p.time, None,
                                           args.get('SoundLocation'),
                                           'sound:' + snd.name))
            return
        if name != 'ReceiveLocalizedMessage':
            return
        if not a.netclass.is_a('PlayerController'):
            return
        self.recorder_ctrl = a
        msg = args.get('Message')
        opt = args.get('OptionalObject')
        pri1 = args.get('RelatedPRI_1')
        pri2 = args.get('RelatedPRI_2')
        mname = self.class_name(msg) or ''
        if 'PickupMessage' in mname and isinstance(opt, ObjRef):
            self.pickup_msgs.append([p.time, opt.name, False])
            return
        # death message: victim PRI + damage type class. The mod in these
        # demos uses its own DeathMessage subclass, so do not rely on the
        # message class name; skip the personal killer/victim messages.
        if mname in ('xVictimMessage', 'xKillerMessage'):
            return
        is_class = isinstance(opt, ObjRef) and opt.class_name == 'Class'
        if self.match_end is not None and p.time > self.match_end:
            return  # deaths after the match ended do not count
        if isinstance(pri2, Actor) and (is_class or isinstance(opt, UnknownObject)):
            killer = pri1 if isinstance(pri1, Actor) else None
            suicide = killer is None or killer is pri2
            if is_class:
                dmg = opt.name
                weapon = DAMAGE_TYPES.get(dmg, dmg)
            else:
                # damage type from a package we do not have
                dmg = '%s:%d' % (opt.pkgname, opt.local_index)
                weapon = UNKNOWN_DAMAGE_TYPES.get(dmg.lower())
                if weapon is None:
                    held = self.held_weapon(p, killer) if not suicide else None
                    weapon = ('%s (Waffe des Killers)' % held if held
                              else 'unbekannt (%s)' % dmg)
            team_kill = (not suicide and killer.props.get('Team') is not None
                         and killer.props.get('Team') is pri2.props.get('Team'))
            self.kills.append({
                'time': p.time,
                'killer': None if suicide else self.pri_name(killer),
                'victim': self.pri_name(pri2),
                'damage_type': dmg,
                'weapon': weapon,
                'suicide': suicide,
                'team_kill': team_kill,
            })

    @staticmethod
    def held_weapon(parser, pri):
        """Weapon the player holds right now (from its weapon attachment)."""
        for ch in parser.channels.values():
            a = ch.actor
            if a is None or not a.netclass.is_a('WeaponAttachment'):
                continue
            inst = a.props.get('Instigator')
            if isinstance(inst, Actor) and inst.alive and \
                    inst.props.get('PlayerReplicationInfo') is pri:
                return ATTACHMENTS.get(a.cls_name, a.cls_name)
        return None

    # -- results -----------------------------------------------------------
    def recorder_pri(self):
        if self.recorder_ctrl is not None:
            pri = self.recorder_ctrl.props.get('PlayerReplicationInfo')
            if isinstance(pri, Actor):
                return pri
        for pri in self.pris.values():
            if pri.props.get('PlayerName') == self.hdr['recorder']:
                return pri
        return None

    def players(self):
        out = []
        for pri in self.pris.values():
            if pri.props.get('bOnlySpectator') and not pri.props.get('Score'):
                continue
            out.append(pri)
        return out

    def merged_pickup_events(self):
        """Merge the evidence for one pickup (state change, sound, ...)."""
        merged = []
        for t, actor, loc, kind in sorted(self.pickup_events, key=lambda e: e[0]):
            hit = None
            for prev in reversed(merged):
                if t - prev['time'] > MATCH_WINDOW:
                    break
                if actor is not None and prev['actor'] is actor:
                    hit = prev
                    break
                if (actor is None or prev['actor'] is None) and loc and \
                        prev['loc'] and dist(loc, prev['loc']) < 150:
                    hit = prev
                    break
            if hit is None:
                merged.append({'time': t, 'actor': actor, 'loc': loc,
                               'kinds': {kind}})
            else:
                hit['kinds'].add(kind)
                if hit['actor'] is None and actor is not None:
                    hit['actor'] = actor
                    hit['loc'] = loc or hit['loc']
        self.infer_from_respawn(merged)
        merged.sort(key=lambda m: m['time'])
        return merged

    def infer_from_respawn(self, merged):
        """Add pickups that happened right at a respawn and were not seen.

        If an item is taken in the same server tick it respawns, the client
        never sees it visible. Such pickups show up as a hidden period that
        lasts a multiple of the item's respawn time.
        """
        by_actor = {}
        for m in merged:
            if m['actor'] is not None:
                by_actor.setdefault(m['actor'].id, []).append(m['time'])
        for aid, st in self.pickups.items():
            if not st.respawning or aid not in by_actor:
                continue
            taken = sorted(by_actor[aid])
            spans = []
            for t in taken:
                nxt = [x for x in st.shown if x > t + 0.05]
                if nxt:
                    spans.append((t, nxt[0] - t))
            durations = sorted(d for _, d in spans if d > 3.0)
            if len(durations) < 3:
                continue
            # respawn time = the most common hidden duration
            support = [(sum(1 for e in durations if abs(e - d) <= 0.04 * d), -d)
                       for d in durations]
            n, neg_d = max(support)
            if n < 3:
                continue
            close = [e for e in durations if abs(e + neg_d) <= 0.04 * -neg_d]
            est = close[len(close) // 2]
            for t, d in spans:
                ratio = d / est
                if abs(ratio - round(ratio)) > 0.08:
                    continue
                k = int(round(ratio)) - 1
                for i in range(1, k + 1):
                    ti = t + est * i
                    if any(abs(ti - x) < 3.0 for x in taken):
                        continue
                    merged.append({'time': ti, 'actor': st.actor, 'loc': st.loc,
                                   'kinds': {'respawn-timing'}})

    def pawn_near(self, t, loc, exclude):
        best = None
        for pid, hist in self.pawn_track.items():
            pri = self.pawns[pid].props.get('PlayerReplicationInfo')
            if not isinstance(pri, Actor) or pri is exclude or not hist:
                continue
            times = [h[0] for h in hist]
            i = bisect.bisect_left(times, t)
            for j in (i - 1, i):
                if 0 <= j < len(hist):
                    pt, ploc = hist[j]
                    if ploc is None or abs(pt - t) > 1.0:
                        continue
                    d = dist(ploc, loc)
                    if best is None or d < best[0]:
                        best = (d, pri)
        return best

    def recorder_dist(self, t, loc):
        if not loc or not self.rec_track:
            return None
        i = bisect.bisect_left(self.rec_track, (t,))
        ds = [dist(self.rec_track[j][1], loc) for j in (i - 1, i)
              if 0 <= j < len(self.rec_track)
              and abs(self.rec_track[j][0] - t) < 0.5]
        return min(ds) if ds else None

    def volumes_at(self, pri, t, slack=1.0):
        """Volumes the player was in between t - slack and t + slack."""
        hist = self.vol_hist.get(pri.id)
        if not hist:
            return set()
        i = bisect.bisect_right(hist, (t - slack, '\uffff'))
        out = {hist[i - 1][1]} if i > 0 else set()
        while i < len(hist) and hist[i][0] <= t + slack:
            out.add(hist[i][1])
            i += 1
        out.discard(None)
        return out

    def just_died(self, pri, t):
        name = self.pri_name(pri)
        return any(k['victim'] == name and 0 <= t - k['time'] <= 1.0
                   for k in self.kills)

    # max. distance a player covers per second (running + dodging) and the
    # tolerance for position / pickup radius
    MAX_SPEED = 1200.0
    POS_SLACK = 250.0
    # a pickup belongs to a series if the same player took another item
    # within SERIES_TIME seconds and SERIES_DIST units
    SERIES_TIME = 1.5
    SERIES_DIST = 600.0

    def player_tracks(self):
        """Known positions per PRI id, merged over all pawns of the player."""
        tracks = {}
        for pid, hist in self.pawn_track.items():
            pri = self.pawns[pid].props.get('PlayerReplicationInfo')
            if isinstance(pri, Actor):
                tracks.setdefault(pri.id, []).extend(
                    h for h in hist if h[1] is not None)
        for v in tracks.values():
            v.sort(key=lambda h: h[0])
        return tracks

    def reachable(self, tracks, pri, t, loc, window=6.0):
        """False if a known position before/after t rules the player out."""
        hist = tracks.get(pri.id)
        if not hist or loc is None:
            return True
        i = bisect.bisect_left(hist, (t,))
        for j in (i - 1, i):
            if 0 <= j < len(hist):
                pt, ploc = hist[j]
                dt = abs(pt - t)
                if dt <= window and \
                        dist(ploc, loc) > self.MAX_SPEED * dt + self.POS_SLACK:
                    return False
        return True

    def item_volumes(self, events):
        """Map volumes each item lies in, learned from attributed pickups."""
        item_vols = {}
        for m in events:
            if m['who'] is not None and m['actor'] is not None:
                for v in self.volumes_at(m['who'], m['time']):
                    c = item_vols.setdefault(m['actor'].id, {})
                    c[v] = c.get(v, 0) + 1
        out = {}
        for aid, c in item_vols.items():
            total = sum(c.values())
            out[aid] = {v for v, n in c.items() if n >= 0.2 * total}
        return out

    def candidates(self, m, others, tracks):
        return [pl for pl in others
                if not self.just_died(pl, m['time'])
                and self.reachable(tracks, pl, m['time'], m['loc'])]

    def attribute_rest(self, events, others):
        """Attribute pickups that have no message and no nearby pawn.

        Evidence, in this order:
        - reachability: players whose known positions shortly before/after
          rule them out are dropped; one candidate left -> 'reachability'
        - series: exactly one candidate took another item within 3 s and
          1000 units (vial chains, weapon + ammo) -> 'series'
        - volume: exactly one candidate was in the map volume of the item
          -> 'volume'
        """
        tracks = self.player_tracks()
        for _ in range(3):  # attributions feed the series / volume evidence
            changed = False
            item_vols = self.item_volumes(events)
            done = [m for m in events if m['who'] is not None]
            for m in events:
                if m['who'] is not None:
                    continue
                cands = self.candidates(m, others, tracks)
                if len(cands) == 1 and len(others) > 1:
                    m['who'], m['how'] = cands[0], 'reachability'
                    changed = True
                    continue
                if len(cands) <= 1:
                    continue
                series = {o['who'] for o in done
                          if o['who'] in cands
                          and abs(o['time'] - m['time']) <= self.SERIES_TIME
                          and m['loc'] and o['loc']
                          and dist(o['loc'], m['loc']) <= self.SERIES_DIST}
                if len(series) == 1:
                    m['who'], m['how'] = series.pop(), 'series'
                    changed = True
                    continue
                vols = item_vols.get(m['actor'].id) if m['actor'] else None
                if vols:
                    in_vol = [pl for pl in cands
                              if vols & self.volumes_at(pl, m['time'])]
                    if len(in_vol) == 1:
                        m['who'], m['how'] = in_vol[0], 'volume'
                        changed = True
            if not changed:
                break
        for m in events:
            if m['who'] is None and len(others) == 1:
                m['who'], m['how'] = others[0], 'only opponent'

    def resolve_pickups(self):
        rec = self.recorder_pri()
        rec_name = self.pri_name(rec)
        others = [pl for pl in self.players() if pl is not rec]
        events = self.merged_pickup_events()
        for m in events:
            if m['actor'] is not None:
                m['cls'] = m['actor'].cls_name
            else:
                m['cls'] = sorted(m['kinds'])[0].split(':', 1)[1]  # from sound
            m['msg'] = None
        # 1) recorder: assign each own pickup message to the matching event of
        #    the same item class, preferring the item closest to the recorder
        for msg in self.pickup_msgs:
            cands = []
            for m in events:
                if m['msg'] is not None or m['cls'] != msg[1]:
                    continue
                window = 3.0 if 'respawn-timing' in m['kinds'] else MATCH_WINDOW
                dt = abs(m['time'] - msg[0])
                if dt <= window:
                    d = self.recorder_dist(msg[0], m['loc'])
                    cands.append((d if d is not None else 1e9, dt, id(m), m))
            if cands:
                m = min(cands)[3]
                m['msg'] = msg
                msg[2] = True
        # 2) nearest other pawn with a known position
        for m in events:
            m['who'] = m['how'] = None
            if m['msg'] is not None:
                m['time'] = m['msg'][0]
                m['who'], m['how'] = rec, 'message'
                continue
            near = self.pawn_near(m['time'], m['loc'], rec) if m['loc'] else None
            if near and near[0] < 300:
                m['who'], m['how'] = near[1], 'position'
        # 3) remaining pickups: combine reachability, series and map volume
        self.attribute_rest(events, others)
        result = []
        for m in events:
            t, loc, cls = m['time'], m['loc'], m['cls']
            if m['who'] is None:
                who, how = '?', 'unknown'
            else:
                who, how = self.pri_name(m['who']), m['how']
            result.append({'time': t, 'player': who, 'item_class': cls,
                           'item': PICKUPS.get(cls, cls), 'attribution': how,
                           'evidence': sorted(m['kinds']),
                           'location': [round(c) for c in loc] if loc else None})
        # recorder messages without a matching actor event
        for msg in self.pickup_msgs:
            if not msg[2]:
                result.append({'time': msg[0], 'player': rec_name,
                               'item_class': msg[1],
                               'item': PICKUPS.get(msg[1], msg[1]),
                               'attribution': 'message', 'evidence': ['message'],
                               'location': None})
        result.sort(key=lambda r: r['time'])
        return result

    @staticmethod
    def team_name(team):
        if not isinstance(team, Actor):
            return None
        idx = team.props.get('TeamIndex') or 0  # default 0 is not replicated
        return {0: 'Rot', 1: 'Blau'}.get(idx, 'Team %s' % idx)

    def winner(self):
        if self.gri is None:
            return None
        w = self.gri.props.get('Winner')
        if not isinstance(w, Actor):
            return None
        if w.netclass.is_a('TeamInfo'):
            return 'Team %s' % self.team_name(w)
        return self.pri_name(w)

    def scoreboard(self):
        rows = []
        for pri in self.players():
            name = self.pri_name(pri)
            mine = [k for k in self.kills if k['killer'] == name]
            kills = sum(1 for k in mine if not k.get('team_kill'))
            team_kills = sum(1 for k in mine if k.get('team_kill'))
            suicides = sum(1 for k in self.kills
                           if k['suicide'] and k['victim'] == name)
            deaths_msg = sum(1 for k in self.kills if k['victim'] == name)
            rows.append({
                'player': name,
                'team': self.team_name(pri.props.get('Team')),
                'score': int(pri.props.get('Score', 0) or 0),
                'deaths': int(pri.props.get('Deaths', 0) or 0),
                'kills': kills,
                'team_kills': team_kills,
                'suicides': suicides,
                'deaths_from_messages': deaths_msg,
            })
        rows.sort(key=lambda r: (str(r['team']), -r['score'], r['deaths']))
        return rows

    def team_scores(self):
        out = []
        for t in self.teams.values():
            out.append({'team': self.team_name(t),
                        'score': int(t.props.get('Score', 0) or 0)})
        out.sort(key=lambda r: -r['score'])
        return out


def build_parser(data, hdr, pkgindex, model):
    unk = {k: (v['exports'], v['names']) for k, v in MISSING_PACKAGES.items()}
    unk.update(NEWER_GENERATIONS)
    pmap = PackageMap(hdr, pkgindex, unk)
    analyzer = Analyzer(hdr)
    parser = DemoParser(data, model, pmap, analyzer)
    for pkgname, spec in MISSING_PACKAGES.items():
        for local, (cname, sup, extra, rot, opaque) in spec.get('classes', {}).items():
            sc = find_class(model, sup)
            vc = VirtualNetClass(cname, sc, extra, opaque)
            parser.virtual['%s:%d' % (pkgname, local)] = (vc, rot)
    return parser, analyzer, pmap


def analyze(path, pkgindex, model):
    data = open(path, 'rb').read()
    hdr = read_header(data)
    parser, an, pmap = build_parser(data, hdr, pkgindex, model)
    parser.run()
    t0 = 0.0
    kills = an.kills
    result = {
        'file': os.path.basename(path),
        'map': hdr['map'],
        'gametype': hdr['gametype'],
        'recorder': hdr['recorder'],
        'date': hdr['date'],
        'duration': round(an.end_time, 1),
        'match_end': round(an.match_end, 1) if an.match_end else None,
        'missing_packages': pmap.missing,
        'newer_generations': pmap.newer,
        'decode_stats': dict(parser.stats),
        'kills': kills,
        'pickups': an.resolve_pickups(),
        'scoreboard': an.scoreboard(),
        'teams': an.team_scores(),
    }
    gri = an.gri
    if gri is not None:
        result['server'] = (gri.props.get('ServerName') or '').strip()
        result['time_limit'] = gri.props.get('TimeLimit')
        result['winner'] = an.winner()
    return result


def to_markdown(r):
    L = []
    L.append('# %s' % r['file'])
    L.append('')
    L.append('- Map: **%s** (%s)' % (r['map'], r['gametype']))
    if r.get('server'):
        L.append('- Server: %s' % r['server'])
    L.append('- Aufgenommen von: %s am %s' % (r['recorder'], r['date']))
    L.append('- Demo-Länge: %s' % fmt_time(r['duration']))
    if r.get('winner'):
        L.append('- Gewinner: **%s**' % r['winner'])
    L.append('')
    L.append('## Score')
    L.append('')
    teams = any(s['team'] for s in r['scoreboard'])
    if r.get('teams'):
        L.append('| Team | Team-Score |')
        L.append('|---|---:|')
        for t in r['teams']:
            L.append('| %s | %d |' % (t['team'], t['score']))
        L.append('')
    if teams:
        L.append('| Spieler | Team | Score | Kills | Teamkills | Deaths | Suizide |')
        L.append('|---|---|---:|---:|---:|---:|---:|')
        for s in r['scoreboard']:
            L.append('| %s | %s | %d | %d | %d | %d | %d |' % (
                s['player'], s['team'], s['score'], s['kills'],
                s['team_kills'], s['deaths'], s['suicides']))
    else:
        L.append('| Spieler | Score | Kills | Deaths | Suizide |')
        L.append('|---|---:|---:|---:|---:|')
        for s in r['scoreboard']:
            L.append('| %s | %d | %d | %d | %d |' % (s['player'], s['score'],
                                                   s['kills'], s['deaths'],
                                                   s['suicides']))
    L.append('')
    L.append('## Kills (%d)' % len(r['kills']))
    L.append('')
    order = [s['player'] for s in r['scoreboard']]
    L.append('| # | Zeit | Killer | Opfer | Waffe / Schadenstyp | Stand (%s) |'
             % ' : '.join(order))
    L.append('|---:|---|---|---|---|---|')
    running = dict.fromkeys(order, 0)
    for i, k in enumerate(r['kills'], 1):
        if k['suicide']:
            killer = '*(Suizid)*'
            if k['victim'] in running:
                running[k['victim']] -= 1
        else:
            killer = k['killer']
            if killer in running:
                running[killer] += -1 if k.get('team_kill') else 1
            if k.get('team_kill'):
                killer += ' *(Teamkill)*'
        L.append('| %d | %s | %s | %s | %s | %s |' % (
            i, fmt_time(k['time']), killer, k['victim'], k['weapon'],
            ' : '.join(str(running[p]) for p in order)))
    L.append('')
    L.append('## Item-Pickups (%d)' % len(r['pickups']))
    L.append('')
    counts = {}
    for pk in r['pickups']:
        counts.setdefault(pk['player'], {}).setdefault(pk['item'], 0)
        counts[pk['player']][pk['item']] += 1
    L.append('### Zusammenfassung')
    L.append('')
    for player, items in sorted(counts.items(), key=lambda kv: str(kv[0])):
        total = sum(items.values())
        L.append('- **%s** (%d): %s' % (player, total, ', '.join(
            '%s ×%d' % (it, n) for it, n in sorted(items.items(), key=lambda kv: -kv[1]))))
    L.append('')
    L.append('### Alle Pickups')
    L.append('')
    L.append('| # | Zeit | Spieler | Item | Zuordnung |')
    L.append('|---:|---|---|---|---|')
    src_de = {'message': 'Pickup-Nachricht an Recorder',
              'position': 'Spielerposition',
              'volume': 'Spieler im selben Kartenbereich',
              'reachability': 'einziger Spieler in Reichweite',
              'series': 'Serie (Spieler nahm kurz davor/danach Item in der Nähe)',
              'only opponent': 'Gegner (nicht Recorder)',
              'unknown': 'unbekannt'}
    ev_de = {'hidden': 'Item verschwindet', 'sound': 'Pickup-Sound',
             'closed': 'Item entfernt', 'respawn-timing': 'Respawn-Timing',
             'message': 'Nachricht'}
    for i, pk in enumerate(r['pickups'], 1):
        ev = ', '.join(ev_de.get(e.split(':')[0], e) for e in pk['evidence'])
        L.append('| %d | %s | %s | %s | %s (%s) |' % (
            i, fmt_time(pk['time']), pk['player'], pk['item'],
            src_de.get(pk['attribution'], pk['attribution']), ev))
    L.append('')
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('demos', nargs='+')
    ap.add_argument('--db', default=DEFAULT_DB,
                    help='package database (default: %(default)s)')
    ap.add_argument('--ut', help='read packages from this UT2004 installation '
                    'instead of the package database')
    ap.add_argument('--out', default='output')
    args = ap.parse_args(argv)
    if args.ut:
        if not os.path.isdir(args.ut):
            ap.error('--ut must point to an existing directory: %s' % args.ut)
        idx = PackageIndex(args.ut)
    else:
        if not os.path.exists(args.db):
            ap.error('package database %s not found; create it with '
                     '"python pkgdb.py --ut <UT2004 dir> <demos>" or pass --ut'
                     % args.db)
        idx = PackageDB(args.db)
    model = Model(idx)
    os.makedirs(args.out, exist_ok=True)
    for path in args.demos:
        r = analyze(path, idx, model)
        unknown = [p for p in r['missing_packages']
                   if p.lower() not in MISSING_PACKAGES]
        if unknown:
            print('WARNING: %s: packages missing and not described in '
                  'MISSING_PACKAGES, results may be wrong: %s'
                  % (path, ', '.join(unknown)))
        if r.get('newer_generations'):
            print('WARNING: %s: server used newer package generations not '
                  'described in NEWER_GENERATIONS, results may be wrong: %s'
                  % (path, ', '.join(r['newer_generations'])))
        base = os.path.splitext(os.path.basename(path))[0]
        with open(os.path.join(args.out, base + '.json'), 'w', encoding='utf-8') as f:
            json.dump(r, f, indent=1, ensure_ascii=False)
        with open(os.path.join(args.out, base + '.md'), 'w', encoding='utf-8') as f:
            f.write(to_markdown(r))
        print('%s: %d kills, %d pickups, stats %s' % (
            base, len(r['kills']), len(r['pickups']), r['decode_stats']))
        for s in r['scoreboard']:
            print('   %-20s %-5s score %3d  kills %3d  teamkills %d  deaths %3d  '
                  'suicides %d  (deaths in msgs %d)' % (
                      s['player'], s['team'] or '', s['score'], s['kills'],
                      s['team_kills'], s['deaths'], s['suicides'],
                      s['deaths_from_messages']))


if __name__ == '__main__':
    main()
