// Generic in-process Qt5 (MSVC x64) controller for RevoMetro.
// The controller prepends: const CMD = {op: 'list'|'click', name, text, cls, visibleOnly};
// Clicks are dispatched with QMetaObject::invokeMethod(obj, "click", Qt::QueuedConnection)
// so they execute on the GUI thread.
function exp(mod, name) {
  const a = Process.getModuleByName(mod).findExportByName(name);
  if (!a) throw new Error('missing export ' + name);
  return a;
}
const W = 'Qt5Widgets.dll', C = 'Qt5Core.dll';
const allWidgets = new NativeFunction(exp(W, '?allWidgets@QApplication@@SA?AV?$QList@PEAVQWidget@@@@XZ'), 'pointer', ['pointer']);
const objectName = new NativeFunction(exp(C, '?objectName@QObject@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const className = new NativeFunction(exp(C, '?className@QMetaObject@@QEBAPEBDXZ'), 'pointer', ['pointer']);
const inherits = new NativeFunction(exp(C, '?inherits@QObject@@QEBA_NPEBD@Z'), 'uint8', ['pointer', 'pointer']);
const windowOf = new NativeFunction(exp(W, '?window@QWidget@@QEBAPEAV1@XZ'), 'pointer', ['pointer']);
const isVisibleTo = new NativeFunction(exp(W, '?isVisibleTo@QWidget@@QEBA_NPEBV1@@Z'), 'uint8', ['pointer', 'pointer']);
const isEnabledTo = new NativeFunction(exp(W, '?isEnabledTo@QWidget@@QEBA_NPEBV1@@Z'), 'uint8', ['pointer', 'pointer']);
const btnText = new NativeFunction(exp(W, '?text@QAbstractButton@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const isChecked = new NativeFunction(exp(W, '?isChecked@QAbstractButton@@QEBA_NXZ'), 'uint8', ['pointer']);
const labelText = new NativeFunction(exp(W, '?text@QLabel@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const spinValue = new NativeFunction(exp(W, '?value@QDoubleSpinBox@@QEBANXZ'), 'double', ['pointer']);
const intSpinValue = new NativeFunction(exp(W, '?value@QSpinBox@@QEBAHXZ'), 'int', ['pointer']);
const lineText = new NativeFunction(exp(W, '?text@QLineEdit@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const comboCurrentText = new NativeFunction(exp(W, '?currentText@QComboBox@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const comboCount = new NativeFunction(exp(W, '?count@QComboBox@@QEBAHXZ'), 'int', ['pointer']);
const comboItemText = new NativeFunction(exp(W, '?itemText@QComboBox@@QEBA?AVQString@@H@Z'), 'pointer', ['pointer', 'pointer', 'int']);
const mapToGlobal = new NativeFunction(exp(W, '?mapToGlobal@QWidget@@QEBA?AVQPoint@@AEBV2@@Z'), 'pointer', ['pointer', 'pointer', 'pointer']);
const frameGeometry = new NativeFunction(exp(W, '?frameGeometry@QWidget@@QEBA?AVQRect@@XZ'), 'pointer', ['pointer', 'pointer']);
const widgetActions = new NativeFunction(exp(W, '?actions@QWidget@@QEBA?AV?$QList@PEAVQAction@@@@XZ'), 'pointer', ['pointer', 'pointer']);
const actionText = new NativeFunction(exp(W, '?text@QAction@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const actionEnabled = new NativeFunction(exp(W, '?isEnabled@QAction@@QEBA_NXZ'), 'uint8', ['pointer']);
const actionChecked = new NativeFunction(exp(W, '?isChecked@QAction@@QEBA_NXZ'), 'uint8', ['pointer']);
const menuTitle = new NativeFunction(exp(W, '?title@QMenu@@QEBA?AVQString@@XZ'), 'pointer', ['pointer', 'pointer']);
const actionMenu = new NativeFunction(exp(W, '?menu@QAction@@QEBAPEAVQMenu@@XZ'), 'pointer', ['pointer']);
const fromUtf8 =new NativeFunction(exp(C, '?fromUtf8@QString@@SA?AV1@PEBDH@Z'), 'pointer', ['pointer', 'pointer', 'int']);
const invokeArgs = ['pointer', 'pointer', 'int'].concat(new Array(10).fill('pointer'));
const invokeMethod = new NativeFunction(exp(C, '?invokeMethod@QMetaObject@@SA_NPEAVQObject@@PEBDW4ConnectionType@Qt@@VQGenericArgument@@333333333@Z'), 'uint8', invokeArgs);

function readQString(qs) {
  const d = qs.readPointer();
  const size = d.add(4).readS32();
  if (size <= 0) return '';
  return d.add(d.add(16).readS64()).readUtf16String(size);
}
function qs(fn, obj) { const b = Memory.alloc(8); fn(obj, b); return readQString(b); }
function metaClass(obj) {
  const mo = new NativeFunction(obj.readPointer().readPointer(), 'pointer', ['pointer'])(obj);
  return className(mo).readCString();
}
const S = {};
function cstr(s) { return S[s] || (S[s] = Memory.allocUtf8String(s)); }

function widgets() {
  const l = Memory.alloc(8);
  allWidgets(l);
  const d = l.readPointer();
  const b = d.add(8).readS32(), e = d.add(12).readS32();
  const res = [];
  for (let i = b; i < e; i++) {
    const w = d.add(16 + 8 * i).readPointer();
    try {
      const win = windowOf(w);
      const vis = !!(isVisibleTo(w, win) && isVisibleTo(win, win));
      const info = { ptr: w, name: qs(objectName, w), cls: metaClass(w), win: metaClass(win), visible: vis, enabled: !!isEnabledTo(w, win), text: '' };
      const p0 = Memory.alloc(8); p0.writeS64(0);
      const gp = Memory.alloc(8); mapToGlobal(w, gp, p0);
      const r = Memory.alloc(16); frameGeometry(w, r);
      info.rect = [gp.readS32(), gp.add(4).readS32(), r.add(8).readS32() - r.readS32() + 1, r.add(12).readS32() - r.add(4).readS32() + 1];
      if (inherits(w, cstr('QAbstractButton'))) { info.text = qs(btnText, w); info.checked = !!isChecked(w); info.button = true; }
      else if (inherits(w, cstr('QLabel'))) info.text = qs(labelText, w);
      else if (inherits(w, cstr('QDoubleSpinBox'))) info.value = spinValue(w);
      else if (inherits(w, cstr('QSpinBox'))) info.value = intSpinValue(w);
      else if (inherits(w, cstr('QLineEdit'))) info.text = qs(lineText, w);
      else if (inherits(w, cstr('QComboBox'))) {
        info.text = qs(comboCurrentText, w);
        info.items = [];
        for (let k = 0; k < comboCount(w); k++) { const b = Memory.alloc(8); comboItemText(w, b, k); info.items.push(readQString(b)); }
      }
      res.push(info);
    } catch (err) { /* widget being destroyed */ }
  }
  return res;
}
function matches(w) {
  if (CMD.visibleOnly !== false && !w.visible) return false;
  if (CMD.name && w.name !== CMD.name) return false;
  if (CMD.text && w.text.trim() !== CMD.text) return false;
  if (CMD.cls && w.cls !== CMD.cls) return false;
  if (CMD.win && w.win !== CMD.win) return false;
  return true;
}
const all = widgets();
const sel = all.filter(matches);
const pub = x => ({ name: x.name, cls: x.cls, win: x.win, visible: x.visible, enabled: x.enabled, text: x.text, checked: x.checked, value: x.value, items: x.items, rect: x.rect });

// Queue `method(arg)` on the GUI thread. arg: {type: 'QString'|'int'|'bool'|'double', value}
function invoke(obj, method, arg) {
  const nul = Memory.alloc(16 * 10);
  const argv = [obj, cstr(method), 2];
  for (let i = 0; i < 10; i++) argv.push(nul.add(16 * i));
  if (arg) {
    let data;
    if (arg.type === 'QString') { data = Memory.alloc(8); const u = Memory.allocUtf8String(String(arg.value)); fromUtf8(data, u, -1); }
    else if (arg.type === 'int') { data = Memory.alloc(4); data.writeS32(arg.value); }
    else if (arg.type === 'bool') { data = Memory.alloc(1); data.writeU8(arg.value ? 1 : 0); }
    else if (arg.type === 'double') { data = Memory.alloc(8); data.writeDouble(arg.value); }
    else throw new Error('unsupported arg type ' + arg.type);
    nul.writePointer(data);  // Qt5 QGenericArgument layout: {const void *_data; const char *_name;}
    nul.add(8).writePointer(cstr(arg.type));
  }
  return !!invokeMethod.apply(null, argv);
}
if (CMD.op === 'list') {
  send({ op: 'list', total: all.length, widgets: sel.filter(x => x.name || x.text).map(pub) });
} else if (CMD.op === 'click') {
  const btn = sel.filter(x => x.button);
  if (btn.length !== 1) {
    send({ op: 'click', ok: false, error: 'expected exactly 1 matching button, got ' + btn.length, candidates: btn.map(pub) });
  } else {
    send({ op: 'click', ok: invoke(btn[0].ptr, 'click'), target: pub(btn[0]) });
  }
} else if (CMD.op === 'actions' || CMD.op === 'trigger') {
  // QActions of every QMenu / QMenuBar / QToolBar (menu entries are actions, not widgets)
  const acts = [], seen = {};
  for (const w of all) {
    if (!(inherits(w.ptr, cstr('QMenu')) || inherits(w.ptr, cstr('QMenuBar')) || inherits(w.ptr, cstr('QToolBar')))) continue;
    const l = Memory.alloc(8); widgetActions(w.ptr, l);
    const d = l.readPointer(), b = d.add(8).readS32(), e = d.add(12).readS32();
    for (let i = b; i < e; i++) {
      const a = d.add(16 + 8 * i).readPointer();
      if (seen[a]) continue;
      seen[a] = 1;
      const sub = actionMenu(a);
      const owner = inherits(w.ptr, cstr('QMenu')) ? (qs(menuTitle, w.ptr).replace(/&/g, '').trim() || w.name || 'QMenu') : (w.name || w.cls);
      acts.push({ ptr: a, owner: owner, text: qs(actionText, a).replace(/&/g, ''),
                  enabled: !!actionEnabled(a), checked: !!actionChecked(a), submenu: sub.isNull() ? '' : qs(objectName, sub) });
    }
  }
  const pa = x => ({ owner: x.owner, text: x.text, enabled: x.enabled, checked: x.checked, submenu: x.submenu });
  if (CMD.op === 'actions') {
    send({ op: 'actions', actions: acts.filter(a => a.text && (!CMD.text || a.text.trim() === CMD.text)).map(pa) });
  } else {
    const m = acts.filter(a => a.text.trim() === CMD.text && (!CMD.owner || a.owner === CMD.owner));
    if (m.length !== 1) send({ op: 'trigger', ok: false, error: 'expected exactly 1 action, got ' + m.length, candidates: m.map(pa) });
    else send({ op: 'trigger', ok: invoke(m[0].ptr, 'trigger'), target: pa(m[0]) });
  }
} else if (CMD.op === 'invoke') {
  if (sel.length !== 1) {
    send({ op: 'invoke', ok: false, error: 'expected exactly 1 matching widget, got ' + sel.length, candidates: sel.map(pub) });
  } else {
    send({ op: 'invoke', ok: invoke(sel[0].ptr, CMD.method, CMD.arg), target: pub(sel[0]) });
  }
}
