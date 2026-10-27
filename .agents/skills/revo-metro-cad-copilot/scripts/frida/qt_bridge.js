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
      const info = { ptr: w, name: qs(objectName, w), cls: metaClass(w), visible: vis, enabled: !!isEnabledTo(w, win), text: '' };
      if (inherits(w, cstr('QAbstractButton'))) { info.text = qs(btnText, w); info.checked = !!isChecked(w); info.button = true; }
      else if (inherits(w, cstr('QLabel'))) info.text = qs(labelText, w);
      else if (inherits(w, cstr('QDoubleSpinBox'))) info.value = spinValue(w);
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
  return true;
}
const all = widgets();
const sel = all.filter(matches);
const pub = x => ({ name: x.name, cls: x.cls, visible: x.visible, enabled: x.enabled, text: x.text, checked: x.checked, value: x.value });
if (CMD.op === 'list') {
  send({ op: 'list', total: all.length, widgets: sel.filter(x => x.name || x.text).map(pub) });
} else if (CMD.op === 'click') {
  const btn = sel.filter(x => x.button);
  if (btn.length !== 1) {
    send({ op: 'click', ok: false, error: 'expected exactly 1 matching button, got ' + btn.length, candidates: btn.map(pub) });
  } else {
    const nul = Memory.alloc(16 * 10);
    const argv = [btn[0].ptr, cstr('click'), 2];
    for (let i = 0; i < 10; i++) argv.push(nul.add(16 * i));
    const ok = invokeMethod.apply(null, argv);
    send({ op: 'click', ok: !!ok, target: pub(btn[0]) });
  }
}
