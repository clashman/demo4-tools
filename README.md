# UT2004 Demo Parser

A pure-Python parser for Unreal Tournament 2004 `.demo4` files. It decodes the
recorded network stream and extracts **kills**, **item pickups** and the
**score** of each player.

```bash
python ut2k4demo.py *.demo4 [--db ut2004_packages.json.gz] [--out output]
```

No UT2004 installation is needed. The layouts of classes and properties come
from the package database `ut2004_packages.json.gz` (about 8 MB). It contains
the metadata of all 871 packages of the UT2004 installation that this tool was
built with. To read directly from an installation instead, pass `--ut <dir>`.

To build or extend the database (an installation is only needed for this step):

```bash
python pkgdb.py --ut UT2004 *.demo4    # packages referenced by these demos
python pkgdb.py --ut UT2004 --all      # every package of the installation
```

For every demo, a Markdown report (`<demo>.md`) and the raw data (`<demo>.json`)
are written to the output directory. Team games (e.g. `xTeamGame`) are
supported: the report then also contains team scores and team kills.

## Architecture

```
ut2k4demo.py   analysis + reports (kills, pickups, scoreboard)
   └─ demo.py         demo file / network stream decoder (packets, bunches, actor channels)
        ├─ netmodel.py    replication layout of classes, properties and RPCs
        │    ├─ pkgdb.py     offline package database (default source of package metadata)
        │    └─ upkg.py      UE2 package reader (names, imports, exports, field metadata)
        └─ bits.py        bit reader compatible with UE2's FBitReader
infer_virtual.py  helper that infers layouts of classes from missing packages
```

---

## `bits.py` – bit-level reading

| Name | Description |
|---|---|
| `BitError` | Raised when reading past the end of a bit buffer. |
| `BitReader(buf, num_bits=None, pos=0)` | LSB-first bit reader, equivalent to UE2's `FBitReader`. |
| `BitReader.at_end()` / `left()` | End-of-buffer check / number of remaining bits. |
| `BitReader.bit()` / `bits(n)` | Read one bit / `n` bits as an unsigned integer. |
| `BitReader.read_int(vmax)` | `SerializeInt(Value, ValueMax)`: variable-width integer whose bit count depends on `vmax`. |
| `BitReader.byte()`, `i32()`, `u16()`, `i16()`, `f32()` | Fixed-size values. |
| `BitReader.compact_index()` | UE compact index (variable-length signed int). |
| `BitReader.fstring()` | `FString` (compact-index length + ANSI or UTF-16 characters). |
| `BitReader.sub(nbits)` | Returns a reader over the next `nbits` bits and skips them (used for bunch payloads). |
| `packet_bits(data)` | Payload length of a packet in bits. Packets end with a 1-bit terminator. |

## `upkg.py` – UE2 package reader

| Name | Description |
|---|---|
| `Reader(data, pos=0)` | Byte reader for package files: `u8`, `u16`, `i32`, `u32`, `ci` (compact index) and `fstring`. |
| `CPF_*`, `FUNC_Net` | Property/function flags that replication uses (`CPF_Net`, `CPF_Parm`, `CPF_ReturnParm`). |
| `PROPERTY_CLASSES` | Names of all `UProperty` subclasses. |
| `Export` | One entry of the export table: class, super, outer, name, flags and the serial size/offset. After `parse_export()`, it also holds the field metadata (`field_next`, `children`, `array_dim`, `prop_flags`, `inner`, `func_flags`, `enum_names`). |
| `Package(path)` | Loads a package: header, generations (export/name counts per generation), name, import and export tables. |
| `Package.exports_by_name(name)` | Case-insensitive lookup of exports by name. |
| `Package.ref_name(ref)` / `ref_path(ref)` / `export_path(e)` | Resolve object references (positive = export, negative = import) to a name or a dotted path. |
| `Package.export_class_name(e)` | Class name of an export (`'Class'` for class objects). |
| `Package._skip_tagged_props(r)` | Skips a tagged property list, which ends with `None`. |
| `Package.parse_export(e)` | Parses only the metadata the netcode needs. For properties: ArrayDim, flags and inner type (enum/class/struct). For enums: the value names. For structs, functions and classes: super field and children list. For functions: the function flags, read from the end of the export. |
| `PackageIndex(root)` | Indexes all package files of a UT2004 install by name. `get(name, guid=None)` loads and caches a `Package`. |

## `pkgdb.py` – offline package database

| Name | Description |
|---|---|
| `DEFAULT_DB` | Path of the database next to the scripts (`ut2004_packages.json.gz`). |
| `FIELD_CLASSES` | Export classes whose field metadata is stored: all `UField` subclasses (properties, functions, structs, states, classes, enums, constants). |
| `package_to_record(pkg)` | Serializes a `Package` into a JSON-compatible record: GUID, generations, names, imports, and exports with their parsed field metadata. |
| `CachedPackage(rec)` | A `Package` restored from a record. It has no file data; `parse_export()` is a no-op because the metadata is already filled in. |
| `PackageDB(path)` | Drop-in replacement for `PackageIndex`. It loads the database. `get(name, guid=None)` returns the package version with the matching GUID. `add_record()` and `save()` extend and write the database. |
| `build_db(ut_dir, demo_paths, all_packages, db_path)` | Adds packages from an installation to the database: either the packages referenced by the given demos (plus the packages they import from) or all packages. |
| `main()` | Command-line entry point (`--ut`, `--db`, `--all`). |

## `netmodel.py` – replication layout

| Name | Description |
|---|---|
| `ceil_log2(n)` | Bit count for enum bytes (`appCeilLogTwo`). |
| `UnknownObject(pkgname, local_index)` | An object that comes from a package that is not installed. |
| `ObjRef(pkg, exp)` | A resolved object (export of a known package). Has the properties `name`, `class_name` and `path`. |
| `NetField` | One replicated field of a class: a property or an RPC. Holds its rep index, type information or RPC parameters, and an optional `opaque_bits` size for fields whose content is not known. |
| `NetClass(model, pkg, exp)` | Replication cache of a class (`FClassNetCache`). It builds on the super class's field indices and adds the class's own `CPF_Net` properties and its new `FUNC_Net` functions, sorted by export index. Provides `field(idx)`, `is_a(name)` and `max_index`. |
| `PropType` | Decoded type of a property or parameter: kind, array dimension, enum bit count, and the struct name and fields. |
| `Model(pkgindex)` | Builds and caches type information. |
| `Model.resolve_ref(pkg, ref)` | Resolves a package-local reference, including imports from other packages, to an `ObjRef`. |
| `Model.net_class(pkg, exp)` | Cached `NetClass` for a class export. |
| `Model.prop_type(pkg, fe)` | Cached `PropType` for a property export. |
| `Model.struct_props(pkg, sexp)` | Struct members in network serialization order. |
| `Model.func_params(pkg, fexp)` | Parameters of an RPC, in order. |
| `VirtualNetClass(name, super_class, extra, opaque=None)` | Stand-in for a class from a missing package. It inherits a known class's layout and adds `extra` unknown fields. `opaque` describes some of them: `{i: nbits}` for a field of fixed size that is skipped, or `{i: (name, [kinds])}` for an RPC whose parameter property kinds are known (e.g. `'IntProperty'`). |

## `demo.py` – demo and network stream decoder

| Name | Description |
|---|---|
| `MAX_PACKETID`, `MAX_CHANNELS`, `MAX_CHSEQUENCE`, `CHTYPE_*`, `MAX_PACKET` | UE2 netcode constants. |
| `DecodeError` | Data that cannot be decoded (e.g. an invalid field index). |
| `UnknownFieldError` | A field of a virtual class with no known layout was reached. The rest of that bunch is skipped. |
| `read_header(data)` | Parses the demo header: map, game type, recorder, date, and the package list (name, GUID, generation). |
| `iter_frames(data, start)` | Yields `(delta_time, frame_number, packet)` records. |
| `PackageMap(hdr, pkgindex, unknown_counts)` | Network package map. It computes the object and name index ranges of every package. Missing packages get their counts from `unknown_counts['<name>']`. Packages that the server had in a newer generation than the local copy (same GUID, more exports) get them from `unknown_counts['<name>#<generation>']`; packages for which no such entry exists are listed in `newer`. Provides `index_to_object()`, `index_to_name()`, `max_object` and `max_name`. |
| `Actor` | Client-side state of a replicated actor: `netclass`, `props`, `spawn_loc`, channel and id. |
| `Channel` | An open channel (type, actor, broken flag). |
| `make_relative(value, reference, maxv)` | Expands wrapped sequence numbers. |
| `DemoParser(data, model, pmap, listener)` | The decoder. `virtual` maps unknown classes to `VirtualNetClass` stand-ins. |
| `DemoParser.run()` | Walks all frames and accumulates the demo time once per frame. |
| `received_packet(pk)` | Splits a packet into acks and bunches (bunch header: control/open/close/reliable bits, channel, sequence, type, size). |
| `received_bunch(...)` / `deliver(...)` | Orders reliable bunches per channel index (sequence numbers persist across channel close and reopen) and creates channels. |
| `dispatch(ch, ...)` | Routes the bunch to the control channel (text messages) or to an actor channel, and handles channel close. |
| `actor_bunch(ch, b_open, b)` | When a channel opens, it reads the actor class or the static actor, plus the compressed spawn location and, if present, the spawn rotation. It then decodes properties and RPCs. |
| `decode_props(...)` / `_decode_props(...)` / `apply_updates(...)` | Field loop: rep index → property value (with an array element byte if needed) or RPC parameters (one presence bit per parameter; booleans are sent inline). Updates are passed to the listener. |
| `read_object`, `read_name`, `read_value` | Value decoding. Objects are encoded as a dynamic channel index or a static package-map index. Vectors and rotators in properties are plain structs. |
| `read_vector`, `read_rotator` | Compressed spawn location/rotation. |
| Listener interface | `on_frame`, `on_control`, `on_actor_open`, `on_actor_close`, `on_prop`, `on_rpc`, `on_end` |

## `infer_virtual.py` – layout inference for missing packages

| Name | Description |
|---|---|
| `CANDIDATE_SUPERS` | Known classes that are tried as the parent of an unknown class. |
| `find_class(model, path)` | `NetClass` for a path like `'XGame.xPawn'`. |
| `collect(data, hdr, pmap, model)` | Collects all bunches of channels whose actor class is unknown. |
| `score(parser, nc, withrot, bunches)` | Counts how many bunches decode completely, partially or not at all under a given hypothesis. |
| `infer(...)` | Tests combinations of parent class, number of extra fields and initial-rotation flag, and reports the best layout per unknown class. Can be run as a script: `python infer_virtual.py demo.demo4 "{'pkg': (exports, names)}"`. |

## `ut2k4demo.py` – analysis and reports

| Name | Description |
|---|---|
| `MISSING_PACKAGES` | Export/name counts and network layouts for packages missing from the installation (`utcompv16a`, `TA100`, `LockRolloff`, `AntiTCC118j`, `3SPNv257`, `3SPNTexv240`), derived from the demos. UTComp's controller gets a hit-notification RPC (`UTComp_HitNotify`: damage type, damage, byte, 3 bools). |
| `NEWER_GENERATIONS` | Export/name counts for standard packages that servers had in a newer generation (announcer packs, taunt pack). |
| `DAMAGE_TYPES`, `PICKUPS` | Readable names for damage types (weapons) and pickup classes. |
| `UNKNOWN_DAMAGE_TYPES` | Weapon names for damage types from missing packages (3SPN flak chunk / flak shell / bio), identified by their damage values and the killer's weapon. |
| `ATTACHMENTS` | Weapon names for weapon attachment classes (used to name unknown damage types by the weapon the killer held). |
| `MATCH_WINDOW` | Time window in which pickup evidence counts as the same pickup. |
| `fmt_time(t)`, `dist(a, b)` | Time formatting (`mm:ss.s`) and 3D distance. |
| `PickupState` | Tracking data per pickup actor: visibility, location, respawn times, and whether it respawns or was dropped. |
| `Analyzer(hdr)` | Listener that collects the game events. |
| `Analyzer.on_actor_open/close`, `on_prop` | Tracks PRIs (name, score, deaths, team, map volume), pawns (positions), teams, the GRI (winner) and pickups. A pickup event is an item that becomes hidden, or a dropped item that is removed. |
| `Analyzer.on_rpc` | Reads `ReceiveLocalizedMessage` (kill messages with killer, victim and damage type; the recorder's pickup messages), `ClientHearSound` (pickup sounds) and `*ClientAdjustPosition` (the recorder's position). Kills after the end of the match are ignored; team kills are flagged. |
| `held_weapon(parser, pri)` | Weapon a player currently holds, from its weapon attachment actor. |
| `recorder_pri()`, `players()`, `pri_name()` | Determines the recording player and the participants (spectators are excluded). |
| `merged_pickup_events()` | Merges the evidence for one pickup (item hidden, sound, item removed). |
| `infer_from_respawn(merged)` | Adds pickups taken exactly at respawn time, detected from hidden periods that last a multiple of the item's respawn time. |
| `pawn_near(t, loc, exclude)` / `recorder_dist(t, loc)` | Position lookups at a given point in time. |
| `volumes_at(pri, t)` / `just_died(pri, t)` | Map volume(s) a player was in around `t` (from `PlayerReplicationInfo.PlayerVolume`, replicated for every player) / whether the player died within the last second. |
| `player_tracks()` / `reachable(...)` | Known positions per player / whether a player could have reached an item, given known positions shortly before and after (`MAX_SPEED`, `POS_SLACK`). |
| `item_volumes(events)` / `candidates(...)` | Map volumes of each item, learned from attributed pickups / players that could have taken a pickup. |
| `attribute_rest(events, others)` | Attributes pickups without message or nearby pawn: only reachable player (`reachability`), same player took a nearby item shortly before/after (`series`, `SERIES_TIME`, `SERIES_DIST`), only player in the item's map volume (`volume`); in 1on1 the only opponent. |
| `resolve_pickups()` | Assigns each pickup to a player: recorder pickup messages first (preferring the item closest to the recorder), then the nearest pawn, then `attribute_rest()`. |
| `team_name()`, `winner()`, `team_scores()` | Team names (red/blue), winner (player or team), team scores. |
| `scoreboard()` | Final score and deaths from the PRIs, plus team, kills, team kills and suicides counted from the kill messages. |
| `build_parser(...)` | Creates the `PackageMap`, the `Analyzer` and the `DemoParser`, and registers the virtual classes. |
| `analyze(path, pkgindex, model)` | Runs a demo and returns the result dictionary (metadata, kills, pickups, scoreboard). |
| `to_markdown(r)` | Builds the report: score table, kill list with running score, and pickup list with how each pickup was attributed. |
| `main()` | Command-line entry point. It uses the package database by default, or an installation with `--ut`. It warns when a demo needs packages that are neither in the database nor described in `MISSING_PACKAGES`. |

## Limitations

- Client demos only contain what the recording player received. Pickup messages exist only for the recorder, so pickups of other players are inferred from item state, pickup sounds and player positions.
- Demos that use packages which are not in the database (other maps or mods) need a database update from an installation that has them. Mods missing from that installation, too, need an entry in `MISSING_PACKAGES`.
- Layouts for classes from missing packages are inferred, not read from the packages. Only the fields needed for kills, pickups and score are decoded. Some UTComp helper actors are skipped.
- With more than two players, part of the pickups cannot be attributed (`?` in the report). In a cross-check against pickups with a known player, the inferred attributions (`reachability`, `series`, `volume`) were right in about 98 % of the cases.

## Test status

| Demo | Result |
|---|---|
| `12-4-2006-(17-08)-DM-1on1-Roughinery-[TC-futuress]`, `DUMPED_zoom_headshot_rough_13-22_zeave` | Kills and score exact; pickups complete |
| `DUMPED_air_bio_rankin_13-15_noiR` (TDM 2v2, 3SPN, AntiTCC 1.18j) | Kills, score and team score exact; 11 % of the pickups unattributed |
| `10-7-2006-(19-11)-DM-1on1-Roughinery-FPS-[TC-nø!R]` | Kills and score consistent |
| `10-17-2006-(21-52)-DM-Rankin-FE-[TC-nø!R]` (missing `NoDoubleDamage`) | Kills and score consistent |
| `DUMPED_double_headshot_rankin_2-00_noiR` | One kill message missing (score off by 1 for one player) |
| `10-23-2006-(20-06)-DM-Campgrounds2004-G1E-[TC-MesserJoe]` (newer announcer packages) | Decodes after the `NEWER_GENERATIONS` fix (~99 %); kills/pickups not yet verified; a spawn problem with one `XWeapons` class remains |
| `Demo2006-11-26-64033093` (InstagibCTF, missing `SafeGame101Beta7b`) | **Not supported yet**: the size of `SafeGame101Beta7b` is unknown, so nearly nothing decodes |
