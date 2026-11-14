bl_info = {
    "name": "Gridfinity Fit",
    "author": "Andrew + Claude",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Gridfinity Fit",
    "description": "Packs Gridfinity baseplate pieces and spacers onto print beds and writes a multi-plate Bambu Studio 3MF",
    "category": "Import-Export",
}

import bpy, os, math, uuid, zipfile, struct, subprocess, sys
import numpy as np
from xml.sax.saxutils import escape

PLATE_GAP = 0.2          # Bambu Studio: plates are laid out bed_size * 1.2 apart
SIDES = ["Left", "Right", "Front", "Back"]
LAST_BED = []   # (bed X, bed Y, margin) read from the evaluated mesh
BAMBU_APP = "BambuStudio"
RELATIVE_PATH_OPTIONS = {'PATH_SUPPORTS_BLEND_RELATIVE'} if bpy.app.version >= (5, 2, 0) else set()


# ----------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------
def _idprops(md):
    """Legacy property storage; Blender 5.2 socket values are handled below."""
    properties = getattr(md, "properties", md)
    getter = getattr(properties, "bl_system_properties_get", None)
    if getter is not None:
        try:
            p = getter()
            if p is not None:
                return p
        except Exception:
            pass
    return md


def _get(md, key, default=None):
    inputs = getattr(getattr(md, "properties", None), "inputs", None)
    socket = getattr(inputs, key, None) if inputs is not None else None
    if socket is not None and hasattr(socket, "value"):
        return socket.value
    for holder in (_idprops(md), md):
        try:
            return holder[key]
        except (TypeError, KeyError):
            continue
    return default


def _draw_input(layout, md, key, text):
    inputs = getattr(getattr(md, "properties", None), "inputs", None)
    socket = getattr(inputs, key, None) if inputs is not None else None
    if socket is not None and hasattr(socket, "value"):
        layout.prop(socket, "value", text=text)
        return
    for holder in (_idprops(md), md):
        try:
            layout.prop(holder, '["%s"]' % key, text=text)
            return
        except Exception:
            continue
    layout.label(text="%s: %s" % (text, _get(md, key)))


def gn_inputs(ob):
    """Return {input name: value} of the Gridfinity modifier."""
    for md in ob.modifiers:
        if md.type == 'NODES' and md.node_group and md.node_group.name.startswith(("Gridfinity Fit", "Gridfinity Drawer")):
            out = {}
            for it in md.node_group.interface.items_tree:
                if getattr(it, "in_out", None) == 'INPUT' and it.socket_type != 'NodeSocketGeometry':
                    v = _get(md, it.identifier)
                    if v is None:
                        v = getattr(it, "default_value", None)
                    out[it.name] = v
            return out
    return None


def find_object(context):
    ob = context.active_object
    if ob and gn_inputs(ob) is not None:
        return ob
    for o in context.scene.objects:
        if gn_inputs(o) is not None:
            return o
    return None


def collect_parts(ob, depsgraph):
    ev = ob.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        me.calc_loop_triangles()
        nt = len(me.loop_triangles)
        if nt == 0:
            return []
        tris = np.empty(nt * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", tris)
        tris = tris.reshape(-1, 3)
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        att = me.attributes.get("gf_part")
        if att is None:
            raise RuntimeError("Mesh has no 'gf_part' attribute – is the Gridfinity node group up to date?")
        part = np.empty(len(me.vertices), dtype=np.int64)
        att.data.foreach_get("value", part)
        bed = me.attributes.get("gf_bed")
        LAST_BED[:] = list(bed.data[0].vector) if bed is not None and len(bed.data) else []
    finally:
        ev.to_mesh_clear()

    tp = part[tris[:, 0]]
    parts = []
    for pid in np.unique(tp):
        sel = tris[tp == pid]
        uniq, inv = np.unique(sel, return_inverse=True)
        v = co[uniq].copy()
        lo, hi = v.min(0), v.max(0)
        v[:, 0] -= (lo[0] + hi[0]) / 2
        v[:, 1] -= (lo[1] + hi[1]) / 2
        v[:, 2] -= lo[2]
        w, d, h = hi - lo
        pid = int(pid)
        if pid < 10000:
            k = pid - 1
            name = "Base %d-%d (%dx%d)" % (k % 512 + 1, k // 512 + 1, round(w / 42), round(d / 42))
            kind = 0
        else:
            side, seg = divmod(pid - 10000, 1000)
            name = "Spacer %s %d (%.1f x %.1f)" % (SIDES[side], seg + 1, w, d)
            kind = 1
        parts.append(dict(pid=pid, name=name, kind=kind, verts=v, faces=inv.reshape(-1, 3),
                          w=float(w), d=float(d), h=float(h)))
    return parts


# ----------------------------------------------------------------------------
# packing (shelf packing with 90° rotation, one bed = one Bambu plate)
# ----------------------------------------------------------------------------
def pack(parts, UW, UD, gap):
    parts = sorted(parts, key=lambda p: (p["kind"], -max(p["w"], p["d"]), -min(p["w"], p["d"])))
    plates = []
    for p in parts:
        opts = []
        for rot in (False, True):
            w, d = (p["d"], p["w"]) if rot else (p["w"], p["d"])
            if w <= UW + 1e-6 and d <= UD + 1e-6:
                opts.append((rot, w, d))
        if not opts:
            raise RuntimeError("%s (%.1f x %.1f mm) does not fit the %.0f x %.0f mm usable bed area"
                               % (p["name"], p["w"], p["d"], UW, UD))
        best = None
        for pi, pl in enumerate(plates):
            for si, sh in enumerate(pl["shelves"]):
                for rot, w, d in opts:
                    if sh["x"] + w <= UW + 1e-6 and d <= sh["h"] + 1e-6:
                        score = (pi, sh["h"] - d)
                        if best is None or score < best[0]:
                            best = (score, pi, si, rot, w, d)
            if best is not None:
                break
        if best is None:
            # new shelf: lay long parts along X so shelves stay thin
            rot, w, d = min(opts, key=lambda o: o[2])
            target = None
            for pi, pl in enumerate(plates):
                if pl["y"] + d <= UD + 1e-6:
                    target = pi
                    break
            if target is None:
                plates.append(dict(shelves=[], y=0.0, items=[]))
                target = len(plates) - 1
            pl = plates[target]
            pl["shelves"].append(dict(y=pl["y"], h=d, x=0.0))
            pl["y"] += d + gap
            best = (None, target, len(pl["shelves"]) - 1, rot, w, d)
        _, pi, si, rot, w, d = best
        pl = plates[pi]
        sh = pl["shelves"][si]
        pl["items"].append(dict(part=p, rot=rot, x=sh["x"] + w / 2, y=sh["y"] + d / 2, w=w, d=d))
        sh["x"] += w + gap
    return plates


# ----------------------------------------------------------------------------
# 3MF writer (Bambu Studio project layout)
# ----------------------------------------------------------------------------
def _fmt(a):
    return ("%.4f" % a).rstrip("0").rstrip(".")


def mesh_xml(obj_id, verts, faces):
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n'
           '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
           'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
           'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
           ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
           '  <object id="%d" p:UUID="%s" type="model">\n   <mesh>\n    <vertices>\n' % (obj_id, uuid.uuid4())]
    out.append("".join('     <vertex x="%s" y="%s" z="%s"/>\n' % (_fmt(x), _fmt(y), _fmt(z)) for x, y, z in verts))
    out.append('    </vertices>\n    <triangles>\n')
    out.append("".join('     <triangle v1="%d" v2="%d" v3="%d"/>\n' % (a, b, c) for a, b, c in faces))
    out.append('    </triangles>\n   </mesh>\n  </object>\n </resources>\n <build/>\n</model>\n')
    return "".join(out)


def write_3mf(path, plates, bedX, bedY, margin, title, settings_src=None):
    n = len(plates)
    cols = max(1, math.ceil(math.sqrt(n)))
    UW, UD = bedX - 2 * margin, bedY - 2 * margin
    objs = []   # (mesh_id, wrap_id, name, verts, faces, tx, ty, tz, plate_idx)
    next_id = 1
    for pi, pl in enumerate(plates):
        col, row = pi % cols, pi // cols
        ox, oy = col * bedX * (1 + PLATE_GAP), -row * bedY * (1 + PLATE_GAP)
        # centre the packed block on the bed
        if pl["items"]:
            minx = min(it["x"] - it["w"] / 2 for it in pl["items"]); maxx = max(it["x"] + it["w"] / 2 for it in pl["items"])
            miny = min(it["y"] - it["d"] / 2 for it in pl["items"]); maxy = max(it["y"] + it["d"] / 2 for it in pl["items"])
            sx = margin + (UW - (maxx - minx)) / 2 - minx
            sy = margin + (UD - (maxy - miny)) / 2 - miny
        for it in pl["items"]:
            p = it["part"]
            v = p["verts"].copy()
            if it["rot"]:
                v[:, 0], v[:, 1] = -p["verts"][:, 1], p["verts"][:, 0].copy()
            h = p["h"]
            v[:, 2] -= h / 2
            objs.append((next_id, next_id + 1, p["name"], v, p["faces"],
                         ox + it["x"] + sx, oy + it["y"] + sy, h / 2, pi))
            next_id += 2

    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("[Content_Types].xml",
               '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
               ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
               ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
               ' <Default Extension="png" ContentType="image/png"/>\n'
               ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>')
    z.writestr("_rels/.rels",
               '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
               ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
               '</Relationships>')
    rels = ['<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n']
    for k, (mid, wid, name, v, f, tx, ty, tz, pi) in enumerate(objs):
        rels.append(' <Relationship Target="/3D/Objects/object_%d.model" Id="rel-%d" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n' % (mid, k + 1))
        z.writestr("3D/Objects/object_%d.model" % mid, mesh_xml(mid, v, f))
    rels.append('</Relationships>')
    z.writestr("3D/_rels/3dmodel.model.rels", "".join(rels))

    m = ['<?xml version="1.0" encoding="UTF-8"?>\n'
         '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
         'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
         'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
         ' <metadata name="Application">%s</metadata>\n'
         ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
         ' <metadata name="Title">%s</metadata>\n'
         ' <metadata name="Designer">Blender Gridfinity Drawer</metadata>\n'
         ' <resources>\n' % (BAMBU_APP, escape(title))]
    for mid, wid, name, v, f, tx, ty, tz, pi in objs:
        m.append('  <object id="%d" p:UUID="%s" type="model">\n   <components>\n'
                 '    <component p:path="/3D/Objects/object_%d.model" objectid="%d" p:UUID="%s" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
                 '   </components>\n  </object>\n' % (wid, uuid.uuid4(), mid, mid, uuid.uuid4()))
    m.append(' </resources>\n <build p:UUID="%s">\n' % uuid.uuid4())
    for mid, wid, name, v, f, tx, ty, tz, pi in objs:
        m.append('  <item objectid="%d" p:UUID="%s" transform="1 0 0 0 1 0 0 0 1 %s %s %s" printable="1"/>\n'
                 % (wid, uuid.uuid4(), _fmt(tx), _fmt(ty), _fmt(tz)))
    m.append(' </build>\n</model>\n')
    z.writestr("3D/3dmodel.model", "".join(m))

    c = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for mid, wid, name, v, f, tx, ty, tz, pi in objs:
        c.append('  <object id="%d">\n    <metadata key="name" value="%s"/>\n    <metadata key="extruder" value="1"/>\n'
                 '    <part id="%d" subtype="normal_part">\n      <metadata key="name" value="%s"/>\n'
                 '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                 '      <mesh_stat face_count="%d" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
                 '    </part>\n  </object>\n' % (wid, escape(name, {'"': "&quot;"}), mid, escape(name, {'"': "&quot;"}), len(f)))
    ident = 100
    for pi, pl in enumerate(plates):
        kinds = {it["part"]["kind"] for it in pl["items"]}
        label = "Base" if kinds == {0} else "Spacers" if kinds == {1} else "Base + Spacers"
        c.append('  <plate>\n    <metadata key="plater_id" value="%d"/>\n    <metadata key="plater_name" value="%s %d"/>\n'
                 '    <metadata key="locked" value="false"/>\n' % (pi + 1, label, pi + 1))
        for mid, wid, name, v, f, tx, ty, tz, opi in objs:
            if opi == pi:
                ident += 1
                c.append('    <model_instance>\n      <metadata key="object_id" value="%d"/>\n'
                         '      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="%d"/>\n'
                         '    </model_instance>\n' % (wid, ident))
        c.append('  </plate>\n')
    c.append('  <assemble>\n')
    for mid, wid, name, v, f, tx, ty, tz, pi in objs:
        c.append('   <assemble_item object_id="%d" instance_id="0" transform="1 0 0 0 1 0 0 0 1 %s %s %s" offset="0 0 0" />\n'
                 % (wid, _fmt(tx), _fmt(ty), _fmt(tz)))
    c.append('  </assemble>\n</config>\n')
    z.writestr("Metadata/model_settings.config", "".join(c))
    if settings_src and os.path.isfile(settings_src):
        with zipfile.ZipFile(settings_src) as src:
            for nm in ("Metadata/project_settings.config", "Metadata/filament_sequence.json"):
                if nm in src.namelist():
                    z.writestr(nm, src.read(nm))
    z.close()
    return len(objs)


def write_stl(path, verts, faces):
    tri = verts[faces]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(nrm, axis=1, keepdims=True); ln[ln == 0] = 1
    nrm = nrm / ln
    rec = np.zeros(len(faces), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"] = nrm; rec["v"] = tri
    with open(path, "wb") as fh:
        fh.write(b"Gridfinity Drawer".ljust(80, b" "))
        fh.write(struct.pack("<I", len(faces)))
        fh.write(rec.tobytes())


def open_in_bambu(path):
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["open", "-a", "BambuStudio", path])
            if r.returncode != 0:
                subprocess.run(["open", path])
        elif sys.platform.startswith("win"):
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print("Could not open Bambu Studio:", e)


def build_layout(context):
    ob = find_object(context)
    if ob is None:
        raise RuntimeError("No object with the 'Gridfinity Drawer' geometry nodes modifier found")
    ins = gn_inputs(ob)
    parts = collect_parts(ob, context.evaluated_depsgraph_get())
    if len(LAST_BED) == 3:
        bedX, bedY, margin = (float(x) for x in LAST_BED)
    else:
        def val(k, d):
            v = ins.get(k)
            return d if v is None else float(v)
        bedX, bedY, margin = val("Bed X", 180.0), val("Bed Y", 180.0), val("Bed Margin", 3.0)
    if not parts:
        raise RuntimeError("Nothing to export")
    gap = context.scene.gf_export.part_gap
    plates = pack(parts, bedX - 2 * margin, bedY - 2 * margin, gap)
    return ob, parts, plates, bedX, bedY, margin


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
class GF_ExportProps(bpy.types.PropertyGroup):
    filepath: bpy.props.StringProperty(name="3MF file", subtype='FILE_PATH', default="//Gridfinity Fit.3mf", options=RELATIVE_PATH_OPTIONS)
    stl_dir: bpy.props.StringProperty(name="STL folder", subtype='DIR_PATH', default="//Gridfinity Fit STL/", options=RELATIVE_PATH_OPTIONS)
    part_gap: bpy.props.FloatProperty(name="Gap between parts", default=4.0, min=0.0, max=50.0, subtype='DISTANCE')
    open_after: bpy.props.BoolProperty(name="Open in Bambu Studio", default=True)
    settings_3mf: bpy.props.StringProperty(name="Printer settings from", subtype='FILE_PATH', default="", options=RELATIVE_PATH_OPTIONS,
        description="Optional: any 3MF saved by Bambu Studio with your A1 mini / filament preset. Its settings are copied into the export")
    summary: bpy.props.StringProperty(default="")


class GF_OT_export_bambu(bpy.types.Operator):
    bl_idname = "gridfinity.export_bambu"
    bl_label = "Export 3MF for Bambu Studio"
    bl_description = "Split into parts, pack them onto print beds and write a multi-plate Bambu Studio 3MF"

    def execute(self, context):
        props = context.scene.gf_export
        try:
            ob, parts, plates, bedX, bedY, margin = build_layout(context)
            path = bpy.path.abspath(props.filepath)
            if not path.lower().endswith(".3mf"):
                path += ".3mf"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            n = write_3mf(path, plates, bedX, bedY, margin, ob.name, bpy.path.abspath(props.settings_3mf) if props.settings_3mf else None)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        props.summary = "%d parts on %d plates" % (n, len(plates))
        self.report({'INFO'}, "Saved %s: %s" % (path, props.summary))
        if props.open_after:
            open_in_bambu(path)
        return {'FINISHED'}


def letter_index(index):
    """Zero-based index as A, B, ..., Z, AA without numeric suffixes."""
    label = ""
    while index >= 0:
        index, digit = divmod(index, 26)
        label = chr(65 + digit) + label
        index -= 1
    return label


def stl_filename(plate_index, part):
    pid = part["pid"]
    if pid < 10000:
        col, row = (pid - 1) % 512, (pid - 1) // 512
        label = "Base %s %s" % (letter_index(col), letter_index(row))
    else:
        side, segment = divmod(pid - 10000, 1000)
        label = "Spacer %s %s" % (SIDES[side], letter_index(segment))
    return "Gridfinity Fit Plate %s %s.stl" % (letter_index(plate_index), label)


class GF_OT_export_stl(bpy.types.Operator):
    bl_idname = "gridfinity.export_stl"
    bl_label = "Export STL per part"
    bl_description = "Write one STL per part, file names prefixed with their plate letter"

    def execute(self, context):
        props = context.scene.gf_export
        try:
            ob, parts, plates, *_ = build_layout(context)
            folder = bpy.path.abspath(props.stl_dir)
            os.makedirs(folder, exist_ok=True)
            n = 0
            for pi, pl in enumerate(plates):
                for it in pl["items"]:
                    p = it["part"]
                    write_stl(os.path.join(folder, stl_filename(pi, p)), p["verts"], p["faces"])
                    n += 1
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, "%d STL files written to %s" % (n, folder))
        return {'FINISHED'}


class GF_OT_preview_layout(bpy.types.Operator):
    bl_idname = "gridfinity.preview_layout"
    bl_label = "Count plates"
    bl_description = "Show how many parts and print beds the current settings need"

    def execute(self, context):
        try:
            ob, parts, plates, *_ = build_layout(context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        nb = sum(1 for p in parts if p["kind"] == 0)
        context.scene.gf_export.summary = "%d base pieces + %d spacers → %d plates" % (nb, len(parts) - nb, len(plates))
        self.report({'INFO'}, context.scene.gf_export.summary)
        return {'FINISHED'}


class GF_PT_panel(bpy.types.Panel):
    bl_label = "Gridfinity Fit"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gridfinity Fit"

    def draw(self, context):
        lay = self.layout
        props = context.scene.gf_export
        ob = find_object(context)
        if ob is None:
            lay.label(text="No Gridfinity object", icon='ERROR')
            return
        ins = gn_inputs(ob)
        md = next(m for m in ob.modifiers if m.type == 'NODES' and m.node_group and m.node_group.name.startswith(("Gridfinity Fit", "Gridfinity Drawer")))
        ids = {it.name: it.identifier for it in md.node_group.interface.items_tree if getattr(it, "in_out", None) == 'INPUT'}
        col = lay.column(align=True)
        col.label(text="Drawer")
        for k in ("Drawer Width X", "Drawer Depth Y", "Clearance"):
            _draw_input(col, md, ids[k], k)
        col = lay.column(align=True)
        col.label(text="Printer bed (A1 mini = 180 × 180)")
        for k in ("Bed X", "Bed Y", "Bed Margin"):
            _draw_input(col, md, ids[k], k)
        _draw_input(col, md, ids["Split For Printer"], "Split For Printer")
        lay.prop(props, "part_gap")
        lay.separator()
        lay.prop(props, "filepath")
        lay.prop(props, "settings_3mf")
        lay.prop(props, "open_after")
        lay.operator(GF_OT_export_bambu.bl_idname, icon='EXPORT')
        row = lay.row(align=True)
        row.operator(GF_OT_preview_layout.bl_idname, icon='INFO')
        if props.summary:
            lay.label(text=props.summary)
        lay.separator()
        lay.prop(props, "stl_dir")
        lay.operator(GF_OT_export_stl.bl_idname, icon='FILE')


classes = (GF_ExportProps, GF_OT_export_bambu, GF_OT_export_stl, GF_OT_preview_layout, GF_PT_panel)


def register():
    for c in classes:
        try:
            bpy.utils.register_class(c)
        except ValueError:
            bpy.utils.unregister_class(c)
            bpy.utils.register_class(c)
    bpy.types.Scene.gf_export = bpy.props.PointerProperty(type=GF_ExportProps)


def unregister():
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    del bpy.types.Scene.gf_export


if __name__ == "__main__":
    register()
