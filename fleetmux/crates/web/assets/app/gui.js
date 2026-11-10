// fleetmux GUI terminal page (design.md §3.6.4–§3.6.7, PROTOCOL.md §15.4).
//
// A full-window xterm.js showing the server's composed screen, and one sandboxed iframe laid over the lower-left
// cell for the history page. The window process talks to this page by evaluate_script (`fm.*` below) and this page
// answers by ipc (window.ipc.postMessage), every message carrying the per-launch token. The token arrives in
// window.__fmBoot (an initialization script for the main frame only, run before this script), is read and deleted
// at once, and from then on lives only in this closure.
(() => {
  'use strict';
  const boot = window.__fmBoot || {};
  delete window.__fmBoot;
  const TOKEN = String(boot.token || '');
  const PREFIX_BYTE = typeof boot.prefixByte === 'number' ? boot.prefixByte : 2;
  const IS_MAC = !!boot.mac;
  // The server's palette (`hello.theme` → `fleetmux-web --theme`, PROTOCOL.md §15.4); absent = xterm.js colours.
  const THEME = boot.theme && typeof boot.theme === 'object' ? boot.theme : null;
  const ipc = window.ipc;
  const send = (type, extra) => ipc.postMessage(JSON.stringify(Object.assign({ token: TOKEN, type }, extra)));

  // ---- base64 (PROTOCOL.md §4.7 alphabet) ----
  const toB64 = bytes => {
    if (typeof bytes.toBase64 === 'function') return bytes.toBase64();
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const fromB64 = s => {
    if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(s);
    const b = atob(s), u = new Uint8Array(b.length);
    for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
    return u;
  };
  const utf8 = new TextEncoder();
  const sendInput = bytes => send('input', { data: toB64(bytes) });

  // ---- terminal ----
  const DEFAULT_FONT = 14;
  // A26 (user report A42-TOPBAR-ALIGN-01): the native window controls are the anchor and are never moved. macOS
  // keeps the system default of a full-size-content window (measured on macOS 26.6.2 with this style mask: 14 px
  // buttons at x = 9 / 32 / 55, top = 9 px, centre 16 px below the window top). The page pads row 0 down so the
  // server's toolbar and the fleet title/status share that centre (`placeTopbar`); the toolbar starts one cell past
  // 69 px of controls + a 12 px gap (81 px). Linux draws one native GTK headerbar as its titlebar instead (A26
  // rev5: its own close/minimize/maximize plus `◫ + ↺ ← →` and the fleet title/status in that row, gui.rs), and
  // its grid keeps no topbar row: no pad and no inset here.
  const CONTROL = 14, CONTROL_GAP = 9, CONTROL_X = 9;
  const CONTROLS_RIGHT = CONTROL_X + 3 * CONTROL + 2 * CONTROL_GAP; // 69 px
  const INSET_PX = CONTROLS_RIGHT + 12; // 81 px: the first toolbar cell starts here
  // Centre of the window controls, logical px below the window top (Rust `CONTROL_CENTER`, measured default 16).
  const CONTROL_CENTER = typeof boot.controlCenter === 'number' ? boot.controlCenter : 16;
  let topPad = 0; // px the grid is pushed down so row 0's centre is the native controls' centre (macOS)
  // The 19-value palette as xterm.js names them (PROTOCOL.md §15.4); the same numbers the server answers to
  // OSC 10 / 11 / 12 / 4, so a program's idea of its background and the grid agree.
  const xtermTheme = t => ({
    background: t.bg, foreground: t.fg, cursor: t.cursor, cursorAccent: t.bg,
    black: t.ansi[0], red: t.ansi[1], green: t.ansi[2], yellow: t.ansi[3],
    blue: t.ansi[4], magenta: t.ansi[5], cyan: t.ansi[6], white: t.ansi[7],
    brightBlack: t.ansi[8], brightRed: t.ansi[9], brightGreen: t.ansi[10], brightYellow: t.ansi[11],
    brightBlue: t.ansi[12], brightMagenta: t.ansi[13], brightCyan: t.ansi[14], brightWhite: t.ansi[15],
  });
  const term = new Terminal({
    allowProposedApi: true, // term.unicode (addon-unicode11) is a proposed API in 6.0
    fontSize: DEFAULT_FONT,
    fontFamily: 'ui-monospace, Menlo, "DejaVu Sans Mono", "Noto Sans Mono", monospace',
    cursorBlink: false,
    scrollback: 0, // the server owns history; the screen is all there is
    macOptionClickForcesSelection: true,
    theme: THEME ? xtermTheme(THEME) : undefined,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new Unicode11Addon.Unicode11Addon());
  term.unicode.activeVersion = '11';
  // The margins beside / below the grid: gui.css sizes html and body, so both carry the colour (the script runs at
  // the end of body, which therefore exists). The css value is only the fallback before this line.
  if (THEME) {
    document.documentElement.style.background = THEME.bg;
    document.body.style.background = THEME.bg;
  }
  term.open(document.getElementById('term'));
  let webgl = null;
  try {
    const a = new WebglAddon.WebglAddon();
    a.onContextLoss(() => { try { a.dispose(); } catch (e) { /* already gone */ } webgl = null; }); // back to DOM
    term.loadAddon(a);
    webgl = a;
  } catch (e) {
    webgl = null; // no WebGL (xvfb, some VMs): xterm.js's own DOM renderer
  }
  // The same mode string `fleetmux attach` writes to the outer terminal (attach/input.rs ENTER + MOUSE_ON).
  term.write('\x1b[?1049h\x1b[?2004h\x1b[?1002h\x1b[?1006h');
  // A42 rev12 diagnostic (`--test-hooks` only, default OFF): a passive tap on this very `term.onData` callback, so a
  // measurement sees the exact bytes the page hands to the input path instead of a proxy further down (the ipc
  // frame). It exists only in a window launched with `--test-hooks` (`fleetmux gui` never passes it), records
  // nothing until the isolated window's own `js` request enables it against an explicitly declared target, keeps a
  // bounded in-page buffer, and never changes what is sent. Contract: `workspace/impl-1/keyboard-hook.md`.
  const onDataDiag = boot.testHooks ? (() => {
    const SCHEMA = 'fm-ondata-diag/1', MAX = 500;
    let enabled = false, target = null, seq = 0, dropped = 0, t0 = 0, records = [];
    const hex = u => {
      let s = '';
      for (let i = 0; i < u.length; i++) s += (u[i] < 16 ? '0' : '') + u[i].toString(16);
      return s;
    };
    const api = {
      schema: SCHEMA,
      // Called by the tap with the very bytes the input path is about to send. Never throws into that path.
      record(data, bytes) {
        if (!enabled) return;
        try {
          const u = bytes instanceof Uint8Array ? bytes : utf8.encode(data);
          const rec = { seq: ++seq, t: Math.round(performance.now() - t0), hex: hex(u), bytes: u.length, source: 'term.onData' };
          if (records.length >= MAX) { records.shift(); dropped++; }
          records.push(rec);
        } catch (e) { /* a diagnostic must never disturb input */ }
      },
      enable() {
        const want = window.__FM_ON_DATA_DIAG_TARGET__;
        if (typeof want !== 'string' || !want || want.length > 64) {
          throw new Error('fm-ondata-diag: refusing to record: no target window selected');
        }
        target = want; enabled = true; seq = 0; dropped = 0; t0 = performance.now(); records = [];
        return api.state();
      },
      disable() { enabled = false; return api.state(); },
      state() {
        return { schema: SCHEMA, target: target, enabled: enabled, records: records.length, dropped: dropped, maxRecords: MAX };
      },
      dump() {
        return { schema: SCHEMA, target: target, enabled: enabled, dropped: dropped,
                 records: records.map(r => ({ seq: r.seq, t: r.t, hex: r.hex, bytes: r.bytes, source: r.source })) };
      },
    };
    Object.freeze(api);
    window.__fmOnDataDiag = api;
    return api;
  })() : null;
  term.onData(d => {
    const bytes = utf8.encode(d); // the exact bytes the input path sends, unchanged
    if (onDataDiag) onDataDiag.record(d, bytes);
    sendInput(bytes);
  });
  term.onBinary(d => {
    const u = new Uint8Array(d.length);
    for (let i = 0; i < d.length; i++) u[i] = d.charCodeAt(i) & 0xff;
    sendInput(u);
  });
  const screenEl = () => document.querySelector('.xterm-screen');

  // ---- size: first fit at once, then debounced (50 ms) on window / font changes ----
  let lastSize = '';
  const reportSize = () => {
    const s = term.cols + 'x' + term.rows;
    if (s === lastSize) return;
    lastSize = s;
    send('resize', { cols: term.cols, rows: term.rows });
  };
  // A26 (plan §17.2 / §17.3): the columns row 0 leaves for the window controls, from the same cell width the
  // history overlay is placed with. The server draws the toolbar at `cols + 1`, so `cols` is one less than the
  // first cell past INSET_PX. Sent before the first size (the client puts it in the view handshake) and again after
  // a font-size change, a window resize or a display-scale change, as the `0x06` frame.
  let lastInset = -1;
  let lastCell = -1;
  const reportInset = () => {
    const c = cellSize();
    if (!(c.w > 0) || !(c.h > 0)) return;
    // macOS: the columns row 0 leaves for the window controls. Linux keeps no row 0 (A26 rev5), so `cols` is 0 —
    // but `cell` still goes out: the window's GTK headerbar puts its workspace/sidebar boundary on the grid's
    // divider column, and the page fits whole cells, so its canvas (`cols · cell`) can be a few px narrower than
    // the window (A42-LINUX-SIDEBAR-ALIGN-03).
    const cols = IS_MAC ? Math.max(0, Math.min(40, Math.round(INSET_PX / c.w) - 1)) : 0;
    const cell = Math.round(c.w * 100) / 100;
    if (cols === lastInset && cell === lastCell) return;
    lastInset = cols;
    lastCell = cell;
    send('inset', { cols, cell });
  };
  // ---- caret: the focused terminal cell, for the GTK input method (Linux only) ----
  // A42-LINUX-IME-CANDIDATE-05: WebKit tells GTK the caret area on focus and selection changes only, so a terminal
  // whose caret cell moved (cursor move, a fed screen, font change, resize, zoom, pane switch) keeps the old area and
  // ibus places its candidate window there. On Linux the window re-pushes this rect to WebKit's own input method
  // context (gui.rs `push_caret_area`); macOS keeps its native behaviour and sends nothing.
  //
  // The numbers are the page's CSS px, the caret cell of the **grid** (`screen rect / cols, rows` at the live
  // cursor), which is what xterm puts its helper textarea on. The textarea's own rect is only a cross-check: after a
  // font change or a resize xterm can leave it on the old cell until the next cursor move, and a stale rect must not
  // become the IM's caret. The window adds its webview allocation origin and nothing else — measured at GDK_SCALE=1
  // and 2, WebKit's own path delivers `css + allocation origin` at both scales, and the public notify path passes
  // its numbers through unchanged, so no devicePixelRatio factor exists (workspace/impl-1/probe-gdk2).
  let inPage = false; // DOM focus is inside the history iframe (set by the iframe's `history_focus`)
  let lastCaret = 'init'; // 'init' before the first report; then 'null' or 'x,y,w,h' in rounded CSS px
  const caretCell = () => {
    // Real state, not a rect: a terminal textarea always has one, so only the focus/pane/caret state decides.
    const ta = term.textarea;
    if (IS_MAC || !ta) return null;
    if (!document.hasFocus() || document.visibilityState !== 'visible') return null;
    if (document.activeElement !== ta || inPage) return null; // the iframe or any other input holds focus
    const b = term.buffer.active;
    if (!(term.rows > 0 && term.cols > 0)) return null;
    // The buffer's cursor must be a cell of the live grid. A hidden cursor (DECTCEM off) is *not* this case: it keeps
    // its coordinates, so hiding it never suppresses the caret; only coordinates outside 0..rows-1 / 0..cols-1 do.
    if (!(b.cursorY >= 0 && b.cursorY < term.rows)) return null;
    if (!(b.cursorX >= 0 && b.cursorX < term.cols)) return null;
    const g = screenEl().getBoundingClientRect();
    if (!(g.width > 0 && g.height > 0)) return null;
    if (g.bottom <= 0 || g.right <= 0 || g.top >= window.innerHeight || g.left >= window.innerWidth) return null;
    const cw = g.width / term.cols, ch = g.height / term.rows;
    const gx = Math.round(g.left), gy = Math.round(g.top);
    const gw = Math.round(g.width), gh = Math.round(g.height);
    const x = Math.round(g.left + b.cursorX * cw), y = Math.round(g.top + b.cursorY * ch);
    // The whole cell, kept inside the grid: the last column/row can round a fraction of a pixel past its edge, and the
    // window rejects a rect that is not entirely inside the webview.
    const w = Math.max(1, Math.min(Math.round(cw), gx + gw - x));
    const h = Math.max(1, Math.min(Math.round(ch), gy + gh - y));
    if (!(w >= 1 && h >= 1) || x < gx || y < gy || x + w > gx + gw || y + h > gy + gh) return null;
    // The textarea should sit on this very cell; when it does not (a font/resize it has not followed yet), the grid
    // still is the caret, so the cross-check only reports the disagreement (it never changes the numbers).
    const r = ta.getBoundingClientRect();
    const agree = [r.left, r.top, r.width, r.height].every(Number.isFinite)
      && Math.abs(r.left - (g.left + b.cursorX * cw)) <= 1
      && Math.abs(r.top - (g.top + b.cursorY * ch)) <= 1
      && Math.abs(r.width - cw) <= 1 && Math.abs(r.height - ch) <= 1;
    return { x, y, w, h, agree };
  };
  // One small message of its own (never an `inset` resend): a caret line, or `caret: null` when the terminal is not
  // the real focus target. Deduped, so a cursor event that does not move the cell sends nothing.
  // macOS keeps WebKit's own behaviour: it sends **nothing at all** — not even the initial `caret: null`.
  // `force` is for a context change that can move the caret in *window* coordinates while the page rect stays the
  // same (a font change, a resize, a window-state change): the window then re-converts and decides whether the IM
  // really needs a new rect. The page's own key stays deduped, so a cursor event that changes nothing sends nothing.
  const reportCaret = (force) => {
    if (IS_MAC) return;
    const c = caretCell();
    const key = c ? c.x + ',' + c.y + ',' + c.w + ',' + c.h : 'null';
    if (!force && key === lastCaret) return;
    lastCaret = key;
    send('caret', c ? { x: c.x, y: c.y, w: c.w, h: c.h } : null);
  };
  // A26: push row 0 down so its centre is the window controls' centre (`CONTROL_CENTER`), which keeps the toolbar
  // and the fleet title/status on the controls' line at any font size. A cell taller than the centre may go a few
  // px above the window top (the empty space above xterm's glyphs), never more than the glyph's own top margin.
  const termEl = document.getElementById('term');
  function placeTopbar() {
    const c = cellSize();
    if (!(c.h > 0)) return;
    // Only macOS puts native controls over row 0; Linux's titlebar is the WM's own, above the whole page.
    if (!IS_MAC) {
      if (topPad !== 0) {
        topPad = 0;
        termEl.style.top = '0px';
        termEl.style.height = '';
      }
      return;
    }
    const floor = -Math.max(0, (c.h - fontSize) / 2);
    const pad = Math.max(floor, Math.min(CONTROL_CENTER, CONTROL_CENTER - c.h / 2));
    const px = Math.round(pad * 100) / 100;
    if (px === topPad) return;
    topPad = px;
    // The pad is taken off the grid's own height, so the last row stays whole: the window never clips the bottom
    // row, and the terminal still ends at the window's bottom edge (a negative pad, a cell taller than twice the
    // centre, starts the grid above the window top and lets the glyph's top margin take the difference).
    termEl.style.top = px + 'px';
    termEl.style.height = px >= 0 ? 'calc(100% - ' + px + 'px)' : 'calc(100% + ' + -px + 'px)';
  }
  const refit = () => {
    placeTopbar();
    const d = fit.proposeDimensions();
    if (d && d.cols > 0 && d.rows > 0 && (d.cols !== term.cols || d.rows !== term.rows)) term.resize(d.cols, d.rows);
    reportSize();
    reportInset();
    placeOverlay();
    placeScrollbars();
    reportCaret(true); // a font change or a resize moves the caret under the IM (and can move the webview's origin)
  };
  let resizeTimer = 0;
  new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(refit, 50); })
    .observe(document.getElementById('term'));

  // ---- overlay: the history iframe over the lower-left cell ----
  const hist = document.createElement('iframe');
  hist.id = 'history';
  hist.setAttribute('sandbox', 'allow-scripts allow-popups'); // never allow-same-origin / allow-top-navigation
  hist.setAttribute('title', 'history');
  screenEl().appendChild(hist);
  let cells = null;      // last 0x85 rect (cells) or null
  let loadedOk = false;  // the iframe holds a page that loaded fine
  let requested = false; // fm.load has been called (before that the iframe is empty)
  let curHref = '';      // the iframe's current fleetmux://page/… address
  let focusWanted = 0;   // `focus` asked while hidden: give it when visible, until this time
  const cellSize = () => {
    const r = screenEl().getBoundingClientRect();
    return { w: r.width / term.cols, h: r.height / term.rows };
  };
  const visible = () => !!cells && loadedOk;
  function placeOverlay() {
    if (!visible()) { hist.style.visibility = 'hidden'; return; }
    const c = cellSize();
    hist.style.left = cells.x * c.w + 'px';
    hist.style.top = cells.y * c.h + 'px';
    hist.style.width = cells.w * c.w + 'px';
    hist.style.height = cells.h * c.h + 'px';
    hist.style.visibility = 'visible';
    if (focusWanted && performance.now() < focusWanted) { focusWanted = 0; focusPage(); }
    reportCaret(); // the overlay can take the focus away from the terminal (and give it back)
  }
  const applyOverlay = ov => {
    const r = ov && ov.rect;
    const ok = r && [r.x, r.y, r.w, r.h].every(n => Number.isInteger(n) && n >= 0) && r.w > 0 && r.h > 0;
    cells = ok ? { x: r.x, y: r.y, w: r.w, h: r.h } : null;
    placeOverlay();
  };
  function focusPage() {
    if (!visible()) { focusWanted = performance.now() + 2000; return; }
    hist.focus();
    try { hist.contentWindow.focus(); } catch (e) { /* cross-origin focus is allowed; ignore if not */ }
    reportCaret();
  }

  // ---- the page must not swallow a drag that starts on the terminal ----
  // The iframe lies over the screen's lower-left cells, so a selection or a mouse report that starts on the terminal
  // and moves down into it would be delivered to the page instead: the terminal never sees the moves or the release.
  // While a button that went down on the terminal is still down, the iframe ignores the pointer; every way that
  // button can come up restores it — mouseup anywhere in the window, a release outside the window (blur, or a later
  // button-less mousemove once the pointer is back), a cancelled drag. A mousedown inside the page never reaches this
  // document, and one at the iframe element itself is not a terminal drag, so a direct click on the page still works.
  let dragDown = false;
  const pointerOff = () => {
    if (dragDown) return;
    dragDown = true;
    hist.style.pointerEvents = 'none';
  };
  const pointerOn = () => {
    if (!dragDown) return;
    dragDown = false;
    hist.style.pointerEvents = '';
  };
  window.addEventListener('mousedown', ev => {
    if (ev.target === hist || !screenEl().contains(ev.target)) return;
    pointerOff();
  }, true);
  window.addEventListener('mouseup', pointerOn, true);
  window.addEventListener('mousemove', ev => { if (dragDown && ev.buttons === 0) pointerOn(); }, true);
  window.addEventListener('blur', pointerOn);
  window.addEventListener('pointercancel', pointerOn, true);
  window.addEventListener('dragend', pointerOn, true);
  document.addEventListener('visibilitychange', () => { if (document.hidden) pointerOn(); });

  // ---- scrollbars: one per visible cell, from 0x86 (PROTOCOL.md §6.3) ----
  // The server owns the history (this page's xterm.js has `scrollback: 0`), so a scrollbar is a DOM element over the
  // right edge of its cell: the thumb shows `offset / total`, a drag or a click on the track sends
  // `{"type":"scroll","pane","offset"}` (the window process turns it into `0x05`) and the next `0x86` is
  // authoritative. A new GUI against an old server never receives a `0x86`, so it never creates an element and
  // never sends `0x05` (which an old server would refuse as an unknown type).
  // The bar is transient (cmux-like): invisible at rest and taking no pointer at all; it appears instantly when that
  // pane's own offset changes, while its thumb is dragged (however long it is held still) or while the pointer is
  // over the right 12 px of the cell, and it fades out (0.3 s, see gui.css) about a second after the last activity —
  // never during a drag. No track colour: a 6 px thumb 2 px from the edge, 10 px and more opaque while hovered or
  // dragged; the `offset/total` label shows only while dragging. Colours are fixed for now; take them from the
  // palette after the theme branch merges.
  const SB_HIT = 12;    // px, the pointer sensing zone at the cell's right edge (the only interactive part)
  const SB_HOLD = 1000; // ms, how long the bar stays after the last activity before it starts to fade (~1 s)
  const SB_MIN = 16;    // px, the shortest thumb (unchanged)
  const sbState = new Map(); // pane → {pane, cells, offset, total, top, el, thumb, label, px, thumbBox, hovered, hideTimer}
  let sbDrag = null;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const sbInside = (s, x, y) => {
    if (!s.el) return false;
    const r = s.el.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  };
  // Instant on: `.sb.show` disables the opacity transition; the fade only runs on the way out (gui.css). Every
  // activity also (re)arms the idle fade — a drag or a hover cancels it in sbHideSoon instead.
  function sbShow(s) {
    if (!s.el) return;
    clearTimeout(s.hideTimer);
    s.hideTimer = 0;
    s.el.style.transition = ''; // a hidden-window hide pins the end state inline (see sbFadeEnd)
    s.el.style.opacity = '';
    s.el.classList.add('show');
    sbHideSoon(s);
  }
  // A window the OS stopped compositing (occluded / hidden, `document.hidden`) freezes the opacity transition, so
  // the fade would never finish; nothing is visible then anyway, so put the bar straight at its end state.
  const sbFadeEnd = s => {
    if (!s.el || s.el.classList.contains('show')) return;
    s.el.style.transition = 'none'; // cancel the frozen transition, then jump: a running transition would freeze again
    s.el.style.opacity = '0';
  };
  // ~1 s after the last activity, then the CSS opacity transition fades it out. A drag holds it up instead.
  function sbHideSoon(s) {
    if (!s.el || s.hovered || (sbDrag && sbDrag.pane === s.pane)) return;
    clearTimeout(s.hideTimer);
    s.hideTimer = setTimeout(() => {
      s.hideTimer = 0;
      if (sbDrag && sbDrag.pane === s.pane) return; // a drag started in the meantime
      s.el.classList.remove('show');
      if (document.hidden) sbFadeEnd(s);
    }, SB_HOLD);
  }

  function sbElement(s) {
    const el = document.createElement('div');
    el.className = 'sb';
    el.dataset.pane = s.pane;
    const thumb = document.createElement('div');
    thumb.className = 'sb-thumb';
    const label = document.createElement('div');
    label.className = 'sb-label';
    el.appendChild(thumb);
    el.appendChild(label);
    // mousedown on the bar never reaches xterm.js: no SGR press / selection, focus stays in its textarea.
    el.addEventListener('mousedown', ev => sbDown(s.pane, ev));
    screenEl().appendChild(el);
    s.el = el;
    s.thumb = thumb;
    s.label = label;
  }

  // One or more 0x86 states from one batch: each is a whole state, the last one wins. Create / update / remove the
  // elements of its panes, then place them. Applied in the term.write callback, so the thumb never moves before
  // the screen it describes. No 0x86 in the batch (an old server never sends one) → no element ever exists.
  function applyScrolls(list) {
    if (!list || !list.length) return;
    const last = list[list.length - 1];
    const panes = last && Array.isArray(last.panes) ? last.panes : null;
    if (!panes) return;
    const seen = new Set();
    for (const e of panes) {
      const r = e && e.rect;
      const ok = e && typeof e.pane === 'string' && r &&
        [r.x, r.y, r.w, r.h].every(n => Number.isInteger(n) && n >= 0) && r.w > 0 && r.h > 0 &&
        Number.isInteger(e.offset) && Number.isInteger(e.total);
      if (!ok) continue;
      seen.add(e.pane);
      let s = sbState.get(e.pane);
      const created = !s;
      if (!s) {
        s = { pane: e.pane, cells: null, offset: 0, total: 0, top: null, hovered: false, hideTimer: 0 };
        sbState.set(e.pane, s);
      }
      s.cells = { x: r.x, y: r.y, w: r.w, h: r.h };
      s.total = e.total;
      if (!s.el) sbElement(s);
      // While that pane is dragged the local position wins (only `total` follows the server). Activity is exactly a
      // *changed* offset of that pane: the first 0x86 of a pane (whatever its offset) and a repeated state never
      // flash a bar, and another pane's offset never touches this one.
      if (!sbDrag || sbDrag.pane !== e.pane) {
        const moved = !created && s.offset !== e.offset;
        s.offset = e.offset;
        if (moved) sbShow(s);
      }
    }
    for (const [pane, s] of [...sbState]) {
      if (seen.has(pane)) continue;
      clearTimeout(s.hideTimer);
      if (s.el) s.el.remove();
      sbState.delete(pane);
      if (sbDrag && sbDrag.pane === pane) sbDrag = null;
    }
    placeScrollbars();
  }

  function placeScrollbars() {
    if (!sbState.size) return;
    const c = cellSize();
    for (const s of sbState.values()) {
      const px = { x: (s.cells.x + s.cells.w) * c.w - SB_HIT, y: s.cells.y * c.h, w: SB_HIT, h: s.cells.h * c.h };
      s.el.style.left = px.x + 'px';
      s.el.style.top = px.y + 'px';
      s.el.style.width = px.w + 'px';
      s.el.style.height = px.h + 'px';
      const rows = s.total + s.cells.h; // history + the screen itself
      const thumbH = Math.max(SB_MIN, px.h * s.cells.h / rows);
      const travel = Math.max(0, px.h - thumbH);
      const at = s.top != null
        ? clamp(s.top, 0, travel)
        : s.total === 0 ? 0 : travel * clamp((s.total - s.offset) / s.total, 0, 1);
      s.thumb.style.top = at + 'px';
      s.thumb.style.height = thumbH + 'px';
      s.label.textContent = s.offset + '/' + s.total;
      s.label.hidden = !(sbDrag && sbDrag.pane === s.pane); // the offset/total label is a drag affordance only
      s.px = px;
      s.thumbBox = { top: at, height: thumbH };
    }
  }

  function sbDown(pane, ev) {
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.button !== 0) return;
    const s = sbState.get(pane);
    if (!s || !s.el) return;
    const track = s.el.getBoundingClientRect();
    const thumb = s.thumb.getBoundingClientRect();
    const onThumb = ev.clientY >= thumb.top && ev.clientY <= thumb.bottom;
    sbDrag = {
      pane,
      // A real drag keeps the lost-release guards below armed; a synthetic one (the graphical tests dispatch
      // MouseEvents, so the browser's button state never changes) only ends on its own mouseup.
      trusted: !!ev.isTrusted,
      grab: onThumb ? ev.clientY - thumb.top : thumb.height / 2,
      trackTop: track.top,
      travel: Math.max(0, track.height - thumb.height),
      sent: null,
      pending: null,
      raf: 0,
    };
    s.hovered = true;
    s.el.classList.add('hover');
    s.el.classList.add('drag');
    sbShow(s); // a drag holds the bar visible, however long the thumb is held still
    if (onThumb) {
      s.top = clamp(thumb.top - track.top, 0, sbDrag.travel);
      placeScrollbars();
    } else {
      sbTo(ev.clientY); // a click on the track jumps there
    }
  }

  // Move the dragged thumb locally first (feel), then send the value it means — at most once per animation frame.
  function sbTo(clientY) {
    const d = sbDrag;
    if (!d) return;
    const s = sbState.get(d.pane);
    if (!s) return;
    const top = clamp(clientY - d.trackTop - d.grab, 0, d.travel);
    const offset = d.travel === 0 ? 0 : Math.round(s.total * (1 - top / d.travel));
    s.top = top;
    s.offset = offset;
    placeScrollbars();
    sbQueue(offset);
  }

  function sbQueue(offset) {
    const d = sbDrag;
    if (!d) return;
    d.pending = offset;
    if (d.raf) return;
    d.raf = requestAnimationFrame(() => {
      if (sbDrag !== d) return;
      d.raf = 0;
      sbFlush();
    });
  }

  function sbFlush() {
    const d = sbDrag;
    if (!d || d.pending == null) return;
    const offset = d.pending;
    d.pending = null;
    if (d.sent === offset) return;
    d.sent = offset;
    send('scroll', { pane: d.pane, offset });
  }

  // The release (or a lost button / focus) ends the drag: flush the final value, then let 0x86 rule again.
  function sbEnd(ev) {
    const d = sbDrag;
    if (!d) return;
    if (d.raf) { cancelAnimationFrame(d.raf); d.raf = 0; }
    sbFlush();
    sbDrag = null;
    const s = sbState.get(d.pane);
    if (!s) return;
    s.el.classList.remove('drag');
    s.top = null;
    placeScrollbars();
    // Released on the bar itself: still hovered, so it stays up and grabbable; anywhere else it is idle again.
    s.hovered = !!(ev && ev.clientX != null && sbInside(s, ev.clientX, ev.clientY));
    if (s.hovered) s.el.classList.add('hover');
    else { s.el.classList.remove('hover'); sbHideSoon(s); }
  }

  // The same window capture-phase pattern as the iframe drag protection above (no Pointer Capture: the graphical
  // tests dispatch synthetic MouseEvents, which have no active pointer). The bar is invisible and `pointer-events:
  // none` at rest, so hovering is detected here by coordinates: only the right 12 px of a cell shows it (and makes
  // it grabbable); everywhere else the terminal keeps every click.
  window.addEventListener('mousemove', ev => {
    if (!sbDrag) {
      let hit = null;
      for (const s of sbState.values()) {
        if (sbInside(s, ev.clientX, ev.clientY)) { hit = s; break; }
      }
      for (const s of sbState.values()) {
        if (!s.el) continue;
        if (s === hit) { s.hovered = true; s.el.classList.add('hover'); sbShow(s); }
        else if (s.hovered) { s.hovered = false; s.el.classList.remove('hover'); sbHideSoon(s); }
      }
      return;
    }
    if (ev.buttons === 0) {
      // The release happened outside the window — but only a real drag can lose it that way; a stray trusted
      // mousemove in the window must not end a synthetic test drag.
      if (sbDrag.trusted) sbEnd(ev);
      return;
    }
    sbTo(ev.clientY);
  }, true);
  window.addEventListener('mouseup', ev => { if (sbDrag) sbEnd(ev); }, true);
  // Losing the window drops a hover (a drag too, if it is a real one): no pointer can stay over the bar.
  window.addEventListener('blur', () => {
    for (const s of sbState.values()) {
      if (!s.el || !s.hovered) continue;
      s.hovered = false;
      s.el.classList.remove('hover');
      sbHideSoon(s);
    }
    if (sbDrag && sbDrag.trusted) sbEnd();
  });
  window.addEventListener('pointercancel', () => { if (sbDrag && sbDrag.trusted) sbEnd(); }, true);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) return;
    for (const s of sbState.values()) sbFadeEnd(s);
  });

  // ---- the titlebar drag zone (macOS, plan §17.2) ----
  // The macOS window has a transparent, title-less titlebar (gui.rs), so row 0 of the grid sits on the traffic
  // lights; Linux has no row 0 (its one header row is the GTK headerbar) and never takes this path. A left press on a row-0 cell the server does nothing with — blank, or a group border
  // `─` — never reaches xterm.js: the page asks the window process to drag the window (`drag_window`), and a double
  // click there zooms it (`zoom_window`, the macOS habit). A button cell (`◫ + ← →`) or a label cell is not a drag
  // zone, and neither is an overlay (the history iframe, a scrollbar, a window control, a resize grip): those keep
  // their own pointer.
  const TITLE_ROW = 0;
  const cellAt = (x, y) => {
    const r = screenEl().getBoundingClientRect();
    const c = cellSize();
    if (!(c.w > 0) || !(c.h > 0) || x < r.left || y < r.top) return null;
    const col = Math.floor((x - r.left) / c.w), row = Math.floor((y - r.top) / c.h);
    if (col < 0 || row < 0 || col >= term.cols || row >= term.rows) return null;
    return { col, row };
  };
  // The server's own rule for these cells: a blank sidebar cell is `Nothing`, a border is `Outside`.
  const dragCell = cell => {
    if (!cell || cell.row !== TITLE_ROW) return false;
    const line = term.buffer.active.getLine(cell.row);
    const c = line && line.getCell(cell.col);
    const ch = c ? c.getChars() : '';
    return ch === '' || ch === ' ' || ch === '─';
  };
  // An overlay over row 0 (the history iframe, a scrollbar) keeps its pointer: not a drag zone.
  const overOverlay = el => !!el && (el === hist || !!(el.closest && el.closest('.sb')));
  // The strip above row 0 (the px `placeTopbar` adds) is part of the window's titlebar band too: dragging there
  // moves the window, a double click zooms it. Only when the press is not on an overlay.
  const titleZone = ev => {
    if (!IS_MAC || ev.button !== 0) return false;
    if (overOverlay(ev.target)) return false;
    if (ev.clientY < topPad) return true;
    if (!screenEl().contains(ev.target)) return false;
    return dragCell(cellAt(ev.clientX, ev.clientY));
  };
  window.addEventListener('mousedown', ev => {
    if (!titleZone(ev)) return;
    ev.preventDefault();
    ev.stopPropagation();
    send('drag_window', {});
  }, true);
  window.addEventListener('dblclick', ev => {
    if (!titleZone(ev)) return;
    ev.preventDefault();
    ev.stopPropagation();
    send('zoom_window', {});
  }, true);

  // Messages from the iframe: only the three of PROTOCOL.md §15.4, only from that iframe.
  let loadTimer = 0, gotLoadMsg = false;
  window.addEventListener('message', ev => {
    if (ev.source !== hist.contentWindow || !requested) return;
    const m = ev.data;
    if (!m || typeof m !== 'object') return;
    if (m.fm === 'prefix') {
      inPage = false;
      term.focus();
      sendInput(new Uint8Array([PREFIX_BYTE]));
    } else if (m.fm === 'url' && typeof m.href === 'string' && m.href.startsWith('fleetmux://page/')) {
      curHref = m.href;
      send('page_url', { url: m.href });
      if (m.why === 'load') {
        gotLoadMsg = true;
        loadedOk = m.ok !== false;
        placeOverlay();
        send('page_loaded', { ok: loadedOk });
      }
    } else if (m.fm === 'history_focus') {
      if (!inPage) { inPage = true; send('history_focus', {}); }
      reportCaret(); // the terminal is no longer the focus target: the window must drop the IM caret
    }
  });
  hist.addEventListener('load', () => {
    if (!requested) return;
    // frame.js reports the load itself; a document it could not run in counts as a failed load
    clearTimeout(loadTimer);
    loadTimer = setTimeout(() => {
      if (gotLoadMsg) return;
      loadedOk = false;
      placeOverlay();
      send('page_loaded', { ok: false });
    }, 500);
  });
  // Focus in/out and the window's own focus are caret events too: a refocus must re-send the same cell (the window
  // drops its cache), and a blur must invalidate the IM caret instead of leaving a stale one. A focus is **forced**:
  // the page's own key can still hold the same cell from before the blur, and the window's re-push of its retained
  // value must not be the only thing that happens (either order of the page's `window focus` and tao's `Focused`
  // ends with the live cell). A blur is not forced: `caret: null` is deduped like any other report.
  term.textarea && term.textarea.addEventListener('focus', () => { inPage = false; reportCaret(true); });
  term.textarea && term.textarea.addEventListener('blur', () => reportCaret());
  window.addEventListener('focus', () => reportCaret(true));
  window.addEventListener('blur', () => reportCaret());
  term.onCursorMove(reportCaret);

  // ---- keys handled by the page: copy / paste / font size (no app menu in the window) ----
  let fontSize = DEFAULT_FONT;
  const setFont = n => {
    fontSize = Math.max(8, Math.min(32, n));
    term.options.fontSize = fontSize;
    refit();
    reportInset(); // a bigger font is a wider cell, so the traffic lights cover fewer columns (plan §17.3)
  };
  term.attachCustomKeyEventHandler(e => {
    if (e.type !== 'keydown') return true;
    const mod = IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
    if (!mod || e.altKey) return true;
    const k = e.key.toLowerCase();
    const clip = IS_MAC ? !e.shiftKey : e.shiftKey; // Cmd-C / Cmd-V, Ctrl-Shift-C / Ctrl-Shift-V
    if (clip && k === 'c') {
      const sel = term.getSelection();
      if (sel) send('copy', { text: sel });
      e.preventDefault();
      return false;
    }
    if (clip && k === 'v') {
      send('paste', {});
      e.preventDefault();
      return false;
    }
    if (k === '=' || k === '+') { setFont(fontSize + 1); e.preventDefault(); return false; }
    if (k === '-' || k === '_') { setFont(fontSize - 1); e.preventDefault(); return false; }
    if (k === '0' && !e.shiftKey) { setFont(DEFAULT_FONT); e.preventDefault(); return false; }
    return true;
  });
  // copy-on-select (PROTOCOL.md §12): a selection the page itself made (Option-click / forced selection, xterm's own
  // double click) is copied on mouseup, like the server's selections. Deferred: xterm finishes the selection in its
  // own mouseup handler, after this capture listener.
  let lastAutoCopy = '';
  window.addEventListener('mouseup', () => setTimeout(() => {
    const sel = term.hasSelection() ? term.getSelection() : '';
    if (sel && sel !== lastAutoCopy) send('copy', { text: sel });
    lastAutoCopy = sel;
  }, 0), true);
  // OSC 52 from the server (drag-select copy, PROTOCOL.md §12): Pc;Pd with Pd base64; "?" (query) is ignored.
  term.parser.registerOscHandler(52, data => {
    const i = data.indexOf(';');
    const pd = i < 0 ? '' : data.slice(i + 1);
    if (!pd || pd === '?') return true;
    try { send('copy', { text: new TextDecoder().decode(fromB64(pd)) }); } catch (e) { /* not base64 */ }
    return true;
  });

  let noticeTimer = 0;
  const notice = msg => {
    const n = document.getElementById('notice');
    n.textContent = msg;
    n.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { n.hidden = true; }, 5000);
  };

  // ---- the window process's side (evaluate_script) ----
  let lastSeq = 0;
  // The terminal's visible rows, and the scrollbars on screen: one implementation each, shared by the public
  // `fm.text()` / `fm.scrollbars()` and by the test-only atomic snapshot below, so they cannot drift.
  const textRows = () => {
    const b = term.buffer.active, rows = [];
    for (let y = 0; y < term.rows; y++) {
      const line = b.getLine(b.viewportY + y);
      rows.push(line ? line.translateToString(true) : '');
    }
    return rows;
  };
  const barsOf = () => [...sbState.values()].map(s => ({
    pane: s.pane,
    cells: s.cells ? { x: s.cells.x, y: s.cells.y, w: s.cells.w, h: s.cells.h } : null,
    offset: s.offset,
    total: s.total,
    px: s.px ? { x: s.px.x, y: s.px.y, w: s.px.w, h: s.px.h } : null,
    thumb: s.thumbBox ? { top: s.thumbBox.top, height: s.thumbBox.height } : null,
  }));
  const fmApi = {
    // One batch of screen bytes in order; the overlays and scroll states that came after them apply once they are
    // written (the last one wins), so a thumb never moves before the screen it describes.
    feed(seq, b64, overlays, scrolls) {
      if (seq !== lastSeq + 1) console.warn('fleetmux: batch', seq, 'after', lastSeq);
      lastSeq = seq;
      term.write(fromB64(b64), () => {
        for (const ov of overlays || []) applyOverlay(ov);
        applyScrolls(scrolls || []);
        reportCaret(); // a fed screen moves the cursor (and a pane switch changes which one it is)
        send('ack', { seq });
      });
    },
    load(url) {
      requested = true;
      gotLoadMsg = false;
      const sameDoc = hist.getAttribute('src') && url.split('#')[0] === (curHref || hist.src).split('#')[0];
      if (!sameDoc) loadedOk = false;
      placeOverlay();
      hist.src = url;
    },
    reloadPage() {
      if (!requested) return;
      gotLoadMsg = false;
      hist.contentWindow.postMessage({ fm: 'reload' }, '*');
    },
    focusPage,
    paste(b64) { term.paste(new TextDecoder().decode(fromB64(b64))); },
    notice,
    text() { return textRows(); },
    overlay() {
      const v = visible();
      let px = null;
      if (v) px = { x: parseFloat(hist.style.left), y: parseFloat(hist.style.top),
        w: parseFloat(hist.style.width), h: parseFloat(hist.style.height) };
      return { visible: v, cells: v ? cells : null, px, src: requested ? (curHref || hist.getAttribute('src') || '') : '' };
    },
    // The scrollbars on screen, for the graphical tests (through `js`, PROTOCOL.md §15.4); `[]` before the first
    // 0x86. `thumb.top` is the thumb's offset inside its track, `px` its place on the page.
    scrollbars() { return barsOf(); },
    renderer() { return webgl ? 'webgl' : 'dom'; },
  };
  // `--test-hooks` only (PROTOCOL.md §15.4): ONE synchronous, read-only sample of the live grid, the screen rect
  // and the bars, so a refit cannot interleave between them. `term.cols` / `term.rows` are the page's authoritative
  // live grid; `textRows` is the visible-row count. It never mutates state, never calls refit / reportSize /
  // placeScrollbars, sends no ipc and returns no terminal text. Without --test-hooks the property is absent.
  if (boot.testHooks) {
    // `--test-hooks` only: the caret reporter's own decision, so a graphical test can read the real DOM guards back
    // (the terminal's textarea is the active element, the page has the focus, no iframe, the cell is a live cell) and
    // the exact key it last sent. Read-only: it sends no ipc, focuses nothing and moves nothing.
    fmApi.caret = () => ({
      sent: lastCaret,
      cell: caretCell(),
      active: document.activeElement === term.textarea,
      hasFocus: document.hasFocus(),
      inPage: inPage,
    });
    fmApi.snapshot = () => {
      const r = screenEl().getBoundingClientRect();
      return {
        cols: term.cols,
        rows: term.rows,
        textRows: textRows().length,
        screen: { x: r.x, y: r.y, w: r.width, h: r.height },
        renderer: webgl ? 'webgl' : 'dom',
        scrollbars: barsOf(),
      };
    };
  }
  window.fm = Object.freeze(fmApi);

  // window.close() from this main frame (PROTOCOL.md §15.4): with --test-hooks (graphical tests only) it closes the
  // window through the same path as the person closing it; otherwise it does nothing. It never reaches WebKit's own
  // close: on Linux wry answers that by destroying the web view (webkitgtk/mod.rs:474), not by closing the window.
  window.close = boot.testHooks ? () => send('test_close', {}) : () => {};

  fit.fit();
  placeTopbar(); // A26: row 0 on the window controls' centre before the first size is reported
  reportInset(); // S1e: before the first size, so the client's view handshake carries it (plan §17.2)
  reportSize(); // the first 0x02 (the client's handshake size)
  term.focus();
})();
