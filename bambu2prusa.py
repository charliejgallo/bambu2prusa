#!/usr/bin/env python3
"""Convert a Bambu Studio 3MF project into 3MF files that PrusaSlicer can open.

Bambu Studio stores meshes in 3D/Objects/*.model and references them through
production-extension components (p:path). PrusaSlicer 2.8 cannot resolve them
("Found component with invalid object id"). This script bakes the component
transforms, merges each object's parts into one mesh with PrusaSlicer volumes
(model parts, negative parts, modifiers, support blockers/enforcers) and writes
one 3MF per Bambu plate, centred on the bed, with the per-object settings that
have a PrusaSlicer equivalent.

Usage: bambu2prusa.py input.3mf [output_dir] [--bed 250x210] [--scale 85%]
"""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
      "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"}
P_PATH = "{%s}path" % NS["p"]

# Bambu part subtype -> PrusaSlicer volume_type
VOLUME_TYPES = {
    "normal_part": "ModelPart",
    "negative_part": "NegativeVolume",
    "modifier_part": "ParameterModifier",
    "support_blocker": "SupportBlocker",
    "support_enforcer": "SupportEnforcer",
}

INFILL = {"zig-zag": "rectilinear", "crosshatch": "rectilinear", "tri-hexagon": "stars",
          "rectilinear": "rectilinear", "alignedrectilinear": "alignedrectilinear",
          "grid": "grid", "triangles": "triangles", "cubic": "cubic", "line": "line",
          "concentric": "concentric", "honeycomb": "honeycomb", "3dhoneycomb": "3dhoneycomb",
          "gyroid": "gyroid", "hilbertcurve": "hilbertcurve",
          "archimedeanchords": "archimedeanchords", "octagramspiral": "octagramspiral",
          "adaptivecubic": "adaptivecubic", "supportcubic": "supportcubic",
          "lightning": "lightning"}
SURFACE = {"monotonic": "monotonic", "monotonicline": "monotoniclines",
           "zig-zag": "rectilinear", "rectilinear": "rectilinear",
           "alignedrectilinear": "alignedrectilinear", "concentric": "concentric",
           "hilbertcurve": "hilbertcurve", "archimedeanchords": "archimedeanchords",
           "octagramspiral": "octagramspiral"}

# Project-wide settings that describe the design rather than the printer or the
# material; they travel as per-object overrides. Everything else comes from the
# user's PrusaSlicer profile.
PROJECT_KEYS = ("enable_support", "support_type", "wall_loops", "top_shell_layers",
                "bottom_shell_layers", "sparse_infill_density", "sparse_infill_pattern")


def map_settings(md):
    """Bambu settings -> PrusaSlicer per-object / per-volume settings."""
    out = {}
    simple = {"sparse_infill_density": "fill_density", "wall_loops": "perimeters",
              "top_shell_layers": "top_solid_layers",
              "bottom_shell_layers": "bottom_solid_layers",
              "layer_height": "layer_height"}
    for b, p in simple.items():
        if b in md:
            out[p] = md[b]
    for b, p, table in (("sparse_infill_pattern", "fill_pattern", INFILL),
                        ("top_surface_pattern", "top_fill_pattern", SURFACE),
                        ("bottom_surface_pattern", "bottom_fill_pattern", SURFACE),
                        ("internal_solid_infill_pattern", "solid_fill_pattern", SURFACE)):
        if md.get(b) in table:
            out[p] = table[md[b]]
    if "ironing_type" in md:
        out["ironing"] = "0" if md["ironing_type"] == "no ironing" else "1"
    if "enable_support" in md:
        out["support_material"] = md["enable_support"]
        if md["enable_support"] == "1":
            out["support_material_auto"] = "1"
            if md.get("support_type", "").startswith("tree"):
                out["support_material_style"] = "organic"
    return out


def parse_matrix(s):
    """3MF transform (12 numbers, column-major) -> 3x4 rows."""
    if not s:
        return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
    v = [float(x) for x in s.split()]
    return [[v[0], v[3], v[6], v[9]], [v[1], v[4], v[7], v[10]], [v[2], v[5], v[8], v[11]]]


def mul(a, b):
    r = []
    for i in range(3):
        row = [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        row.append(sum(a[i][k] * b[k][3] for k in range(3)) + a[i][3])
        r.append(row)
    return r


def det(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def read_meshes(zf, path, cache):
    """Return {objectid: (verts, tris)} for one .model file."""
    if path in cache:
        return cache[path]
    root = ET.fromstring(zf.read(path.lstrip("/")))
    meshes = {}
    for obj in root.iter("{%s}object" % NS["m"]):
        mesh = obj.find("m:mesh", NS)
        if mesh is None:
            continue
        verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                 for v in mesh.find("m:vertices", NS)]
        tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")))
                for t in mesh.find("m:triangles", NS)]
        meshes[obj.get("id")] = (verts, tris)
    cache[path] = meshes
    return meshes


def load(src):
    zf = zipfile.ZipFile(src)
    main = ET.fromstring(zf.read("3D/3dmodel.model"))
    names = zf.namelist()
    cfg = (ET.fromstring(zf.read("Metadata/model_settings.config"))
           if "Metadata/model_settings.config" in names else ET.Element("config"))
    cache = {}

    project = {}
    if "Metadata/project_settings.config" in names:
        project = json.loads(zf.read("Metadata/project_settings.config"))
    defaults = {k: project[k] for k in PROJECT_KEYS if k in project}

    # Settings and part types per object, from model_settings.config
    settings = {}
    for o in cfg.findall("object"):
        omd = dict(defaults)
        omd.update({m.get("key"): m.get("value") for m in o.findall("metadata")})
        parts = {}
        for p in o.findall("part"):
            pmd = {m.get("key"): m.get("value") for m in p.findall("metadata")}
            parts[p.get("id")] = (p.get("subtype", "normal_part"), pmd)
        settings[o.get("id")] = (omd, parts)

    objects = {}
    for obj in main.find("m:resources", NS).findall("m:object", NS):
        oid = obj.get("id")
        omd, parts = settings.get(oid, (dict(defaults), {}))
        vols = []
        comps = obj.find("m:components", NS)
        if comps is not None:
            for c in comps.findall("m:component", NS):
                path = c.get(P_PATH, "/3D/3dmodel.model")
                mesh = read_meshes(zf, path, cache).get(c.get("objectid"))
                if mesh is None:
                    continue
                subtype, pmd = parts.get(c.get("objectid"), ("normal_part", {}))
                # The part "matrix" in model_settings.config belongs to the source
                # mesh, not to the volume: the component transform is the one to use.
                vols.append((mesh, parse_matrix(c.get("transform")), subtype, pmd))
        elif obj.find("m:mesh", NS) is not None:
            mesh = read_meshes(zf, "/3D/3dmodel.model", cache)[oid]
            vols.append((mesh, parse_matrix(None), "normal_part", {}))
        if vols:
            objects[oid] = (omd, vols)

    items = {it.get("objectid"): parse_matrix(it.get("transform"))
             for it in main.find("m:build", NS).findall("m:item", NS)}

    plates = []
    for p in cfg.findall("plate"):
        pmd = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        ids = [i.find("metadata[@key='object_id']").get("value")
               for i in p.findall("model_instance")]
        plates.append((int(pmd.get("plater_id", len(plates) + 1)),
                       pmd.get("plater_name") or "", ids))
    if not plates:
        plates = [(1, "", list(items))]
    return objects, items, plates, project


def bake(objects, items, oid, scale=1.0):
    """One mesh per object with all its volumes, in Bambu bed coordinates."""
    omd, vols = objects[oid]
    item = items.get(oid, parse_matrix(None))
    if scale != 1.0:
        item = mul([[scale, 0, 0, 0], [0, scale, 0, 0], [0, 0, scale, 0]], item)
    # PrusaSlicer wants the model parts first
    vols = sorted(vols, key=lambda v: v[2] != "normal_part")
    verts, tris, ranges = [], [], []
    for (mv, mt), m, subtype, pmd in vols:
        full = mul(item, m)
        flip = det(full) < 0  # mirrored part: keep the normals pointing out
        base = len(verts)
        for x, y, z in mv:
            verts.append(tuple(full[i][0] * x + full[i][1] * y + full[i][2] * z + full[i][3]
                               for i in range(3)))
        first = len(tris)
        for a, b, c in mt:
            tris.append((base + a, base + c, base + b) if flip else (base + a, base + b, base + c))
        ranges.append((first, len(tris) - 1, subtype, pmd))
    return omd, verts, tris, ranges


def place(baked):
    """Move the mesh to the origin: XY centred, model parts resting on z=0."""
    omd, verts, tris, ranges = baked
    used = set()
    for first, last, subtype, _ in ranges:
        if subtype == "normal_part":
            for t in tris[first:last + 1]:
                used.update(t)
    pts = [verts[i] for i in used] or verts
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    verts = [(x - cx, y - cy, z - lo[2]) for x, y, z in verts]
    return (omd, verts, tris, ranges), tuple(hi[i] - lo[i] for i in range(3))


def fmt(v):
    return ("%.6f" % v).rstrip("0").rstrip(".")


def esc(s):
    return (str(s).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def write_prusa(dst, objs):
    """objs: list of (name, settings, verts, tris, ranges, (x, y)) already placed."""
    model = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
             'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">',
             ' <metadata name="slic3rpe:Version3mf">1</metadata>',
             ' <metadata name="Application">bambu2prusa</metadata>',
             ' <resources>']
    conf = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>']
    build = []
    for n, (name, omd, verts, tris, ranges, (dx, dy)) in enumerate(objs, 1):
        model.append('  <object id="%d" type="model">' % n)
        model.append('   <mesh>')
        model.append('    <vertices>')
        model.extend('     <vertex x="%s" y="%s" z="%s"/>' % (fmt(x), fmt(y), fmt(z))
                     for x, y, z in verts)
        model.append('    </vertices>')
        model.append('    <triangles>')
        model.extend('     <triangle v1="%d" v2="%d" v3="%d"/>' % t for t in tris)
        model.append('    </triangles>')
        model.append('   </mesh>')
        model.append('  </object>')
        build.append('  <item objectid="%d" transform="1 0 0 0 1 0 0 0 1 %s %s 0" printable="1"/>'
                     % (n, fmt(dx), fmt(dy)))

        oset = map_settings(omd)
        conf.append(' <object id="%d" instances_count="1">' % n)
        conf.append('  <metadata type="object" key="name" value="%s"/>' % esc(name))
        conf.extend('  <metadata type="object" key="%s" value="%s"/>' % (k, esc(v))
                    for k, v in oset.items())
        for first, last, subtype, pmd in ranges:
            vtype = VOLUME_TYPES.get(subtype, "ModelPart")
            conf.append('  <volume firstid="%d" lastid="%d">' % (first, last))
            conf.append('   <metadata type="volume" key="name" value="%s"/>'
                        % esc(pmd.get("name", "")))
            conf.append('   <metadata type="volume" key="volume_type" value="%s"/>' % vtype)
            conf.append('   <metadata type="volume" key="modifier" value="%d"/>'
                        % (vtype == "ParameterModifier"))
            if vtype in ("ModelPart", "ParameterModifier"):
                for k, v in map_settings(pmd).items():
                    if oset.get(k) != v:
                        conf.append('   <metadata type="volume" key="%s" value="%s"/>'
                                    % (k, esc(v)))
            conf.append('  </volume>')
        conf.append(' </object>')
    model.append(' </resources>')
    model.append(' <build>')
    model.extend(build)
    model.append(' </build>')
    model.append('</model>')
    conf.append('</config>')

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
                   'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.'
                   '3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   'Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com'
                   '/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", "\n".join(model))
        z.writestr("Metadata/Slic3r_PE_model.config", "\n".join(conf))


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip() or "part"


def parse_scale(s):
    s = s.strip()
    return float(s[:-1]) / 100 if s.endswith("%") else float(s)


def main():
    ap = argparse.ArgumentParser(
        description="Convert a Bambu Studio 3MF into PrusaSlicer 3MF files (one per plate).")
    ap.add_argument("input", help="Bambu Studio .3mf")
    ap.add_argument("output", nargs="?",
                    help='output folder (default: "<input name> (PrusaSlicer)" next to the input)')
    ap.add_argument("--bed", default="250x210",
                    help="bed width x depth in mm, used to centre parts (default: 250x210, MK3/MK4)")
    ap.add_argument("--scale", default="1", help="uniform scale, as a factor or a percentage (e.g. 85%%)")
    a = ap.parse_args()
    bed = tuple(float(x) for x in a.bed.lower().split("x"))
    scale = parse_scale(a.scale)
    base = os.path.splitext(os.path.basename(a.input))[0]
    out = a.output or os.path.join(os.path.dirname(os.path.abspath(a.input)),
                                   base + " (PrusaSlicer)")
    os.makedirs(out, exist_ok=True)

    objects, items, plates, project = load(a.input)
    for pid, pname, ids in plates:
        objs, sizes = [], []
        for oid in ids:
            if oid not in objects:
                continue
            (omd, verts, tris, ranges), size = place(bake(objects, items, oid, scale))
            sizes.append(size)
            objs.append([omd.get("name", "part"), omd, verts, tris, ranges, None])
        if not objs:
            continue
        # Several objects on one plate: lay them out in a row, centred on the bed
        gap = 5.0
        total = sum(s[0] for s in sizes) + gap * (len(sizes) - 1)
        x = bed[0] / 2 - total / 2
        for o, s in zip(objs, sizes):
            o[5] = (x + s[0] / 2, bed[1] / 2)
            x += s[0] + gap
        name = "%d - %s" % (pid, pname) if pname else "%s plate %d" % (base, pid)
        dst = os.path.join(out, safe(name) + ".3mf")
        write_prusa(dst, objs)
        dims = ", ".join("%.1f x %.1f x %.1f mm" % s for s in sizes)
        warn = ("  (does not fit the %gx%g bed)" % bed
                if total > bed[0] or max(s[1] for s in sizes) > bed[1] else "")
        print("%s: %s%s" % (os.path.basename(dst), dims, warn))
    if project.get("spiral_mode") == "1":
        print("Note: the original project prints in spiral vase mode; turn it on in PrusaSlicer.")
    print("Done:", out)


if __name__ == "__main__":
    sys.exit(main())
