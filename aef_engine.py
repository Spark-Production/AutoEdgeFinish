"""
AutoEdgeFinish engine: edge classification + ordered, failure-tolerant
fillet/chamfer application. No UI code here, so it can be driven by the
dialog (AutoEdgeFinish.py) or called headless from a script.
"""
import adsk.core, adsk.fusion

TOL_PAR = 0.999
EPS = 0.002          # cm, convexity probe offset
LEVEL_TOL = 0.001    # cm, "same height" tolerance for outermost-face test

# Order matters: inside corners first (tool radii), then outside corners,
# then floors, then the loops that run around everything, then holes.
GROUPS = [
    ('in_vert',  'Inside vertical corners'),
    ('out_vert', 'Outside vertical corners'),
    ('floor',    'Inside floor edges'),
    ('other',    'Other convex edges'),
    ('top',      'Top edges'),
    ('bottom',   'Bottom edges'),
    ('holes',    'Hole rims'),
]
GROUP_KEYS = [g[0] for g in GROUPS]
GROUP_NAMES = dict(GROUPS)

AXES = {
    '+Z': (0, 0, 1), '-Z': (0, 0, -1),
    '+Y': (0, 1, 0), '-Y': (0, -1, 0),
    '+X': (1, 0, 0), '-X': (-1, 0, 0),
}

# ------------------------------------------------------------------ presets
# group -> [treatment, size, angle_deg, g2]   treatment: none | fillet | chamfer
def _p(**g):
    base = {k: ['none', '0.5 mm', 45.0, False] for k in GROUP_KEYS}
    base.update(g)
    return base

BUILTIN_PRESETS = {
    'FDM print-ready': {
        'groups': _p(out_vert=['fillet', '2 mm', 45.0, False],
                     in_vert=['fillet', '1 mm', 45.0, False],
                     top=['fillet', '1 mm', 45.0, False],
                     bottom=['chamfer', '0.5 mm', 45.0, False],
                     holes=['chamfer', '0.4 mm', 45.0, False]),
        'options': {'bottom_outermost': True, 'top_outermost': False},
        'note': 'Bed-side edges get a chamfer (no overhang, hides elephant\'s foot); '
                'top edges and corners get fillets; hole rims get a lead-in chamfer.',
    },
    'FDM functional / fit-up': {
        'groups': _p(out_vert=['fillet', '1 mm', 45.0, False],
                     top=['chamfer', '0.3 mm', 45.0, False],
                     bottom=['chamfer', '0.4 mm', 45.0, False],
                     holes=['chamfer', '0.5 mm', 45.0, False]),
        'options': {'bottom_outermost': True, 'top_outermost': False},
        'note': 'Small, dimension-friendly edge breaks for parts that mate with others.',
    },
    'Handheld / soft-touch (print)': {
        'groups': _p(out_vert=['fillet', '4 mm', 45.0, True],
                     top=['fillet', '2 mm', 45.0, True],
                     bottom=['chamfer', '0.8 mm', 45.0, False],
                     other=['fillet', '1 mm', 45.0, False],
                     holes=['fillet', '0.5 mm', 45.0, False]),
        'options': {'bottom_outermost': True, 'top_outermost': False},
        'note': 'Large G2 rounds where hands go; bed edge still chamfered so it prints.',
    },
    'Machined break-edge': {
        'groups': _p(out_vert=['chamfer', '0.4 mm', 45.0, False],
                     top=['chamfer', '0.4 mm', 45.0, False],
                     bottom=['chamfer', '0.4 mm', 45.0, False],
                     other=['chamfer', '0.4 mm', 45.0, False],
                     holes=['chamfer', '0.4 mm', 45.0, False]),
        'options': {'bottom_outermost': False, 'top_outermost': False},
        'note': '~0.015" x 45 deg on every sharp outside edge. Typical "break all sharp edges" callout.',
    },
    'Machined, tool-friendly corners': {
        'groups': _p(in_vert=['fillet', '3.5 mm', 45.0, False],
                     out_vert=['chamfer', '0.4 mm', 45.0, False],
                     top=['chamfer', '0.4 mm', 45.0, False],
                     bottom=['chamfer', '0.4 mm', 45.0, False],
                     holes=['chamfer', '0.4 mm', 45.0, False]),
        'options': {'bottom_outermost': False, 'top_outermost': False},
        'note': 'Inside corners get a radius larger than a 1/4" end mill (3.175 mm) so they can be cut; '
                'outside edges get a break-edge chamfer.',
    },
    'Machined cosmetic': {
        'groups': _p(in_vert=['fillet', '3.5 mm', 45.0, False],
                     out_vert=['fillet', '3 mm', 45.0, False],
                     top=['chamfer', '0.5 mm', 45.0, False],
                     bottom=['chamfer', '0.5 mm', 45.0, False],
                     holes=['chamfer', '0.5 mm', 45.0, False]),
        'options': {'bottom_outermost': False, 'top_outermost': False},
        'note': 'Rounded outside corners with a crisp chamfer running around top and bottom.',
    },
    'Fully rounded (cosmetic)': {
        'groups': _p(out_vert=['fillet', '3 mm', 45.0, True],
                     top=['fillet', '1.5 mm', 45.0, True],
                     bottom=['fillet', '1.5 mm', 45.0, True],
                     other=['fillet', '1 mm', 45.0, False],
                     holes=['fillet', '0.5 mm', 45.0, False]),
        'options': {'bottom_outermost': False, 'top_outermost': False},
        'note': 'Everything rounded, G2 on the main blends. Good for show parts and renders.',
    },
    'Heavy deburr (plate / weldment)': {
        'groups': _p(out_vert=['chamfer', '1 mm', 45.0, False],
                     top=['chamfer', '1 mm', 45.0, False],
                     bottom=['chamfer', '1 mm', 45.0, False],
                     other=['chamfer', '1 mm', 45.0, False],
                     holes=['chamfer', '0.8 mm', 45.0, False]),
        'options': {'bottom_outermost': False, 'top_outermost': False},
        'note': '1 mm x 45 deg everywhere. Approximates a hand/grinder deburr on plate.',
    },
}

DEFAULT_OPTIONS = {
    'axis': '+Z',
    'min_length': 0.0,          # cm; skip edges shorter than this
    'top_outermost': False,     # only edges on the highest up-facing face(s)
    'bottom_outermost': True,   # only edges on the lowest down-facing face(s)
    'fillet_setback': True,     # setback corners (else rolling ball)
    'chamfer_corner': 'miter',  # miter | chamfer | blend
    'tangent_chain': True,
    'on_fail': 'shrink',        # shrink | skip | stop
    'shrink_steps': [0.75, 0.5],
    'use_params': True,
}


# ------------------------------------------------------------ geometry utils
def vec(t):
    v = adsk.core.Vector3D.create(*t)
    v.normalize()
    return v


def mid(edge):
    ev = edge.evaluator
    _, p0, p1 = ev.getParameterExtents()
    _, p = ev.getPointAtParameter((p0 + p1) / 2)
    return p


def _dot_pt(p, axis):
    return p.x * axis.x + p.y * axis.y + p.z * axis.z


def _is_hole_face(face, edge, p):
    """True if face is a cylinder/cone whose material normal points toward the
    edge's circle centre (i.e. the face is the inside wall of a hole)."""
    g = edge.geometry
    if not isinstance(face.geometry, (adsk.core.Cylinder, adsk.core.Cone)):
        return False
    c = getattr(g, 'center', None)
    if c is None:
        return False
    _, n = face.evaluator.getNormalAtPoint(p)
    return n.dotProduct(p.vectorTo(c)) > 0


def _is_round_rim(g):
    if isinstance(g, adsk.core.Circle3D):
        return True
    if isinstance(g, adsk.core.Arc3D):
        return abs(g.endAngle - g.startAngle) >= 3.1  # ~178 deg
    return False


def _chamfer_face_tokens(body):
    toks = set()
    try:
        for cf in body.parentComponent.features.chamferFeatures:
            if cf.isSuppressed:
                continue
            for f in cf.faces:
                if f.body == body:
                    toks.add(f.entityToken)
    except Exception:
        pass
    return toks


def classify(body, opts, exclude_pts=None):
    """Return {group: [edges]} plus 'skipped' list."""
    axis = vec(AXES[opts['axis']])
    out = {k: [] for k in GROUP_KEYS}
    out['skipped'] = []

    # extreme levels for outermost filters
    up_levels, dn_levels = [], []
    for f in body.faces:
        if isinstance(f.geometry, adsk.core.Plane):
            # evaluator normals follow the material side (geometry normals may not)
            _, n = f.evaluator.getNormalAtPoint(f.pointOnFace)
            d = n.dotProduct(axis)
            lvl = _dot_pt(f.pointOnFace, axis)
            if d > TOL_PAR:
                up_levels.append(lvl)
            elif d < -TOL_PAR:
                dn_levels.append(lvl)
    top_lvl = max(up_levels) if up_levels else None
    bot_lvl = min(dn_levels) if dn_levels else None

    mm = adsk.core.Application.get().measureManager
    min_len = opts.get('min_length', 0.0) or 0.0
    done = _chamfer_face_tokens(body)

    for e in body.edges:
        if e.faces.count != 2 or e.length < min_len:
            out['skipped'].append(e)
            continue
        # edges bounding an existing chamfer are already finished
        if done and (e.faces.item(0).entityToken in done or e.faces.item(1).entityToken in done):
            out['skipped'].append(e)
            continue
        if exclude_pts:
            hit = False
            for ep in exclude_pts:
                try:
                    if mm.measureMinimumDistance(e, ep).value < 0.005:
                        hit = True
                        break
                except Exception:
                    pass
            if hit:
                out['skipped'].append(e)
                continue

        p = mid(e)
        f1, f2 = e.faces.item(0), e.faces.item(1)
        _, n1 = f1.evaluator.getNormalAtPoint(p)
        _, n2 = f2.evaluator.getNormalAtPoint(p)
        if n1.dotProduct(n2) > TOL_PAR:          # tangent / smooth
            out['skipped'].append(e)
            continue
        dv = n2.copy(); dv.subtract(n1)
        if dv.length < 1e-9:
            out['skipped'].append(e)
            continue
        dv.normalize(); dv.scaleBy(EPS)
        probe = p.copy(); probe.translateBy(dv)
        concave = body.pointContainment(probe) == \
            adsk.fusion.PointContainment.PointInsidePointContainment

        g = e.geometry
        # vertical straight edges
        if isinstance(g, adsk.core.Line3D):
            _, sp, epnt = e.evaluator.getEndPoints()
            t = sp.vectorTo(epnt); t.normalize()
            if abs(t.dotProduct(axis)) > TOL_PAR:
                out['in_vert' if concave else 'out_vert'].append(e)
                continue

        # cap faces (normal along +/- axis)
        up = dn = None
        for f, n in ((f1, n1), (f2, n2)):
            if isinstance(f.geometry, adsk.core.Plane):
                d = n.dotProduct(axis)
                if d > TOL_PAR:
                    up = f
                elif d < -TOL_PAR:
                    dn = f

        if concave:
            if up is not None:
                out['floor'].append(e)
            else:
                out['skipped'].append(e)
            continue

        # convex hole rims: full circles, or arcs >= ~180 deg (slot ends) on a
        # hole wall. Smaller arcs are inside-corner fillets, not holes.
        if _is_round_rim(g) and (_is_hole_face(f1, e, p) or _is_hole_face(f2, e, p)):
            out['holes'].append(e)
            continue

        if up is not None:
            if opts.get('top_outermost') and top_lvl is not None and \
                    abs(_dot_pt(p, axis) - top_lvl) > LEVEL_TOL:
                out['skipped'].append(e)
            else:
                out['top'].append(e)
            continue
        if dn is not None:
            if opts.get('bottom_outermost') and bot_lvl is not None and \
                    abs(_dot_pt(p, axis) - bot_lvl) > LEVEL_TOL:
                out['skipped'].append(e)
            else:
                out['bottom'].append(e)
            continue
        out['other'].append(e)
    return out


# ----------------------------------------------------------- feature makers
def _coll(items):
    c = adsk.core.ObjectCollection.create()
    for i in items:
        c.add(i)
    return c


def _healthy(f):
    return f and f.healthState == adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState


def make_fillet(comp, groups, spec, opts):
    """groups: [(edges, size_expr)]"""
    try:
        fi = comp.features.filletFeatures.createInput()
        fi.isRollingBallCorner = not opts.get('fillet_setback', True)
        for edges, expr in groups:
            if not edges:
                continue
            es = fi.edgeSetInputs.addConstantRadiusEdgeSet(
                _coll(edges), adsk.core.ValueInput.createByString(expr), True)
            if spec.get('g2'):
                es.continuity = adsk.fusion.SurfaceContinuityTypes.CurvatureSurfaceContinuityType
        f = comp.features.filletFeatures.add(fi)
        if _healthy(f):
            return f
        if f:
            f.deleteMe()
    except Exception:
        pass
    return None


_CORNER = {
    'miter': 'MiterCornerType',
    'chamfer': 'ChamferCornerType',
    'blend': 'BlendCornertype',
}


def make_chamfer(comp, groups, spec, opts):
    try:
        ci = comp.features.chamferFeatures.createInput2()
        ci.cornerType = getattr(adsk.fusion.ChamferCornerTypes,
                                _CORNER.get(opts.get('chamfer_corner', 'miter')))
        tc = bool(opts.get('tangent_chain', True))
        ang = float(spec.get('angle', 45.0))
        for edges, expr in groups:
            if not edges:
                continue
            if abs(ang - 45.0) < 1e-6:
                ci.chamferEdgeSets.addEqualDistanceChamferEdgeSet(
                    _coll(edges), adsk.core.ValueInput.createByString(expr), tc)
            else:
                ci.chamferEdgeSets.addDistanceAndAngleChamferEdgeSet(
                    _coll(edges), adsk.core.ValueInput.createByString(expr),
                    adsk.core.ValueInput.createByString(f'{ang} deg'), False, tc)
        f = comp.features.chamferFeatures.add(ci)
        if _healthy(f):
            return f
        if f:
            f.deleteMe()
    except Exception:
        pass
    return None


def _works(maker, comp, tiers, spec, opts):
    f = maker(comp, [(l, x) for x, l in tiers if l], spec, opts)
    if f:
        f.deleteMe()
        return True
    return False


def _at(e):
    p = mid(e)
    return f'({p.x*10:.1f}, {p.y*10:.1f}, {p.z*10:.1f}) mm'


def apply_group(comp, edges, size_expr, spec, opts, label, log, chunk=8):
    """Create one feature for the group; on failure fall back per opts['on_fail'].
    Returns (feature or None, stop_flag)."""
    maker = make_fillet if spec['treatment'] == 'fillet' else make_chamfer
    feat = maker(comp, [(edges, size_expr)], spec, opts)
    if feat:
        feat.name = f'AEF {label}'
        log.append(f'{label}: {len(edges)} edges, {spec["treatment"]} {size_expr}')
        return feat, False

    mode = opts.get('on_fail', 'shrink')
    if mode == 'stop':
        log.append(f'{label}: FAILED as a whole ({len(edges)} edges) - stopped (On failure = Stop)')
        return None, True

    steps = opts.get('shrink_steps', [0.75, 0.5]) if mode == 'shrink' else []
    tiers = [(size_expr, [])] + [(f'({size_expr}) * {s}', []) for s in steps]
    dropped = []

    def place(e):
        for i, (x, lst) in enumerate(tiers):
            trial = [(xx, ll + ([e] if j == i else [])) for j, (xx, ll) in enumerate(tiers)]
            if _works(maker, comp, trial, spec, opts):
                lst.append(e)
                return True
        return False

    for k in range(0, len(edges), chunk):
        block = edges[k:k + chunk]
        trial = [(x, l + block) if i == 0 else (x, l) for i, (x, l) in enumerate(tiers)]
        if _works(maker, comp, trial, spec, opts):
            tiers[0][1].extend(block)
            continue
        for e in block:
            if not place(e):
                dropped.append(e)

    notes = [f'   reduced to {x} at {_at(e)}' for x, l in tiers[1:] for e in l]
    notes += [f'   skipped edge at {_at(e)}, L={e.length*10:.1f} mm' for e in dropped]
    feat = maker(comp, [(l, x) for x, l in tiers if l], spec, opts)
    if feat:
        feat.name = f'AEF {label}'
    summary = ', '.join(f'{len(l)} @ {x}' for x, l in tiers if l) or 'none placed'
    log.append(f'{label}: {spec["treatment"]} {summary}; skipped {len(dropped)}'
               + ('' if feat else ' (no feature created)'))
    log.extend(notes)
    return feat, False


# --------------------------------------------------------------- main entry
def _ensure_param(design, name, expr):
    """Reuse aef_<group> if it already holds this value; otherwise make a new
    aef_<group>_N so earlier AEF features don't silently change."""
    ups = design.userParameters
    cand, i = name, 1
    while True:
        up = ups.itemByName(cand)
        if not up:
            ups.add(cand, adsk.core.ValueInput.createByString(expr), 'mm', 'AutoEdgeFinish')
            return cand
        try:
            same = abs(up.value - design.unitsManager.evaluateExpression(expr, 'mm')) < 1e-9
        except Exception:
            same = up.expression.replace(' ', '') == expr.replace(' ', '')
        if same:
            return cand
        i += 1
        cand = f'{name}_{i}'


def run_plan(bodies, plan, opts, exclude_pts=None, log=None):
    """bodies: native BRepBody list. plan: {group: {'treatment','size','angle','g2'}}.
    Applies groups in GROUP order, re-classifying between groups."""
    log = [] if log is None else log
    design = adsk.fusion.Design.cast(adsk.core.Application.get().activeProduct)
    size_exprs = {}
    for key in GROUP_KEYS:
        spec = plan.get(key)
        if not spec or spec['treatment'] == 'none':
            continue
        if opts.get('use_params', True):
            size_exprs[key] = _ensure_param(design, f'aef_{key}', spec['size'])
        else:
            size_exprs[key] = spec['size']

    for body in bodies:
        comp = body.parentComponent
        name = body.name
        log.append(f'== {comp.name} / {name}')
        for key in GROUP_KEYS:
            spec = plan.get(key)
            if not spec or spec['treatment'] == 'none':
                continue
            b = comp.bRepBodies.itemByName(name) or body
            groups = classify(b, opts, exclude_pts)
            edges = groups[key]
            if not edges:
                log.append(f'{GROUP_NAMES[key]}: no edges')
                continue
            _, stop = apply_group(comp, edges, size_exprs[key], spec, opts,
                                  GROUP_NAMES[key], log)
            if stop:
                return log
    return log
