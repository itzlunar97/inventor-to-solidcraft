# fc_solidify.py - run with FreeCAD's console:  freecadcmd fc_solidify.py
#   env FC_IN=<shape.step>  FC_OUT_STEP=<solid.step>  FC_OUT_STL=<mesh.stl>
# For a STEP whose faces SolidCraft could not close into a solid: sew the faces in FreeCAD's
# kernel and export the closed shells as solids; when nothing closes, export a mesh (STL)
# of the surfaces instead. Writes <FC_OUT_STEP>.json with what it did.
import os, json
import FreeCAD, Part

src, out_step, out_stl = os.environ['FC_IN'], os.environ['FC_OUT_STEP'], os.environ['FC_OUT_STL']
res = {'solids': 0, 'faces': 0, 'route': None}
def read_any(path):
    try:
        return Part.read(path)
    except Exception as e:
        res['part_read_error'] = str(e)[:160]
    import Import
    doc = FreeCAD.newDocument('fix')
    Import.insert(path, doc.Name)
    shapes = [o.Shape for o in doc.Objects if hasattr(o, 'Shape') and not o.Shape.isNull()]
    return Part.makeCompound(shapes)


try:
    shape = read_any(src)
    res['faces'] = len(shape.Faces)
    solids = list(shape.Solids)
    if not solids:
        sewn = shape.copy()
        sewn.sewShape()
        for sh in sewn.Shells:
            if sh.isClosed():
                try:
                    s = Part.Solid(sh)
                    if s.Volume < 0:
                        s.reverse()
                    solids.append(s)
                except Exception:
                    pass
    if solids:
        Part.makeCompound(solids).exportStep(out_step)
        res.update(solids=len(solids), route='sewn-solids')
    else:
        import Mesh, MeshPart
        m = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.02, AngularDeflection=0.2)
        m.write(out_stl)
        res.update(route='mesh', triangles=m.CountFacets)
except Exception as e:
    res['error'] = f'{type(e).__name__}: {e}'
with open(out_step + '.json', 'w') as f:
    json.dump(res, f)
