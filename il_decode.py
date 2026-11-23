# il_decode.py - headless Inventor decoder built on the InventorLoader add-on's readers (GPL-2.0).
# Run with FreeCAD's console:  freecadcmd il_decode.py
#   env FC_IN=<file.ipt>  FC_OUT=<dir>
# Writes <name>.decode.json (parameters, sketches, features as decoded node trees) and
# <name>.step (exact ACIS solid via InventorLoader's own Acis2Step writer). No GUI is used.
import os, sys, json, types, traceback
import FreeCAD

IN  = os.environ['FC_IN']
OUT = os.environ['FC_OUT']
os.makedirs(OUT, exist_ok=True)
base = os.path.splitext(os.path.basename(IN))[0]
status = {'file': IN, 'stage': 'start'}


def save_status():
    with open(os.path.join(OUT, base + '.status.json'), 'w', encoding='utf-8') as f:
        json.dump(status, f, indent=1, default=str)


save_status()
import faulthandler
_crash = open(os.path.join(OUT, base + '.crash.txt'), 'w')
faulthandler.enable(file=_crash, all_threads=True)
faulthandler.dump_traceback_later(int(os.environ.get('FC_HANG_SECS', '100')), exit=True, file=_crash)

# --- console mode: give InventorLoader inert stand-ins for its GUI-only imports ------------
class _NoOp:
    def __call__(self, *a, **k): return _NoOp()
    def __getattr__(self, n): return _NoOp()
    def __bool__(self): return False
    def __iter__(self): return iter(())


for _m in ('FreeCADGui', 'ImportGui'):
    if _m not in sys.modules:
        _mod = types.ModuleType(_m)
        _mod.__getattr__ = lambda n: _NoOp()
        sys.modules[_m] = _mod

mod = os.path.join(FreeCAD.getUserAppDataDir(), 'Mod', 'InventorLoader')
sys.path.insert(0, mod)

# Qt aborts the process if a widget (message box, icon) is built without a QApplication:
# make an invisible one, and turn every message box into a logged no-op.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
status['dialogs'] = []
try:
    from PySide import QtGui as _QtGui
    try:
        from PySide import QtWidgets as _QtW
    except ImportError:
        _QtW = _QtGui
    _app = _QtW.QApplication.instance() or _QtW.QApplication(['il_decode', '-platform', 'offscreen'])
    for _QM in {getattr(_QtGui, 'QMessageBox', None), getattr(_QtW, 'QMessageBox', None)} - {None}:
        def _static(kind, _QM=_QM):
            def f(*a, **k):
                status['dialogs'].append(f'{kind}: ' + ' | '.join(str(x) for x in a[1:3]))
                return _QM.Ok
            return f
        for _k in ('critical', 'warning', 'information', 'question', 'about'):
            setattr(_QM, _k, staticmethod(_static(_k)))
        _QM.exec_ = lambda self, *a, **k: (status['dialogs'].append('box: ' + self.text()), 0)[1]
        _QM.exec = _QM.exec_
except Exception as _e:
    status['dialogs'].append(f'qt setup failed: {_e}')

try:
    import importerUtils
    importerUtils.chooseImportStrategy = lambda *a, **k: importerUtils.STRATEGY_SAT
    importerUtils.chooseImportStrategyAcis = lambda *a, **k: importerUtils.STRATEGY_SAT
    importerUtils.setStrategy(importerUtils.STRATEGY_SAT)
    import Import_IPT
    Import_IPT.chooseImportStrategy = importerUtils.chooseImportStrategy
    # an unreadable embedded attachment (OLE "Ole10Native" object) must not stop the model read
    _ole10 = Import_IPT.ReadOle10Native

    def _safe_ole10(*a, **k):
        try:
            return _ole10(*a, **k)
        except Exception as e:
            status.setdefault('skipped_embeddings', []).append(f'{type(e).__name__}: {e}'[:120])
    Import_IPT.ReadOle10Native = _safe_ole10
    status['stage'] = 'imported'
    save_status()

    ole = Import_IPT.checkVersion(IN)
    if ole is None:
        raise RuntimeError('unsupported Inventor version')
    Import_IPT.read(ole)
    status['stage'] = 'read'
    save_status()
    import importerClasses
    model = importerClasses.getModel()

    # ---------------------------------------------------------------- generic node dumper
    def val(v, depth=0):
        """JSON-safe view of an InventorLoader property value; node refs become {ref,type}."""
        if depth > 4:
            return repr(v)[:80]
        if isinstance(v, (bool, int, float, str)) or v is None:
            return v
        cls = type(v).__name__
        if cls == 'SecNodeRef':
            tgt = getattr(v, 'data', None) or getattr(v, 'node', None)
            return {'ref': v.index, 'type': getattr(tgt, 'typeName', None) if tgt is not None else None}
        if hasattr(v, 'typeName') and hasattr(v, 'properties'):
            return {'ref': getattr(v, 'index', None), 'type': v.typeName}
        if hasattr(v, 'toStandard'):
            try:
                return {'value': v.toStandard(), 'raw': getattr(v, 'x', None) if hasattr(v, 'x') and not hasattr(v, 'y') else None}
            except Exception:
                pass
        if hasattr(v, 'x') and hasattr(v, 'y'):
            return [getattr(v, a) for a in ('x', 'y', 'z') if hasattr(v, a)]
        if isinstance(v, (list, tuple)):
            return [val(x, depth + 1) for x in list(v)[:256]]
        if isinstance(v, dict):
            return {str(k): val(x, depth + 1) for k, x in list(v.items())[:256]}
        return str(v)[:160]

    def node_name(n):
        nm = getattr(n, 'name', None)
        if isinstance(nm, str) and nm:
            return nm
        for key in ('label', 'name'):
            try:
                x = n.get(key)
            except Exception:
                x = None
            if isinstance(x, str) and x:
                return x
            if x is not None and hasattr(x, 'get'):
                for k2 in ('nameOrig', 'name', 'value'):
                    try:
                        y = x.get(k2)
                        if isinstance(y, str) and y:
                            return y
                    except Exception:
                        pass
        nm = getattr(n, 'name', None)
        return nm if isinstance(nm, str) and nm else None

    def dump_node(n):
        try:
            nm = node_name(n)
        except Exception:
            nm = None
        d = {'index': getattr(n, 'index', None), 'type': getattr(n, 'typeName', None), 'name': nm}
        for flag in ('construction', 'visible'):
            v = getattr(n, flag, None)
            if isinstance(v, bool):
                d[flag] = v
        props = {}
        for k, pv in (getattr(n, 'properties', {}) or {}).items():
            v = pv[0] if isinstance(pv, tuple) and len(pv) == 2 else pv
            try:
                props[str(k)] = val(v)
            except Exception as e:
                props[str(k)] = f'<unreadable: {e}>'
        d['props'] = props
        # tree wrappers (ParameterNode, SketchNode, FeatureNode...) carry the resolved values
        dn = getattr(n, 'node', None)
        for obj in (dn, n):
            if obj is None:
                continue
            if d.get('name') is None:
                nm = getattr(obj, 'name', None)
                if isinstance(nm, str) and nm:
                    d['name'] = nm
            for meth, key in (('getValue', 'value'), ('getUnitName', 'unit'), ('getRefText', 'ref_text')):
                if key in d:
                    continue
                f = getattr(obj, meth, None)
                if callable(f):
                    try:
                        r = f()
                        if r is not None and r != '':
                            d[key] = str(r) if not isinstance(r, (int, float)) else r
                    except Exception:
                        pass
            # the parameter's real expression, rebuilt by InventorLoader from the formula tree
            f = getattr(obj, 'getParameterFormula', None)
            if callable(f) and 'expr' not in d and d.get('type') == 'Parameter':
                try:
                    fr = obj.get('formula')
                    if fr is not None:
                        d['expr'] = str(f(fr, True))
                except Exception as e:
                    d['expr_error'] = str(e)[:120]
            f = getattr(obj, 'getSubTypeName', None)
            if callable(f) and 'subtype' not in d and d.get('type') == 'Feature':
                try:
                    d['subtype'] = str(f())
                except Exception:
                    pass
            f = getattr(obj, 'getParticipants', None)
            if callable(f) and 'participants' not in d and d.get('type') == 'Feature':
                try:
                    d['participants'] = [getattr(p, 'index', None) for p in (f() or [])]
                except Exception:
                    pass
            f = getattr(obj, 'getFormula', None)
            if callable(f) and 'formula' not in d:
                try:
                    r = f(True)
                    if r:
                        d['formula'] = str(r)
                except Exception:
                    pass
            if hasattr(obj, 'get') and 'valueNominal' not in props:
                try:
                    vn = obj.get('valueNominal')
                    if vn is not None:
                        props['valueNominal'] = val(vn)
                except Exception:
                    pass
        return d

    dc = model.getDC()
    nodes = []
    if dc is not None:
        # The finished nodes hang off segment.tree as DataNode wrappers (ParameterNode,
        # SketchNode, FeatureNode...) around the SecNode data; walk it, then add any
        # element-table nodes the tree did not reach.
        seen = {}

        # iterative walk; the tree has cross links, so visit each wrapper once
        visited = set()
        stack = [getattr(dc, 'tree', None)]
        while stack:
            dn = stack.pop()
            if dn is None or id(dn) in visited:
                continue
            visited.add(id(dn))
            if getattr(dn, 'isRef', False):
                continue
            data = getattr(dn, 'data', None)
            if data is not None and getattr(data, 'index', None) is not None and data.index not in seen:
                try:
                    data.node = dn
                    d = dump_node(data)
                except Exception as e:
                    d = {'index': data.index, 'type': getattr(data, 'typeName', None), 'props': {}, 'error': str(e)}
                seen[data.index] = d
            stack.extend(getattr(dn, 'children', []) or [])
        for idx, n in getattr(dc, 'elementNodes', {}).items():
            if idx not in seen:
                try:
                    d = dump_node(n)
                except Exception as e:
                    d = {'type': getattr(n, 'typeName', None), 'props': {}, 'error': str(e)}
                d['index'] = idx
                seen[idx] = d
        nodes = [seen[k] for k in sorted(seen)]
        dbg = os.environ.get('FC_DEBUG_IDX')
        if dbg:
            n = dc.elementNodes.get(int(dbg))
            info = {'class': type(n).__name__, 'attrs': sorted(k for k in vars(n).keys())}
            info['properties'] = {k: str(v)[:120] for k, v in (getattr(n, 'properties', {}) or {}).items()}
            dn = getattr(n, 'node', None)
            info['node_class'] = type(dn).__name__ if dn is not None else None
            for k in ('name', 'typeName'):
                info[k] = str(getattr(n, k, None))
            status['debug_node'] = info
    type_counts = {}
    for n in nodes:
        type_counts[n.get('type')] = type_counts.get(n.get('type'), 0) + 1
    status['dc_nodes'] = len(nodes)
    status['dc_types'] = dict(sorted(type_counts.items(), key=lambda kv: -kv[1])[:60])

    with open(os.path.join(OUT, base + '.dc.json'), 'w', encoding='utf-8') as f:
        json.dump(nodes, f, indent=0, default=str)
    status['stage'] = 'dc dumped'
    save_status()

    # assemblies: the occurrence segment (component defs, occurrence ids, placements) and
    # the browser segment (occurrence names) go out too
    if IN.lower().endswith('.iam'):
        for seg in model.RSeMetaData.values():
            sname = getattr(seg, 'name', '') or ''
            if sname not in ('AmRxSegment', 'AmBrowserSegment'):
                continue
            out_nodes = []
            for idx, n in sorted(getattr(seg, 'elementNodes', {}).items()):
                try:
                    d = dump_node(n)
                except Exception as e:
                    d = {'type': getattr(n, 'typeName', None), 'props': {}, 'error': str(e)}
                d['index'] = idx
                out_nodes.append(d)
            with open(os.path.join(OUT, f'{base}.{sname}.json'), 'w', encoding='utf-8') as f:
                json.dump(out_nodes, f, indent=0, default=str)
            status.setdefault('segments', []).append(sname)
        save_status()

    # ---------------------------------------------------------------- exact solid -> STEP
    try:
        import Acis, Acis2Step, importerSAT, shutil
        brep = model.getBRep()
        steps = []
        for i, asm in enumerate(getattr(brep, 'AcisList', []) or []):
            Acis.setReader(asm.SAT)
            acis = Acis.getReader()
            bodies = importerSAT.resolveNodes(acis)
            produced = Acis2Step.export(acis.name or base, acis.header, bodies)
            out = os.path.join(OUT, f'{base}.step' if i == 0 else f'{base}.{i}.step')
            if produced and os.path.exists(produced):
                shutil.copyfile(produced, out)
                steps.append(os.path.basename(out))
        status['step'] = steps
        status['step_route'] = 'acis2step'
        # Acis2Step sometimes writes no faces at all for a large body: rebuild it through
        # InventorLoader's ACIS reader into FreeCAD shapes instead, sew, and export that
        main = os.path.join(OUT, f'{base}.step')
        has_faces = os.path.exists(main) and 'ADVANCED_FACE' in open(main, encoding='latin-1').read()
        acis_list = getattr(brep, 'AcisList', []) or []
        status['step_has_faces'] = has_faces
        status['acis_blocks'] = len(acis_list)
        if not has_faces and acis_list:
            import Part
            doc = FreeCAD.newDocument('satroute')
            FreeCAD.setActiveDocument(doc.Name)
            for asm in acis_list:
                Acis.setReader(asm.SAT)
                try:
                    bodies = importerSAT.resolveNodes(Acis.getReader())
                    status['sat_route_bodies'] = len(bodies)
                    for body in bodies:
                        importerSAT.buildBody(None, body)
                except Exception as e:
                    status.setdefault('sat_route_errors', []).append(f'{type(e).__name__}: {e}'[:200])
                    status['sat_route_trace'] = traceback.format_exc()[-1200:]
            status['sat_route_objects'] = len(doc.Objects)
            shapes = [o.Shape for o in doc.Objects if hasattr(o, 'Shape') and not o.Shape.isNull()]
            if shapes:
                comp = Part.makeCompound(shapes)
                solids = []
                try:
                    sewn = comp.copy()
                    sewn.sewShape()
                    for sh in sewn.Shells:
                        if sh.isClosed():
                            try:
                                solids.append(Part.Solid(sh))
                            except Exception:
                                pass
                except Exception as e:
                    status.setdefault('sat_route_errors', []).append(f'sew: {e}'[:160])
                out_shape = Part.makeCompound(solids) if solids else comp
                out_shape.exportStep(main)
                status['step'] = [os.path.basename(main)]
                status['step_route'] = 'freecad-sat' + ('-solid' if solids else '-surfaces')
                status['sat_route_solids'] = len(solids)
    except Exception as e:
        status['step_error'] = f'{type(e).__name__}: {e}'
        status['step_trace'] = traceback.format_exc()[-1500:]
    status['stage'] = 'done'
except Exception as e:
    status['error'] = f'{type(e).__name__}: {e}'
    status['trace'] = traceback.format_exc()[-2500:]
save_status()
print('RESULT', base, status['stage'], status.get('error', ''))
