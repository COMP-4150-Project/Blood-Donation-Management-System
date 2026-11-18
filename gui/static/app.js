/* Vytal GUI behaviour: motion, toasts, confirmations, live filters. Everything degrades to plain forms and links without JS. */
(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // stagger the page entrance
  $$('.page > *').forEach((el, i) => el.style.setProperty('--i', Math.min(i, 8)));
  $$('tbody tr').forEach((tr, i) => { tr.classList.add('reveal-row'); tr.style.setProperty('--r', Math.min(i, 14)); });

  // animated numbers (RSP-01): the server renders the real value; motion only replays it from 0 when the number is on the first screen
  const fmt = (v, dec, grouped) => grouped ? v.toLocaleString('en-CA', { minimumFractionDigits: dec, maximumFractionDigits: dec }) : v.toFixed(dec);
  const count = (el, still) => {
    const raw = el.dataset.count, end = parseFloat(raw), dec = (raw.split('.')[1] || '').length, shown = el.textContent.trim();
    if (isNaN(end)) return;
    const final = shown && parseFloat(shown.replace(/,/g, '')) === end ? shown : fmt(end, dec, false), grouped = shown.includes(',');
    if (reduce || end === 0 || still) { el.textContent = final; return; }
    const t0 = performance.now(), dur = 900;
    const tick = t => { const p = Math.min((t - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3); el.textContent = p < 1 ? fmt(end * e, dec, grouped) : final; if (p < 1) requestAnimationFrame(tick); };
    el.textContent = fmt(0, dec, grouped); requestAnimationFrame(tick);
  };
  const ringOff = el => 326.7 * (1 - Math.min(Math.max(parseFloat(el.dataset.p) || 0, 0), 100) / 100);
  const settle = el => {   // final state, no motion
    if (el.dataset.w) el.style.setProperty('--w', el.dataset.w + '%');
    if (el.classList.contains('ring')) el.querySelector('.fg').style.strokeDashoffset = ringOff(el);
  };
  const grow = $$('[data-count],[data-w],.ring');
  if (reduce || !window.IntersectionObserver) grow.forEach(el => { settle(el); if (el.dataset.count) count(el); });
  else {
    // what is on the first screen grows in; the rest (and hidden tab panels) shows its final state at once,
    // so long pages, full-page captures and print never show empty bars
    const io = new IntersectionObserver(es => { es.forEach(e => {
      const el = e.target; io.unobserve(el); const seen = e.isIntersecting && e.intersectionRatio > 0;
      if (el.dataset.count) count(el, !seen);
      if (seen) requestAnimationFrame(() => settle(el)); else settle(el);
    }); });
    grow.forEach(el => { if (el.dataset.w) el.style.setProperty('--w', '0%'); io.observe(el); });
  }

  // times are stored in UTC; show them in the viewer's time zone
  $$('time[datetime]').forEach(t => {
    const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(t.dateTime); const [yy, mm, dd] = dateOnly ? t.dateTime.split('-').map(Number) : [];
    const d = dateOnly ? new Date(yy, mm - 1, dd) : new Date(t.dateTime); if (isNaN(d)) return;
    const o = { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' };
    if (t.dataset.rel) { const m = Math.round((d - Date.now()) / 60000), a = Math.abs(m); t.title = d.toLocaleString([], o);
      t.textContent = a < 1 ? 'just now' : a < 60 ? `${m < 0 ? '' : 'in '}${a} min${m < 0 ? ' ago' : ''}` : a < 2880 ? `${m < 0 ? '' : 'in '}${Math.round(a / 60)} h${m < 0 ? ' ago' : ''}` : d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
    } else t.textContent = d.toLocaleString([], t.dataset.date ? { month: 'short', day: 'numeric', year: 'numeric' } : o);
  });

  // sticky header shadow
  const top = $('.top'); if (top) addEventListener('scroll', () => top.classList.toggle('scrolled', scrollY > 6), { passive: true });
  // phone and tablet menu: toggle button, height of the sticky bar recomputed on resize and rotation
  const side = $('.side'), mb = $('#menu'), narrow = matchMedia('(max-width:1000px)');
  const setH = () => document.documentElement.style.setProperty('--sideh', side && narrow.matches && getComputedStyle(side).position === 'sticky' ? $('.side-bar', side).offsetHeight + 17 + 'px' : '0px');
  const setMenu = open => { if (!side || !mb) return; side.classList.toggle('open', open); mb.setAttribute('aria-expanded', String(open)); };
  if (side) {
    setH(); addEventListener('resize', setH); addEventListener('orientationchange', () => setTimeout(setH, 200));
    if (window.ResizeObserver) new ResizeObserver(setH).observe(side);
    narrow.addEventListener ? narrow.addEventListener('change', () => { setMenu(false); setH(); }) : 0;
    if (mb) {
      mb.addEventListener('click', () => setMenu(mb.getAttribute('aria-expanded') !== 'true'));
      addEventListener('keydown', e => { if (e.key === 'Escape' && side.classList.contains('open')) { setMenu(false); mb.focus(); } });
      document.addEventListener('click', e => { if (side.classList.contains('open') && !side.contains(e.target)) setMenu(false); });
    }
  }

  // soft page transitions
  addEventListener('pageshow', () => document.body.classList.remove('leaving'));
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href]'); if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.hasAttribute('download')) return;
    const u = new URL(a.href, location.href); if (u.origin !== location.origin || (u.pathname === location.pathname && u.hash) || u.pathname.endsWith('.csv') || reduce) return;
    e.preventDefault(); document.body.classList.add('leaving'); setTimeout(() => location.href = a.href, 150);
  });

  // toasts
  const box = $('.toasts');
  const closeToast = t => { t.classList.add('out'); setTimeout(() => t.remove(), 330); };
  $$('.toast').forEach((t, i) => { const err = t.classList.contains('err'); t.style.setProperty('--ttl', '6000ms'); t.style.animationDelay = i * 120 + 'ms';
    t.querySelector('button').onclick = () => closeToast(t); if (!err) setTimeout(() => closeToast(t), 6000 + i * 120); });
  window.toast = (msg, kind = 'info') => { const t = document.createElement('div'); t.className = 'toast ' + kind; t.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    t.innerHTML = '<div></div><button type="button" aria-label="Dismiss">&times;</button>'; t.firstChild.textContent = msg; t.querySelector('button').onclick = () => closeToast(t); box.append(t); if (kind !== 'err') setTimeout(() => closeToast(t), 5000); };

  // confirmation dialog for destructive actions (US-07)
  const dlg = $('#confirm-dialog');
  if (dlg) {
    let pending = null, opener = null;
    document.addEventListener('submit', e => {
      const f = e.target, btn = e.submitter, msg = (btn && btn.dataset.confirm) || f.dataset.confirm;
      if (!msg || f.dataset.ok) return;
      e.preventDefault(); pending = { f, btn };
      $('#confirm-title').textContent = (btn && btn.dataset.title) || f.dataset.title || 'Please confirm';
      $('#confirm-text').textContent = msg; $('#confirm-go').textContent = (btn && btn.dataset.go) || f.dataset.go || 'Confirm';
      opener = btn || document.activeElement; dlg.showModal(); $('#confirm-go').focus();
    }, true);
    // keep Tab and Shift+Tab inside the open dialog, and hand focus back to whatever opened it
    dlg.addEventListener('keydown', e => {
      if (e.key !== 'Tab') return;
      const items = [...dlg.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(x => !x.disabled && x.offsetParent !== null);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !dlg.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !dlg.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    });
    dlg.addEventListener('cancel', () => { pending = null; });
    dlg.addEventListener('close', () => { const o = opener; opener = null; if (o && o.isConnected && typeof o.focus === 'function') o.focus(); });
    $('#confirm-go').onclick = () => { dlg.close(); if (!pending) return; const { f, btn } = pending; f.dataset.ok = '1'; f.requestSubmit(btn); };
    $('#confirm-no').onclick = () => { dlg.close(); pending = null; };
    dlg.addEventListener('click', e => { if (e.target === dlg) { dlg.close(); pending = null; } });
  }
  // busy state on submit so slow calls feel intentional, and no double submits
  document.addEventListener('submit', e => {
    if (e.defaultPrevented) return; const b = e.submitter || $('button[type=submit]', e.target);
    if (b && b.classList.contains('btn')) setTimeout(() => b.classList.add('busy'), 0);
  });

  // live table filter
  $$('[data-filter]').forEach(inp => {
    const rows = $$(inp.dataset.filter + ' tbody tr');
    const none = $(inp.dataset.empty || '#none-' + inp.id);
    // STF-03: blood groups print a typographic minus (U+2212); a typed '-' and a pasted minus match either way
    const norm = t => t.toLowerCase().replace(/[\u2212\u2012\u2013\u2014\uFE63\uFF0D]/g, '-').replace(/\s+/g, ' ');
    const text = new Map(rows.map(r => [r, norm(r.textContent)]));
    inp.addEventListener('input', () => { const q = norm(inp.value).trim(); let n = 0; rows.forEach(r => { r.hidden = !!q && !text.get(r).includes(q); if (!r.hidden) n++; }); if (none) none.hidden = n > 0; });
  });
  // select all
  $$('[data-all]').forEach(master => master.addEventListener('change', () => $$(master.dataset.all).forEach(c => c.checked = master.checked)));

  // theme toggle
  // theme toggle (RSP-09): a toggle button, pressed = dark theme; the tooltip says what a press does
  const tb = $('#theme'), osDark = matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => (document.documentElement.dataset.theme || (osDark.matches ? 'dark' : 'light')) === 'dark';
  const showTheme = () => { if (!tb) return; const d = isDark(); tb.setAttribute('aria-pressed', String(d)); tb.title = d ? 'Switch to light theme' : 'Switch to dark theme'; };
  if (tb) {
    showTheme();
    tb.onclick = () => { const n = isDark() ? 'light' : 'dark'; document.documentElement.dataset.theme = n; try { localStorage.setItem('vytal-theme', n); } catch (_) {} showTheme(); };
    if (osDark.addEventListener) osDark.addEventListener('change', showTheme);
  }

  // request form: sensible default deadline per urgency (BR-10), sent with the browser's own offset
  const rf = $('#request-form');
  if (rf) {
    const urg = $('#urgency'), by = $('#required_by'), hid = $('#required_by_iso');
    const lead = { Emergency: 45, Urgent: 6 * 60, Routine: 3 * 24 * 60 };
    const pad = n => String(n).padStart(2, '0');
    const local = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const fill = () => { if (by.dataset.touched) return; by.value = local(new Date(Date.now() + lead[urg.value] * 60000)); sync(); };
    const sync = () => { const d = new Date(by.value); hid.value = isNaN(d) ? '' : d.toISOString(); };
    by.addEventListener('input', () => { by.dataset.touched = '1'; sync(); }); urg.addEventListener('change', () => { delete by.dataset.touched; fill(); });
    if (!by.value && by.dataset.iso) { const d = new Date(by.dataset.iso); if (!isNaN(d)) { by.value = local(d); by.dataset.touched = '1'; } }
    if (!by.value) fill(); else sync();
    $$('[data-pick-urgency]').forEach(b => b.onclick = () => { urg.value = b.dataset.pickUrgency; delete by.dataset.touched; fill(); $$('[data-pick-urgency]').forEach(x => x.setAttribute('aria-pressed', x === b)); });
  }

  // compatibility matrix toggles (admin rules): JSON call, animate, roll back on refusal
  $$('.cmx button[data-c]').forEach(b => b.addEventListener('click', async () => {
    const next = b.getAttribute('aria-pressed') !== 'true';
    b.setAttribute('aria-pressed', next);
    try {
      const r = await fetch((document.body.dataset.base || '') + '/admin/rules/compat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF': document.body.dataset.csrf },
        body: JSON.stringify({ component: b.dataset.c, recipient: b.dataset.r, donor: b.dataset.d, allowed: next }) });
      const j = await r.json(); if (!j.ok) throw new Error(j.message);
      b.classList.remove('flash'); void b.offsetWidth; b.classList.add('flash'); toast(`${b.dataset.d} can ${next ? 'now' : 'no longer'} give ${b.dataset.c} to ${b.dataset.r}.`, 'ok');
    } catch (err) { b.setAttribute('aria-pressed', !next); b.classList.add('bad'); setTimeout(() => b.classList.remove('bad'), 450); toast(err.message || 'Could not save that change.', 'err'); }
  }));

  // demo sign-in shortcuts on the login page
  const fillDemo = b => { const em = $('#email'), pw = $('#password'); if (!em || !pw) return; em.value = b.dataset.demo; pw.value = 'Vytal-demo-2026'; pw.focus(); };
  $$('[data-demo]').forEach(b => { b.onclick = () => fillDemo(b);
    if (b.tagName !== 'BUTTON') b.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fillDemo(b); } }); });

  // scrolling tables (RSP-04): mark when columns are hidden past the right edge, so the pinned action column casts a shadow
  $$('.tablewrap').forEach(w => {
    // RSP-03 safety net: a region that scrolls must take keyboard focus (templates normally set tabindex/role/aria-label)
    if (!w.hasAttribute('tabindex') && w.scrollWidth > w.clientWidth + 1) {
      w.tabIndex = 0;
      if (!w.getAttribute('role')) w.setAttribute('role', 'region');
      if (!w.hasAttribute('aria-label') && !w.hasAttribute('aria-labelledby')) {
        const card = w.closest('.card'), h = card && card.querySelector('h2,h3'), cap = w.querySelector('caption');
        w.setAttribute('aria-label', ((cap || h || {}).textContent || 'Table').trim() + ' (scrolls sideways)');
      }
    }
    const mark =() => w.classList.toggle('more-r', w.scrollLeft + w.clientWidth < w.scrollWidth - 1);
    mark(); w.addEventListener('scroll', mark, { passive: true }); addEventListener('resize', mark);
  });
})();

// tabs (component switchers): aria-selected, roving tabindex and arrow keys
document.querySelectorAll('[role=tablist]').forEach(list => {
  const tabs = [...list.querySelectorAll('[role=tab]')], card = list.closest('.card');
  const pick = (b, focus) => {
    tabs.forEach(x => { const on = x === b; x.setAttribute('aria-selected', String(on)); x.tabIndex = on ? 0 : -1; });
    card.querySelectorAll('[data-panel]').forEach(p => { const show = p.dataset.panel === b.dataset.tab; p.hidden = !show; if (show) { p.style.animation = 'none'; void p.offsetWidth; p.style.animation = 'rise .4s cubic-bezier(.22,.8,.24,1) both'; } });
    if (focus) b.focus();
  };
  tabs.forEach((b, i) => {
    b.addEventListener('click', () => pick(b));
    b.addEventListener('keydown', e => { const k = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (k) { e.preventDefault(); pick(tabs[(i + k + tabs.length) % tabs.length], true); } else if (e.key === 'Home') { e.preventDefault(); pick(tabs[0], true); } else if (e.key === 'End') { e.preventDefault(); pick(tabs[tabs.length - 1], true); } });
  });
});
