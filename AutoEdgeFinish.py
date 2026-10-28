"""
AutoEdgeFinish - Fusion add-in.

Adds "Auto Edge Finish" to SOLID > MODIFY. The dialog lets you:
  * pick one or more bodies
  * start from a preset (FDM print-ready, machined break-edge, ...) or your own saved presets
  * set fillet / chamfer / none + size per edge group:
        inside vertical corners, outside vertical corners, inside floor edges,
        other convex edges, top edges, bottom edges, hole rims
  * chamfer angle and G2 (curvature) fillets per group
  * choose the up/build direction, outermost-face-only for top and bottom
  * exclude specific edges, skip short edges
  * choose corner types, tangent chaining, and what happens when a size won't fit
  * drive every size from user parameters (aef_<group>) so you can edit later
It applies the groups in a fixed, sensible order, each as its own timeline feature,
re-reading the body between groups so later treatments flow around earlier ones.
"""
import adsk.core, adsk.fusion, traceback, os, sys, json, importlib

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
import aef_engine as eng
importlib.reload(eng)

CMD_ID = 'sparkAutoEdgeFinish'
CMD_NAME = 'Auto Edge Finish'
PANEL_ID = 'SolidModifyPanel'
PRESET_FILE = os.path.join(_here, 'presets.json')
CUSTOM = '(custom)'

_app = adsk.core.Application.get()
_ui = _app.userInterface
_handlers = []
_state = {'loading': False}


# ----------------------------------------------------------------- presets
def load_presets():
    p = dict(eng.BUILTIN_PRESETS)
    try:
        if os.path.exists(PRESET_FILE):
            with open(PRESET_FILE, 'r') as f:
                for k, v in json.load(f).items():
                    p[k] = v
    except Exception:
        pass
    return p


def save_user_preset(name, preset):
    data = {}
    try:
        if os.path.exists(PRESET_FILE):
            with open(PRESET_FILE, 'r') as f:
                data = json.load(f)
    except Exception:
        data = {}
    data[name] = preset
    with open(PRESET_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# --------------------------------------------------------------- UI build
TREAT = ['None', 'Fillet', 'Chamfer']


def _dd(inputs, iid, label, items, selected):
    d = inputs.addDropDownCommandInput(iid, label, adsk.core.DropDownStyles.TextListDropDownStyle)
    for it in items:
        d.listItems.add(it, it == selected, '')
    return d


def _set_dd(dd, value):
    for i in range(dd.listItems.count):
        li = dd.listItems.item(i)
        li.isSelected = (li.name == value)


def _vi(inputs, iid, label, expr, units):
    return inputs.addValueInput(iid, label, units, adsk.core.ValueInput.createByString(expr))


def build_ui(cmd):
    inputs = cmd.commandInputs
    presets = load_presets()
    first = 'FDM print-ready'

    # ---------------- Setup tab
    t1 = inputs.addTabCommandInput('tab_setup', 'Setup').children
    sel = t1.addSelectionInput('bodies', 'Bodies', 'Select the solid bodies to finish')
    sel.addSelectionFilter('SolidBodies')
    sel.setSelectionLimits(1, 0)
    _dd(t1, 'preset', 'Preset', [CUSTOM] + list(presets.keys()), first)
    tb = t1.addTextBoxCommandInput('preset_note', '', '', 3, True)
    tb.isFullWidth = True
    _dd(t1, 'axis', 'Up / build direction', list(eng.AXES.keys()), '+Z')
    counts = t1.addTextBoxCommandInput('counts', 'Edges found', 'Select a body', 9, True)
    counts.isFullWidth = True
    t1.addBoolValueInput('preview', 'Live preview', True, '', True)
    res = t1.addTextBoxCommandInput('result', 'Result', '', 6, True)
    res.isFullWidth = True

    # ---------------- Edge groups tab
    t2 = inputs.addTabCommandInput('tab_groups', 'Edge groups').children
    for key, name in eng.GROUPS:
        g = t2.addGroupCommandInput(f'grp_{key}', name)
        g.isExpanded = key in ('out_vert', 'top', 'bottom')
        c = g.children
        _dd(c, f'{key}_treat', 'Treatment', TREAT, 'None')
        _vi(c, f'{key}_size', 'Size', '0.5 mm', 'mm')
        _vi(c, f'{key}_angle', 'Chamfer angle', '45 deg', 'deg')
        c.addBoolValueInput(f'{key}_g2', 'G2 (curvature) fillet', True, '', False)
        if key == 'top':
            c.addBoolValueInput('top_outermost', 'Highest face only', True, '', False)
        if key == 'bottom':
            c.addBoolValueInput('bottom_outermost', 'Lowest (bed) face only', True, '', True)

    # ---------------- Options tab
    t3 = inputs.addTabCommandInput('tab_opts', 'Options').children
    ex = t3.addSelectionInput('exclude', 'Exclude edges', 'Edges to leave sharp')
    ex.addSelectionFilter('Edges')
    ex.setSelectionLimits(0, 0)
    _vi(t3, 'min_length', 'Skip edges shorter than', '0 mm', 'mm')
    _dd(t3, 'fillet_corner', 'Fillet corners', ['Setback', 'Rolling ball'], 'Setback')
    _dd(t3, 'chamfer_corner', 'Chamfer corners', ['Miter', 'Chamfer', 'Blend'], 'Miter')
    t3.addBoolValueInput('tangent_chain', 'Tangent chain chamfers', True, '', True)
    _dd(t3, 'on_fail', 'If a size will not fit',
        ['Shrink those edges, then skip', 'Skip those edges', 'Stop'],
        'Shrink those edges, then skip')
    t3.addStringValueInput('shrink_steps', 'Shrink steps', '0.75, 0.5')
    t3.addBoolValueInput('use_params', 'Drive sizes from user parameters', True, '', True)
    sg = t3.addGroupCommandInput('grp_save', 'Save current settings as preset').children
    sg.addStringValueInput('save_name', 'Name', '')
    sg.addBoolValueInput('save_btn', 'Save preset', False, '', False)

    apply_preset(inputs, first, presets)
    refresh_visibility(inputs)


# --------------------------------------------------------- UI <-> settings
def apply_preset(inputs, name, presets=None):
    presets = presets or load_presets()
    p = presets.get(name)
    if not p:
        return
    _state['loading'] = True
    try:
        for key in eng.GROUP_KEYS:
            treat, size, ang, g2 = p['groups'].get(key, ['none', '0.5 mm', 45.0, False])
            _set_dd(inputs.itemById(f'{key}_treat'), treat.capitalize())
            inputs.itemById(f'{key}_size').expression = size
            inputs.itemById(f'{key}_angle').expression = f'{ang} deg'
            inputs.itemById(f'{key}_g2').value = bool(g2)
        o = p.get('options', {})
        for k in ('top_outermost', 'bottom_outermost'):
            if k in o:
                inputs.itemById(k).value = bool(o[k])
        inputs.itemById('preset_note').text = p.get('note', '')
    finally:
        _state['loading'] = False


def refresh_visibility(inputs):
    for key in eng.GROUP_KEYS:
        t = inputs.itemById(f'{key}_treat').selectedItem.name
        inputs.itemById(f'{key}_size').isVisible = t != 'None'
        inputs.itemById(f'{key}_angle').isVisible = t == 'Chamfer'
        inputs.itemById(f'{key}_g2').isVisible = t == 'Fillet'


def read_settings(inputs):
    plan = {}
    for key in eng.GROUP_KEYS:
        t = inputs.itemById(f'{key}_treat').selectedItem.name.lower()
        ang_in = inputs.itemById(f'{key}_angle')
        plan[key] = {
            'treatment': t,
            'size': inputs.itemById(f'{key}_size').expression,
            'angle': ang_in.value * 180.0 / 3.141592653589793,
            'g2': inputs.itemById(f'{key}_g2').value,
        }
    steps = []
    for s in inputs.itemById('shrink_steps').value.split(','):
        try:
            v = float(s)
            if 0 < v < 1:
                steps.append(v)
        except ValueError:
            pass
    fail = inputs.itemById('on_fail').selectedItem.name
    opts = dict(eng.DEFAULT_OPTIONS)
    opts.update({
        'axis': inputs.itemById('axis').selectedItem.name,
        'min_length': inputs.itemById('min_length').value,
        'top_outermost': inputs.itemById('top_outermost').value,
        'bottom_outermost': inputs.itemById('bottom_outermost').value,
        'fillet_setback': inputs.itemById('fillet_corner').selectedItem.name == 'Setback',
        'chamfer_corner': inputs.itemById('chamfer_corner').selectedItem.name.lower(),
        'tangent_chain': inputs.itemById('tangent_chain').value,
        'on_fail': 'shrink' if fail.startswith('Shrink') else ('skip' if fail.startswith('Skip') else 'stop'),
        'shrink_steps': steps or [0.75, 0.5],
        'use_params': inputs.itemById('use_params').value,
    })
    return plan, opts


def settings_to_preset(plan, opts, note=''):
    return {
        'groups': {k: [v['treatment'], v['size'], round(v['angle'], 3), v['g2']] for k, v in plan.items()},
        'options': {'top_outermost': opts['top_outermost'], 'bottom_outermost': opts['bottom_outermost']},
        'note': note or 'Saved from Auto Edge Finish.',
    }


def matches_preset(inputs, name):
    """True if the dialog still equals preset `name` (so edits flip it to custom)."""
    p = load_presets().get(name)
    if not p:
        return False
    um = adsk.fusion.Design.cast(_app.activeProduct).unitsManager
    plan, opts = read_settings(inputs)
    for k in eng.GROUP_KEYS:
        treat, size, ang, g2 = p['groups'].get(k, ['none', '0.5 mm', 45.0, False])
        cur = plan[k]
        if cur['treatment'] != treat:
            return False
        if treat == 'none':
            continue
        try:
            if abs(um.evaluateExpression(cur['size'], 'mm') - um.evaluateExpression(size, 'mm')) > 1e-9:
                return False
        except Exception:
            return False
        if treat == 'chamfer' and abs(cur['angle'] - float(ang)) > 1e-6:
            return False
        if treat == 'fillet' and bool(cur['g2']) != bool(g2):
            return False
    o = p.get('options', {})
    for k in ('top_outermost', 'bottom_outermost'):
        if k in o and bool(o[k]) != bool(opts[k]):
            return False
    return True


def selected_bodies(inputs):
    sel = inputs.itemById('bodies')
    out, seen = [], set()
    for i in range(sel.selectionCount):
        b = sel.selection(i).entity
        n = b.nativeObject or b
        if n.entityToken not in seen:
            seen.add(n.entityToken)
            out.append(n)
    return out


def exclude_points(inputs):
    sel = inputs.itemById('exclude')
    pts = []
    for i in range(sel.selectionCount):
        e = sel.selection(i).entity
        e = e.nativeObject or e
        pts.append(eng.mid(e))
    return pts


def update_counts(inputs):
    bodies = selected_bodies(inputs)
    box = inputs.itemById('counts')
    if not bodies:
        box.text = 'Select a body'
        return
    plan, opts = read_settings(inputs)
    pts = exclude_points(inputs)
    tot = {k: 0 for k in eng.GROUP_KEYS}
    skipped = 0
    for b in bodies:
        c = eng.classify(b, opts, pts)
        for k in eng.GROUP_KEYS:
            tot[k] += len(c[k])
        skipped += len(c['skipped'])
    rows = []
    for k, name in eng.GROUPS:
        t = plan[k]['treatment']
        act = 'none' if t == 'none' else f"{t} {plan[k]['size']}"
        rows.append(f'{name}: <b>{tot[k]}</b> &nbsp;&rarr; {act}')
    rows.append(f'<i>Not treated (inside, smooth, excluded, short): {skipped}</i>')
    box.formattedText = '<br>'.join(rows)


# ----------------------------------------------------------------- handlers
class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            cmd.setDialogInitialSize(420, 640)
            build_ui(cmd)
            for ev, h in ((cmd.inputChanged, ChangedHandler()),
                          (cmd.executePreview, PreviewHandler()),
                          (cmd.execute, ExecuteHandler()),
                          (cmd.validateInputs, ValidateHandler()),
                          (cmd.destroy, DestroyHandler())):
                ev.add(h)
                _handlers.append(h)
        except Exception:
            _ui.messageBox('AutoEdgeFinish create failed:\n' + traceback.format_exc())


class ChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            # args.inputs is only the tab/group holding the changed input;
            # use the command's full input set so every itemById resolves
            inputs = args.firingEvent.sender.commandInputs
            cid = args.input.id
            if cid == 'preset':
                name = args.input.selectedItem.name
                if name != CUSTOM:
                    apply_preset(inputs, name)
                refresh_visibility(inputs)
            elif cid == 'save_btn':
                name = inputs.itemById('save_name').value.strip()
                if not name:
                    _ui.messageBox('Type a preset name first.')
                    return
                plan, opts = read_settings(inputs)
                save_user_preset(name, settings_to_preset(plan, opts))
                dd = inputs.itemById('preset')
                exists = any(dd.listItems.item(i).name == name for i in range(dd.listItems.count))
                _state['loading'] = True
                if not exists:
                    dd.listItems.add(name, False, '')
                _set_dd(dd, name)
                _state['loading'] = False
                inputs.itemById('preset_note').text = f'Saved "{name}" to presets.json'
                return
            elif not _state['loading'] and (cid.endswith(('_treat', '_size', '_angle', '_g2'))
                                            or cid in ('top_outermost', 'bottom_outermost')):
                dd = inputs.itemById('preset')
                cur = dd.selectedItem.name if dd.selectedItem else CUSTOM
                if cur != CUSTOM and not matches_preset(inputs, cur):
                    _state['loading'] = True
                    _set_dd(dd, CUSTOM)
                    _state['loading'] = False
            if cid.endswith('_treat'):
                refresh_visibility(inputs)
            update_counts(inputs)
        except Exception:
            _ui.messageBox('AutoEdgeFinish input error:\n' + traceback.format_exc())


def _run(inputs):
    bodies = selected_bodies(inputs)
    plan, opts = read_settings(inputs)
    pts = exclude_points(inputs)
    log = eng.run_plan(bodies, plan, opts, pts)
    return log


class PreviewHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            if not inputs.itemById('preview').value:
                inputs.itemById('result').text = 'Preview off - click OK to apply.'
                return
            log = _run(inputs)
            inputs.itemById('result').text = '\n'.join(log)
            args.isValidResult = True   # OK keeps exactly what you previewed
        except Exception:
            inputs.itemById('result').text = 'Preview error:\n' + traceback.format_exc()


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            log = _run(args.command.commandInputs)
            _ui.messageBox('\n'.join(log), CMD_NAME)
        except Exception:
            _ui.messageBox('AutoEdgeFinish failed:\n' + traceback.format_exc())


class ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        args.areInputsValid = args.inputs.itemById('bodies').selectionCount > 0


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        pass


# ------------------------------------------------------------ add-in hooks
def run(context):
    try:
        # clear any stale button/definition left by an earlier load
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            old = panel.controls.itemById(CMD_ID)
            if old:
                old.deleteMe()
        cd = _ui.commandDefinitions.itemById(CMD_ID)
        if cd:
            cd.deleteMe()
        cd = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME,
            'Fillet/chamfer a whole body by edge group, with presets and fallback.', '')
        h = CreatedHandler()
        cd.commandCreated.add(h)
        _handlers.append(h)
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            ctl = panel.controls.addCommand(cd)
            ctl.isPromotedByDefault = True
            ctl.isPromoted = True
    except Exception:
        _ui.messageBox('AutoEdgeFinish start failed:\n' + traceback.format_exc())


def stop(context):
    try:
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            c = panel.controls.itemById(CMD_ID)
            if c:
                c.deleteMe()
        cd = _ui.commandDefinitions.itemById(CMD_ID)
        if cd:
            cd.deleteMe()
    except Exception:
        pass
