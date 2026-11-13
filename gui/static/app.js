/* Vytal GUI behaviour: motion, toasts, confirmations, live filters. Everything degrades to plain forms and links without JS. */
(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // stagger the page entrance
  $$('.page > *').forEach((el, i) => el.style.setProperty('--i', Math.min(i, 8)));
  $$('tbody tr').forEach((tr, i) => { tr.classList.add('reveal-row'); tr.style.setProperty('--r', Math.min(i, 14)); });

  // animated numbers
  const count = el => {
    const end = parseFloat(el.dataset.count), dec = (el.dataset.count.split('.')[1] || '').length;
    if (reduce || isNaN(end)) { el.textContent = end.toFixed(dec); return; }
    const t0 = performance.now(), dur = 900;
    const tick = t => { const p = Math.min((t - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3); el.textContent = (end * e).toFixed(dec); if (p < 1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  };
  // bars, rings and cells grow once they are on screen
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (!e.isIntersecting) return; io.unobserve(e.target); const el = e.target;
    if (el.dataset.count) count(el);
    if (el.dataset.w) el.style.setProperty('--w', el.dataset.w + '%');
    if (el.classList.contains('ring')) el.querySelector('.fg').style.strokeDashoffset = 326.7 * (1 - el.dataset.p / 100);
  }), { threshold: .2 });
  $$('[data-count],[data-w],.ring').forEach(el => { if (el.dataset.w) el.style.setProperty('--w', '0%'); io.observe(el); });

  // times are stored in UTC; show them in the viewer's time zone
  $$('time[datetime]').forEach(t => {
    const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(t.dateTime); const [yy, mm, dd] = dateOnly ? t.dateTime.split('-').map(Number) : [];
    const d = dateOnly ? new Date(yy, mm - 1, dd) : new Date(t.dateTime); if (isNaN(d)) return;
    const o = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
    if (t.dataset.rel) { const m = Math.round((d - Date.now()) / 60000), a = Math.abs(m); t.title = d.toLocaleString([], o);
      t.textContent = a < 60 ? `${m < 0 ? '' : 'in '}${a} min${m < 0 ? ' ago' : ''}` : a < 2880 ? `${m < 0 ? '' : 'in '}${Math.round(a / 60)} h${m < 0 ? ' ago' : ''}` : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    } else t.textContent = d.toLocaleString([], t.dataset.date ? { month: 'short', day: 'numeric', year: 'numeric' } : o);
  });

  // sticky header shadow
  const top = $('.top'); if (top) addEventListener('scroll', () => top.classList.toggle('scrolled', scrollY > 6), { passive: true });
  const side = $('.side'); if (side && innerWidth <= 1000) document.documentElement.style.setProperty('--sideh', side.offsetHeight + 'px');

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
  $$('.toast').forEach((t, i) => { const ttl = t.classList.contains('err') ? 9000 : 6000; t.style.setProperty('--ttl', ttl + 'ms'); t.style.animationDelay = i * 120 + 'ms';
    t.querySelector('button').onclick = () => closeToast(t); setTimeout(() => closeToast(t), ttl + i * 120); });
  window.toast = (msg, kind = 'info') => { const t = document.createElement('div'); t.className = 'toast ' + kind; t.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    t.innerHTML = '<div></div><button aria-label="Dismiss">&times;</button>'; t.firstChild.textContent = msg; t.querySelector('button').onclick = () => closeToast(t); box.append(t); setTimeout(() => closeToast(t), 5000); };

  // confirmation dialog for destructive actions (US-07)
  const dlg = $('#confirm');
  if (dlg) {
    let pending = null;
    document.addEventListener('submit', e => {
      const f = e.target, btn = e.submitter, msg = (btn && btn.dataset.confirm) || f.dataset.confirm;
      if (!msg || f.dataset.ok) return;
      e.preventDefault(); pending = { f, btn };
      $('#confirm-title').textContent = (btn && btn.dataset.title) || f.dataset.title || 'Please confirm';
      $('#confirm-text').textContent = msg; $('#confirm-go').textContent = (btn && btn.dataset.go) || f.dataset.go || 'Confirm';
      dlg.showModal(); $('#confirm-go').focus();
    }, true);
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
    inp.addEventListener('input', () => { const q = inp.value.toLowerCase(); rows.forEach(r => r.hidden = q && !r.textContent.toLowerCase().includes(q)); });
  });
  // select all
  $$('[data-all]').forEach(master => master.addEventListener('change', () => $$(master.dataset.all).forEach(c => c.checked = master.checked)));

  // theme toggle
  const tb = $('#theme');
  if (tb) tb.onclick = () => { const dark = (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
    const n = dark ? 'light' : 'dark'; document.documentElement.dataset.theme = n; try { localStorage.setItem('vytal-theme', n); } catch (_) {} };

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
  $$('[data-demo]').forEach(b => b.onclick = () => { $('#email').value = b.dataset.demo; $('#password').value = 'Vytal-demo-2026'; $('#password').focus(); });
})();

// tabs (component switchers)
document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => {
  const card = b.closest('.card');
  card.querySelectorAll('[data-tab]').forEach(x => x.setAttribute('aria-pressed', x === b));
  card.querySelectorAll('[data-panel]').forEach(p => { const show = p.dataset.panel === b.dataset.tab; p.hidden = !show; if (show) { p.style.animation = 'none'; void p.offsetWidth; p.style.animation = 'rise .4s cubic-bezier(.22,.8,.24,1) both'; } });
}));
