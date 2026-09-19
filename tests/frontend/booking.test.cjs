const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/main.js', 'utf8');
function fixture(flags = []) {
    let classes = new Set(flags);
    const cell = {
        classList: {contains: x => classes.has(x), add: x => classes.add(x),
            remove: (...xs) => xs.forEach(x => classes.delete(x)),
            toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
        get className() {return [...classes].join(' ');},
        set className(value) {classes = new Set(value.split(' '));},
        dataset: {court: '1', hour: '9'}, style: {}
    };
    const messages = [], requests = [], timers = [];
    const ctx = {
        window: {__auth: {username: 'me'}, __token: 'fake'},
        State: {fetchToken: 0, dataReady: true, lastPendingJobId: '', lastJobSig: '', stopping: false},
        Elements: {dateInput: {value: '2026-12-18'}, statusText: {}, lastUpdate: {}, confirmDialog: {close() {}}},
        CellMap: {map: new Map([['1-9', cell]])}, performance, Date, console,
        pad2: x => String(x).padStart(2, '0'), showSkeleton() {}, setLoading() {},
        Auth: {loadToken: () => 'fake'},
        fetchLocalBookings: async () => [],
        response: {ok: true, data: {list: [{resources_name: '羽毛球1号场地', slots: [
            {kssj: '09:00', canAppointmentNumber: 1, bookedByMe: false}
        ]}]}},
        Toast: Object.fromEntries(['info', 'success', 'warning', 'error'].map(k => [k, (...a) => messages.push([k, ...a])])),
        clearCells() {cell.className = 'hour-cell'; cell.style.pointerEvents = '';},
        setInterval(fn) {timers.push(fn); return timers.length;}, clearInterval() {},
        fetch: async (url, options) => {requests.push({url, body: options && JSON.parse(options.body)}); return {json: async () => ctx.response};}
    };
    vm.createContext(ctx);
    for (const [start, end] of [
        ['window.fetchAndRenderBookings =', '// ============ 静默续期'],
        ['function armFastPolling()', '// ============ 登录弹窗'],
        ['async function handleDialogConfirm()', '// ============ 事件绑定']
    ]) vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))), ctx);
    ctx.fetchAndRenderBookings = ctx.window.fetchAndRenderBookings;
    return {ctx, cell, messages, requests, timers};
}
test('failed booking refresh removes pending and allows retry', async () => {
    const {ctx, cell} = fixture(['hour-cell', 'pending']);
    await ctx.fetchAndRenderBookings(true);
    assert.equal(cell.classList.contains('pending'), false);
    assert.equal(ctx.State.dataReady, true);
});
test('query failure preserves reserved cell and disables actions', async () => {
    const {ctx, cell} = fixture(['hour-cell', 'reserved']);
    ctx.response = {ok: false, error: 'upstream_query_failed'};
    await ctx.fetchAndRenderBookings(true);
    assert.equal(cell.classList.contains('reserved'), true);
    assert.equal(ctx.State.dataReady, false);
});
test('refresh requests fresh server data', async () => {
    const {ctx, requests} = fixture();
    await ctx.fetchAndRenderBookings(true);
    assert.equal(requests[0].body.force_refresh, true);
});
for (const action of ['book', 'cancel']) test(`09:00 ${action} sends valid time`, async () => {
    const {ctx, cell, requests} = fixture(['selected']);
    Object.assign(ctx.State, {pendingCell: cell, dialogAction: action, isScheduleMode: false});
    ctx.response = {ok: true, data: {job_id: 'new', upstream_status: 'failed'}};
    ctx.fetchAndRenderBookings = async () => {};
    await ctx.handleDialogConfirm();
    assert.equal(requests[0].body.kssj, '09:00');
});
test('cancel failure preserves own reservation even if refresh fails', async () => {
    const {ctx, cell, messages} = fixture(['selected']);
    Object.assign(ctx.State, {pendingCell: cell, dialogAction: 'cancel'});
    ctx.response = {ok: true, data: {upstream_status: 'failed', message: '暂不能取消'}};
    ctx.fetchAndRenderBookings = async () => {};
    await ctx.handleDialogConfirm();
    assert.equal(cell.classList.contains('selected'), true);
    assert.equal(messages[0][1], '取消未完成');
});
test('pending cancellation keeps polling without pending CSS class', async () => {
    const {ctx, cell, timers} = fixture(['selected']);
    Object.assign(ctx.State, {pendingCell: cell, dialogAction: 'cancel'});
    ctx.response = {ok: true, data: {upstream_status: 'pending'}};
    await ctx.handleDialogConfirm();
    ctx.response = {ok: true, data: {db_jobs: [{job_id: 'x', bookdate: '2026-12-18', status: 'running'}]}};
    ctx.fetchAndRenderBookings = async () => {};
    await timers[0]();
    assert.equal(ctx.State.stopping, '2026-12-18');
    assert.ok(ctx.State.fastPollTimer);
    ctx.response.data.db_jobs[0].status = 'done';
    await timers[0]();
    assert.equal(ctx.State.stopping, false);
    assert.equal(ctx.State.fastPollTimer, null);
});
test('scheduled task displays waiting instead of running', () => {
    const jobs = fs.readFileSync('static/js/jobs.js', 'utf8');
    const ctx = {tbody: {}, Date}; vm.createContext(ctx);
    vm.runInContext(jobs.slice(jobs.indexOf('function renderJobs('), jobs.indexOf('// ============ 显示参数')), ctx);
    ctx.renderJobs([{job_id: 'test123456', alive: true, type: 'scheduled', status: 'scheduled', created_at: 1, params: {}}]);
    assert.match(ctx.tbody.innerHTML, /待执行/);
    assert.doesNotMatch(ctx.tbody.innerHTML, /执行中/);
});
