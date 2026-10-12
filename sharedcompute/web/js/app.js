// App shell: session, router, account menu, auth dialog and polling.
import { api } from './api.js';
import * as ui from './ui.js';
import { html } from './ui.js';
import marketplace from './views/marketplace.js';
import jobs from './views/jobs.js';
import provide from './views/provide.js';
import economics from './views/economics.js';
import status from './views/status.js';

const VIEWS = { marketplace, jobs, provide, economics, status };
const main = document.getElementById('main');

export const store = { meta: null, user: null, balances: null };

let active = null; // { view, ctx, timer, cleanup }

async function loadSession() {
  try {
    const me = await api.get('/me');
    store.user = me.user;
    store.balances = me.balances;
  } catch (err) {
    if (err.status !== 401) throw err;
    store.user = null;
    store.balances = null;
  }
  renderAccount();
}

export async function refreshBalances() {
  if (!store.user) return;
  try {
    store.balances = (await api.get('/me')).balances;
    renderAccount();
  } catch (err) {
    if (err.status === 401) { store.user = null; renderAccount(); route(); }
  }
}

function initials(name) {
  return name.split(/\s+/).map((p) => p[0]).join('').slice(0, 2).toUpperCase();
}

function renderAccount() {
  const el = document.getElementById('account');
  if (!store.user) {
    el.innerHTML = String(html`
      <button class="btn btn-ghost btn-sm" data-auth="login">Sign in</button>
      <button class="btn btn-primary btn-sm" data-auth="register">Create account</button>`);
    return;
  }
  const b = store.balances || { available_cents: 0 };
  el.innerHTML = String(html`
    <a class="balance-chip num" href="#/economics" title="Simulated credits available to spend">${ui.usd(b.available_cents)} <small>credits</small></a>
    <div class="menu">
      <button class="avatar" aria-haspopup="true" aria-expanded="false" data-menu>${initials(store.user.display_name)}</button>
      <div class="menu-pop hidden" role="menu">
        <div class="who"><b>${store.user.display_name}</b><span>${store.user.email}${store.user.is_operator ? ' · operator' : ''}</span></div>
        <button role="menuitem" data-logout>Sign out</button>
      </div>
    </div>`);
}

function authDialog(mode) {
  const register = mode === 'register';
  ui.openDialog(html`
    <div class="dialog-body">
      <h2>${register ? 'Create your account' : 'Sign in'}</h2>
      <p class="lead">${register ? 'One account works for renting compute and for providing it.' : 'Welcome back.'}</p>
      <form class="form" id="auth-form" novalidate>
        ${register ? html`<div class="field"><label for="a-name">Name</label><input class="input" id="a-name" name="display_name" autocomplete="name" required minlength="2" maxlength="60"></div>` : ''}
        <div class="field"><label for="a-email">Email</label><input class="input" id="a-email" name="email" type="email" autocomplete="email" required></div>
        <div class="field"><label for="a-pass">Password</label><input class="input" id="a-pass" name="password" type="password" autocomplete="${register ? 'new-password' : 'current-password'}" required minlength="${register ? 10 : 1}">
          ${register ? html`<span class="hint">At least 10 characters.</span>` : ''}</div>
        <div class="form-error hidden" role="alert"></div>
        <button class="btn btn-primary" type="submit">${register ? 'Create account' : 'Sign in'}</button>
      </form>
      <div class="dialog-foot">
        <span class="faint">${register ? 'Already have an account?' : 'New to SharedCompute?'}</span>
        <button class="link-btn" data-auth="${register ? 'login' : 'register'}">${register ? 'Sign in' : 'Create an account'}</button>
      </div>
    </div>`, (dialog) => {
    dialog.querySelector('input').focus();
    dialog.querySelector('#auth-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector('button[type=submit]');
      button.disabled = true;
      try {
        await api.post(register ? '/auth/register' : '/auth/login', ui.formData(form));
        ui.closeDialog();
        await loadSession();
        ui.toast(register ? `Welcome, ${store.user.display_name}.` : 'Signed in.');
        route();
      } catch (err) {
        ui.showFormError(form, err);
      } finally {
        button.disabled = false;
      }
    });
  });
}

export function requireAuth(ctx, title, body) {
  if (store.user) return true;
  ctx.root.innerHTML = String(html`
    <div class="gate">
      <h1>${title}</h1>
      <p>${body}</p>
      <div class="btn-row" data-center>
        <button class="btn btn-primary" data-auth="register">Create account</button>
        <button class="btn" data-auth="login">Sign in</button>
      </div>
    </div>`);
  return false;
}

function parseHash() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  return { name: VIEWS[path] ? path : 'marketplace', params: new URLSearchParams(query || '') };
}

function stopActive() {
  if (!active) return;
  clearInterval(active.timer);
  if (typeof active.cleanup === 'function') active.cleanup();
  active = null;
}

export function route() {
  stopActive();
  const { name, params } = parseHash();
  const view = VIEWS[name];
  document.querySelectorAll('[data-nav]').forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  document.title = `${view.title} · SharedCompute`;
  main.innerHTML = '';
  main.__html = undefined;
  const ctx = { root: main, params, store, requireAuth, refreshBalances, navigate };
  active = { view, ctx, cleanup: null, timer: null };
  const cleanup = view.mount(ctx);
  if (active) active.cleanup = cleanup;
  if (view.poll && view.refresh) {
    active.timer = setInterval(() => {
      if (!document.hidden && active?.view === view) view.refresh(ctx);
    }, view.poll);
  }
}

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

document.addEventListener('click', async (event) => {
  const auth = event.target.closest('[data-auth]');
  if (auth) { event.preventDefault(); authDialog(auth.dataset.auth); return; }
  const menuBtn = event.target.closest('[data-menu]');
  const pop = document.querySelector('.menu-pop');
  if (menuBtn && pop) {
    const open = pop.classList.toggle('hidden') === false;
    menuBtn.setAttribute('aria-expanded', String(open));
    return;
  }
  if (pop && !event.target.closest('.menu')) pop.classList.add('hidden');
  if (event.target.closest('[data-logout]')) {
    await api.post('/auth/logout').catch(() => {});
    store.user = null;
    store.balances = null;
    renderAccount();
    ui.toast('Signed out.');
    route();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') document.querySelector('.menu-pop')?.classList.add('hidden');
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && active?.view.refresh) active.view.refresh(active.ctx);
});

window.addEventListener('hashchange', route);

async function boot() {
  try {
    store.meta = await api.get('/meta');
    document.getElementById('version').textContent = `v${store.meta.version}`;
    await loadSession();
  } catch (err) {
    main.innerHTML = String(html`<div class="gate"><h1>Server unavailable</h1><p>${err.message}</p></div>`);
    return;
  }
  route();
}

boot();
