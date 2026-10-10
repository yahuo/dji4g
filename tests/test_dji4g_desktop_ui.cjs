const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../deploy/omarchy/dji4g.sms/Service.qml'), 'utf8');
function service() {
  const writes = [];
  const context = {
    snapshot: { online: true, ready: true, messages: [], unread: 0 },
    busy: false, pendingAction: '', discardSensitiveReply: false,
    detail: null, preview: null, error: '', sendState: '', deviceUp: true,
    worker: { running: true, write: line => writes.push(JSON.parse(line)) },
    probe: { running: false, again: false },
  };
  context.root = context;
  vm.createContext(context);
  const names = ['request', 'showMessage', 'prepare', 'submit', 'clearSensitive', 'receive',
    'syncDevice', 'applyDevice', 'forget'];
  for (const name of names) {
    const body = source.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
    assert.ok(body, name);
    vm.runInContext(body[0], context);
  }
  context.reply = value => context.receive(JSON.stringify({ ...value, response: true }));
  context.writes = writes;
  const present = source.match(/readonly property bool present: (.*)/);
  assert.ok(present);
  context.present = () => vm.runInContext(present[1], context);
  return context;
}

test('prepare only requests preview; no send without a preview', () => {
  const s = service();
  s.submit();
  assert.equal(s.writes.length, 0);
  s.prepare('10086', 'test');
  assert.deepEqual(s.writes, [{ action: 'preview', recipient: '10086', body: 'test' }]);
});

test('confirmed preview is single-use and sends only its key', () => {
  const s = service();
  s.preview = { key: 'one-use' };
  s.submit();
  s.submit();
  assert.equal(s.preview, null);
  assert.equal(s.busy, true);
  assert.deepEqual(s.writes, [{ action: 'send', key: 'one-use', confirmed: true }]);
});

test('closing while preview is in flight discards the late confirmation', () => {
  const s = service();
  s.prepare('10086', 'test');
  s.clearSensitive();
  s.reply({ type: 'preview', key: 'stale', preview: {} });
  assert.equal(s.preview, null);
  assert.equal(s.busy, false);
  s.submit();
  assert.equal(s.writes.length, 1);
});

test('closing while details are in flight does not retain the SMS body', () => {
  const s = service();
  s.showMessage(1);
  s.clearSensitive();
  s.reply({ type: 'detail', message: { id: 1, body: 'private' } });
  assert.equal(s.detail, null);
});

test('background snapshots do not release an in-flight action', () => {
  const s = service();
  s.prepare('10086', 'test');
  s.receive(JSON.stringify({ type: 'snapshot', online: true, ready: true, messages: [] }));
  assert.equal(s.busy, true);
});

test('unknown send outcome is visible and never automatically retried', () => {
  const s = service();
  s.preview = { key: 'one-use' };
  s.submit();
  s.reply({ type: 'sent', state: 'unknown' });
  assert.equal(s.sendState, 'unknown');
  assert.equal(s.busy, false);
  s.submit();
  assert.equal(s.writes.length, 1);
});

test('expired confirmation stops the sending indicator and preserves its error', () => {
  const s = service();
  s.preview = { key: 'expired' };
  s.submit();
  s.reply({ type: 'error', action: 'send', error: 'expired' });
  assert.equal(s.sendState, '');
  assert.equal(s.error, 'expired');
  assert.equal(s.busy, false);
});

test('offline refresh does not erase inbox metadata', () => {
  const s = service();
  s.snapshot.messages = [{ id: 1 }];
  s.receive(JSON.stringify({ type: 'snapshot', online: false, error: 'offline' }));
  assert.equal(s.snapshot.online, false);
  assert.equal(s.snapshot.messages[0].id, 1);
});

test('the engine runs only while systemd reports the module plugged in', () => {
  const s = service();
  // A device unit is active while plugged; "activating" means tentative, which
  // a USB device never needs, and anything else means gone.
  for (const state of ['inactive\n', 'activating\n', 'deactivating\n', 'failed\n', '']) {
    s.deviceUp = true;
    s.worker.running = true;
    s.applyDevice(state);
    assert.equal(s.deviceUp, false, JSON.stringify(state));
    assert.equal(s.worker.running, false, JSON.stringify(state));
  }
  s.deviceUp = false;
  s.worker.running = false;
  s.applyDevice('active\n');
  assert.equal(s.deviceUp, true);
  assert.equal(s.worker.running, true);
});

test('nothing read from the module is kept once it is gone, and nothing is sent to it', () => {
  const s = service();
  s.snapshot = { online: true, ready: true, unread: 2, messages: [{ id: 1, peer: '138****0000' }] };
  s.error = 'stale';
  s.applyDevice('inactive');
  assert.equal(JSON.stringify(s.snapshot), '{"online":false,"messages":[],"unread":0}');
  assert.equal(s.error, '');
  assert.equal(s.present(), false);
  s.prepare('10086', 'test');
  s.preview = { key: 'left-over' };
  s.submit();
  assert.equal(s.writes.length, 0);
  assert.equal(s.sendState, '');
});

test('an unresolved send keeps the widget on screen after the module is gone', () => {
  const s = service();
  s.preview = { key: 'one-use' };
  s.submit();
  s.applyDevice('inactive');
  assert.equal(s.present(), true);
  s.sendState = 'unknown';
  assert.equal(s.present(), true);
  s.sendState = '';
  assert.equal(s.present(), false);
});

test('an engine that lost the module asks systemd again, without overlapping probes', () => {
  for (const lost of [{ online: false, error: 'offline' }, { online: true, ready: false, connected: false, messages: [] }]) {
    const s = service();
    s.receive(JSON.stringify({ type: 'snapshot', online: true, ready: true, connected: true, messages: [] }));
    assert.equal(s.probe.running, false);
    s.receive(JSON.stringify({ type: 'snapshot', ...lost }));
    assert.equal(s.probe.running, true, JSON.stringify(lost));
    assert.equal(s.probe.again, false);
    s.syncDevice();
    assert.equal(s.probe.again, true);
  }
});

// These files are installed separately and only meet by name.
test('the hot-plug pieces name each other consistently', () => {
  const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  const rule = read('deploy/linux/71-dji4g-hotplug.rules');
  const alias = rule.match(/ENV\{SYSTEMD_ALIAS\}\+="\/([a-z0-9/]+)"/);
  assert.ok(alias);
  const device = alias[1].replace(/\//g, '-') + '.device';
  assert.ok(source.includes('"is-active", "' + device + '"'));

  const unit = 'dji4g-sms-panel.service';
  assert.ok(rule.includes('ENV{SYSTEMD_USER_WANTS}+="' + unit + '"'));
  const notify = read('deploy/omarchy/' + unit);
  assert.match(notify, new RegExp('^BindsTo=' + device + '$', 'm'));
  assert.match(notify, new RegExp('^After=' + device + '$', 'm'));
  const target = source.match(/IpcHandler \{\s*target: "([^"]+)"/);
  assert.ok(target);
  for (const key of ['ExecStart', 'ExecStop'])
    assert.ok(notify.split('\n').includes(key + '=-omarchy-shell -q ' + target[1] + ' sync'), key);
});

const panelSource = fs.readFileSync(path.join(__dirname, '../deploy/omarchy/dji4g.sms/Panel.qml'), 'utf8');
function panel() {
  const context = {};
  vm.createContext(context);
  const basic = panelSource.match(/readonly property string gsmBasic: ("[^\n]*")\n\s*\+ ("[^\n]*")/);
  const extension = panelSource.match(/readonly property string gsmExtension: ("[^\n]*")/);
  assert.ok(basic && extension);
  vm.runInContext(`var gsmBasic = ${basic[1]} + ${basic[2]}; var gsmExtension = ${extension[1]};`, context);
  for (const name of ['findCode', 'estimate']) {
    const body = panelSource.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
    assert.ok(body, name);
    vm.runInContext(body[0], context);
  }
  return context;
}

// Expected values come from dji4g_sms.build_submit_pdus, which the gateway sends with.
test('segment counter matches the gateway encoder at every boundary', () => {
  const p = panel();
  const cases = [
    ['', 'GSM-7', 0],
    ['a'.repeat(160), 'GSM-7', 1], ['a'.repeat(161), 'GSM-7', 2],
    ['a'.repeat(306), 'GSM-7', 2], ['a'.repeat(307), 'GSM-7', 3],
    ['€'.repeat(80), 'GSM-7', 1], ['€'.repeat(81), 'GSM-7', 2],
    // A two-septet character cannot straddle a segment boundary.
    ['a'.repeat(152) + '€' + 'a'.repeat(152), 'GSM-7', 3],
    ['中'.repeat(70), 'UCS-2', 1], ['中'.repeat(71), 'UCS-2', 2],
    ['中'.repeat(134), 'UCS-2', 2], ['中'.repeat(135), 'UCS-2', 3],
    ['😀'.repeat(35), 'UCS-2', 1], ['😀'.repeat(36), 'UCS-2', 2],
    // Nor can a surrogate pair.
    ['中'.repeat(66) + '😀' + '中'.repeat(66), 'UCS-2', 3],
    ['a'.repeat(1530), 'GSM-7', 10], ['a'.repeat(1531), 'GSM-7', 11],
  ];
  for (const [text, encoding, segments] of cases) {
    const result = p.estimate(text);
    assert.deepEqual([result.encoding, result.segments], [encoding, segments], JSON.stringify(text.slice(0, 8)) + ' x' + text.length);
  }
});

test('a code is offered only when the text names one, and never a fragment of a longer number', () => {
  const p = panel();
  assert.equal(p.findCode('【示例银行】验证码123456，5分钟内有效'), '123456');
  assert.equal(p.findCode('订单 20261010 已发货，取件码 8842'), '8842');
  assert.equal(p.findCode('Your code is 4821. Order 99887766'), '4821');
  assert.equal(p.findCode('您尾号1234的卡消费100元'), '');
  assert.equal(p.findCode('验证码请致电 13800138000'), '');
  assert.equal(p.findCode('code AB1234'), '');
  assert.equal(p.findCode(''), '');
});

test('the panel cannot be summoned over IPC while the module is unplugged', () => {
  const handler = panelSource.match(/  IpcHandler \{[^]*?\n  }/);
  assert.ok(handler);
  const methods = handler[0].split('\n').filter(line => /function \w+\(/.test(line));
  assert.equal(methods.length, 7);
  for (const line of methods) {
    if (/function (close|hide)\(/.test(line)) continue;
    assert.match(line, /\{ if \(sms\.available\) sms\./, line);
  }
});
