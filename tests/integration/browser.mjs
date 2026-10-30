// Synthetic frontend smoke. No screenshots, traces, credentials or payloads are logged.
// Selectors come from the pinned 3.11.0 frontend; credentials enter only through a private file.
import assert from 'node:assert/strict';
import { readFile, lstat } from 'node:fs/promises';
import { resolve, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

let stage = 'arguments';
let browser;
try {
  const args = process.argv.slice(2);
  assert.equal(args.length, 4);
  const options = Object.fromEntries([[args[0], args[1]], [args[2], args[3]]]);
  assert.deepEqual(Object.keys(options).sort(), ['--base-url', '--state-file']);
  const base = new URL(options['--base-url']);
  assert(['http:', 'https:'].includes(base.protocol));
  assert.equal(base.pathname, '/');
  assert(!base.username && !base.password && !base.search && !base.hash);
  // Credentials must stay outside the repository in a regular owner-only file.
  const statePath = resolve(options['--state-file']);
  const repository = resolve(fileURLToPath(new URL('../..', import.meta.url)));
  const pathWithinRepo = relative(repository, statePath);
  assert(pathWithinRepo.startsWith('..') || isAbsolute(pathWithinRepo));
  const info = await lstat(statePath);
  assert(info.isFile() && (info.mode & 0o077) === 0);
  const state = JSON.parse(await readFile(statePath, 'utf8'));
  assert(typeof state.password === 'string' && state.password.length > 0);
  assert(typeof state.profileId === 'string' && state.profileId.length > 0);
  assert(typeof state.accountName === 'string' && state.accountName.startsWith('Chart synthetic '));
  assert(typeof state.accountId === 'string' && state.accountId.length > 0);
  assert(typeof state.vaultSha256 === 'string' && /^[a-f0-9]{64}$/.test(state.vaultSha256));

  stage = 'browser-launch';
  const browserArgs = ['--no-proxy-server'];
  if (state.tlsSpkiSha256) {
    // Exact process-local certificate key trust; never a blanket TLS-error bypass.
    // ingress.py separately verifies this certificate chain and hostname with its private CA.
    assert(base.protocol === 'https:' && state.tlsHostname === base.hostname);
    assert(/^[A-Za-z0-9+/]{43}=$/.test(state.tlsSpkiSha256));
    assert(/^[a-z0-9.-]+\.example\.test$/.test(state.tlsHostname));
    browserArgs.push(`--ignore-certificate-errors-spki-list=${state.tlsSpkiSha256}`);
    browserArgs.push(`--host-resolver-rules=MAP ${state.tlsHostname} 127.0.0.1`);
  }
  browser = await chromium.launch({ headless: true, args: browserArgs });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'en-US' });
  const page = await context.newPage();
  page.setDefaultTimeout(45000);
  const successfulAssets = new Set();
  let frontendScope;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/v1/accounts') {
      frontendScope = request.headers()['x-wf-profile-scope'] || frontendScope;
    }
  });
  page.on('response', (response) => {
    if (new URL(response.url()).origin === base.origin && response.ok()
        && ['script', 'stylesheet'].includes(response.request().resourceType())) {
      successfulAssets.add(response.request().resourceType());
    }
  });
  // The test requires no provider/update/telemetry services. Do not permit external requests.
  await context.route('**/*', (route) => {
    if (new URL(route.request().url()).origin === base.origin) return route.continue();
    return route.abort('blockedbyclient');
  });

  stage = 'frontend-login';
  const document = await page.goto(base.href, { waitUntil: 'domcontentloaded' });
  assert(document?.ok());
  await page.getByTestId('login-password-input').fill(state.password);
  const login = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/auth/login'
    && response.request().method() === 'POST');
  await page.getByTestId('login-submit-button').click();
  assert.equal((await login).status(), 200);
  await page.getByTestId('login-password-input').waitFor({ state: 'hidden' });
  const cookie = (await context.cookies()).find((item) => item.name === 'wf_session');
  assert(cookie?.httpOnly && cookie.sameSite === 'Lax');
  if (base.protocol === 'https:') assert(cookie.secure);

  stage = 'profile-selection';
  const metadata = await page.evaluate(async () => {
    const response = await fetch('/api/v1/profiles/get_profile_state', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    return { status: response.status, body: await response.json() };
  });
  assert.equal(metadata.status, 200);
  const profiles = metadata.body.profiles;
  const target = profiles.find((profile) => profile.id === state.profileId);
  assert(target && !target.lockEnabled);
  await page.goto(new URL('/settings/accounts', base).href, { waitUntil: 'domcontentloaded' });
  // A fresh browser can show the profile chooser; choose by the authenticated registry name.
  const chooser = page.locator('.profile-selection-list');
  await Promise.race([
    chooser.waitFor({ state: 'visible' }),
    page.getByText(state.accountName, { exact: true }).first().waitFor({ state: 'visible' }),
    page.locator('nav').first().waitFor({ state: 'visible' }),
  ]);
  if (await chooser.isVisible()) {
    await chooser.getByRole('button', { name: target.name, exact: true }).click();
  } else if (!(await page.getByText(state.accountName, { exact: true }).first().isVisible())) {
    // The app may recover another profile's session; explicitly switch using its real UI.
    await Promise.any(profiles.map((profile) => page.getByTitle(profile.name, { exact: true }).waitFor({ state: 'visible' })));
    const profileButtons = page.locator('button[title]');
    let opened = false;
    for (const profile of profiles) {
      const button = profileButtons.and(page.getByTitle(profile.name, { exact: true }));
      if (await button.isVisible()) {
        await button.click();
        opened = true;
        break;
      }
    }
    assert(opened);
    await page.getByRole('menuitem', { name: /switch profile/i }).click();
    await chooser.getByRole('button', { name: target.name, exact: true }).click();
  }

  stage = 'seeded-account-visible';
  await page.getByText(state.accountName, { exact: true }).first().waitFor({ state: 'visible' });
  assert(successfulAssets.has('script'));
  assert(successfulAssets.has('stylesheet'));
  assert.equal(await page.getByTestId('login-password-input').count(), 0);

  stage = 'browser-profile-vault-readback';
  assert(frontendScope);
  // Use the exact grant installed by the real frontend, never create a substitute session.
  // Return booleans only; registry/account/vault payloads remain in this browser process.
  const proof = await page.evaluate(async ({ scope, profileId, accountId, accountName, vaultSha256 }) => {
    const headers = { 'x-wf-profile-scope': scope, 'Content-Type': 'application/json' };
    const registry = await fetch('/api/v1/profiles/get_profile_state', { method: 'POST', headers, body: '{}' });
    const accounts = await fetch('/api/v1/accounts', { headers });
    const vault = await fetch('/api/v1/secrets?secretKey=chart-test-marker', { headers });
    if (![registry, accounts, vault].every((response) => response.ok)) return false;
    const current = await registry.json();
    const items = await accounts.json();
    const marker = await vault.json();
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(marker))))
      .map((byte) => byte.toString(16).padStart(2, '0')).join('');
    return current.session?.profileId === profileId
      && items.some((item) => item.id === accountId && item.name === accountName && item.currency === 'USD')
      && digest === vaultSha256;
  }, { scope: frontendScope, profileId: state.profileId, accountId: state.accountId,
    accountName: state.accountName, vaultSha256: state.vaultSha256 });
  assert(proof);

  stage = 'frontend-logout';
  const logout = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/auth/logout'
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: /log\s*out|sign\s*out/i }).click();
  assert((await logout).ok());
  await page.getByTestId('login-password-input').waitFor({ state: 'visible' });
  const deniedStatus = await page.evaluate(async () => (await fetch('/api/v1/accounts')).status);
  assert.equal(deniedStatus, 401);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByTestId('login-password-input').waitFor({ state: 'visible' });
  assert.equal(await page.getByText(state.accountName, { exact: true }).count(), 0);
  console.log(JSON.stringify({ test: 'T-BROWSER', status: 'PASS', assertions: [
    'frontend scripts/styles loaded', 'form login established HttpOnly session',
    'selected seeded profile and displayed synthetic account', 'frontend scope matched account ID and saved vault digest',
    'UI logout protected API and reload',
  ] }));
} catch {
  // Playwright errors may include private values/selectors or response details: withhold them.
  console.error(JSON.stringify({ test: 'T-BROWSER', status: 'FAIL', stage, diagnostic: 'Details withheld to protect the private fixture.' }));
  process.exitCode = 1;
} finally {
  await browser?.close();
}
