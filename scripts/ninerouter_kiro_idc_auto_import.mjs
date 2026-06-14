import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

const moduleRoot = process.env.PLAYWRIGHT_NODE_MODULES || process.env.PW_NODE_MODULES;
const requireFromRoot = moduleRoot ? createRequire(path.join(moduleRoot, 'noop.js')) : createRequire(import.meta.url);
const { chromium } = requireFromRoot('playwright-core');

const _defaultDb = path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), '9router', 'db', 'data.sqlite');
const DB_PATH = process.env.NINEROUTER_DB || _defaultDb;
const BASE_URL = process.env.NINEROUTER_BASE_URL || 'http://127.0.0.1:20128';
const CHROME_PATH = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PYTHON = process.env.PYTHON || 'python';
const DB_HELPER = process.env.KIRO_DB_HELPER || '';
const ROOT = process.env.AUTOREG_ROOT || process.cwd();
const TIMEOUT_MS = Number(process.env.KIRO_LOGIN_TIMEOUT_MS || 900000);
const REDIRECT_URI = process.env.KIRO_REDIRECT_URI || 'http://127.0.0.1/oauth/callback';
const LOG_PREFIX = '[kiro-auto]';

const scopes = [
  'codewhisperer:completions',
  'codewhisperer:analysis',
  'codewhisperer:conversations',
  'codewhisperer:transformations',
  'codewhisperer:taskassist',
];

function log(message, data = undefined) {
  if (data === undefined) {
    console.error(`${LOG_PREFIX} ${message}`);
    return;
  }
  console.error(`${LOG_PREFIX} ${message} ${JSON.stringify(data)}`);
}

// --- TOTP (RFC 4648 base32 + RFC 6238), no external deps ---
function base32Decode(input) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  const clean = String(input).replace(/\s+/g, '').replace(/=+$/, '').toUpperCase();
  for (const ch of clean) {
    const idx = alphabet.indexOf(ch);
    if (idx < 0) continue;
    bits += idx.toString(2).padStart(5, '0');
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

function totpCode(secret, forTime = Date.now()) {
  const key = base32Decode(secret);
  let counter = Math.floor(forTime / 1000 / 30);
  const buf = Buffer.alloc(8);
  for (let i = 7; i >= 0; i--) { buf[i] = counter & 0xff; counter = Math.floor(counter / 256); }
  const hmac = crypto.createHmac('sha1', key).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const bin = ((hmac[offset] & 0x7f) << 24) | ((hmac[offset + 1] & 0xff) << 16) | ((hmac[offset + 2] & 0xff) << 8) | (hmac[offset + 3] & 0xff);
  return (bin % 1000000).toString().padStart(6, '0');
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

async function postJson(url, payload, headers = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...headers },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${text.slice(0, 500)}`);
  }
  return text.trim() ? JSON.parse(text) : {};
}

async function getJson(url, headers = {}) {
  const response = await fetch(url, { headers: { Accept: 'application/json', ...headers } });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${text.slice(0, 500)}`);
  }
  return text.trim() ? JSON.parse(text) : {};
}

function startCallbackServer(expectedState, redirectUri) {
  const parsedRedirect = new URL(redirectUri);
  const listenPort = Number(parsedRedirect.port || (parsedRedirect.protocol === 'https:' ? 443 : 80));
  const callbackPath = parsedRedirect.pathname || '/oauth/callback';
  let resolveCode;
  let rejectCode;
  const codePromise = new Promise((resolve, reject) => {
    resolveCode = resolve;
    rejectCode = reject;
  });
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, `http://${request.headers.host}`);
    if (url.pathname !== callbackPath) {
      response.writeHead(404);
      response.end('not found');
      return;
    }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end('<html><body><h3>Kiro OAuth captured.</h3><p>You can close this tab.</p></body></html>');
    const error = url.searchParams.get('error');
    const state = url.searchParams.get('state');
    const code = url.searchParams.get('code');
    if (error) rejectCode(new Error(`OAuth error: ${error}`));
    else if (state !== expectedState) rejectCode(new Error('OAuth state mismatch'));
    else if (!code) rejectCode(new Error('OAuth callback missing code'));
    else resolveCode(code);
    setTimeout(() => server.close(), 100);
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(listenPort, parsedRedirect.hostname, () => resolve({ server, codePromise, port: server.address().port }));
  });
}

async function stablePageInfo(page) {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      await page.waitForLoadState('domcontentloaded', { timeout: 1500 }).catch(() => {});
      return await page.evaluate(() => ({
        url: location.href,
        title: document.title,
        text: document.body?.innerText?.slice(0, 1800) || '',
        inputs: Array.from(document.querySelectorAll('input')).map((el, i) => ({
          i,
          type: el.type,
          name: el.getAttribute('name'),
          id: el.id,
          placeholder: el.getAttribute('placeholder'),
          autocomplete: el.getAttribute('autocomplete'),
          aria: el.getAttribute('aria-label'),
          visible: Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
        })).slice(0, 30),
        buttons: Array.from(document.querySelectorAll('button,input[type=submit],a')).map((el, i) => ({
          i,
          tag: el.tagName,
          type: el.getAttribute('type'),
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 100),
          id: el.id,
          name: el.getAttribute('name'),
          visible: Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
        })).slice(0, 50),
      }));
    } catch {
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }
  return { url: page.url(), title: await page.title().catch(() => ''), text: '', inputs: [], buttons: [] };
}

async function allFrames(page) {
  return [page, ...page.frames()];
}

async function fillFirstVisible(page, selectors, value) {
  for (const frame of await allFrames(page)) {
    for (const selector of selectors) {
      try {
        const locator = frame.locator(selector).first();
        if (await locator.count() && await locator.isVisible({ timeout: 700 })) {
          await locator.click({ timeout: 2000 });
          await locator.fill(value, { timeout: 5000 });
          return selector;
        }
      } catch {}
    }
  }
  return null;
}

async function clickByText(page, labels) {
  for (const frame of await allFrames(page)) {
    for (const label of labels) {
      const pattern = new RegExp(label, 'i');
      const candidates = [
        frame.getByRole('button', { name: pattern }).first(),
        frame.getByRole('link', { name: pattern }).first(),
        frame.getByText(pattern).first(),
      ];
      for (const candidate of candidates) {
        try {
          if (await candidate.count() && await candidate.isVisible({ timeout: 500 })) {
            await Promise.allSettled([
              page.waitForLoadState('domcontentloaded', { timeout: 7000 }),
              candidate.click({ timeout: 4000 }),
            ]);
            return true;
          }
        } catch {}
      }
    }
  }
  return false;
}

async function submit(page) {
  if (await clickByText(page, ['Đặt mật khẩu mới', 'Set new password', 'Change password', 'Next', 'Continue', 'Sign in', 'Sign In', 'Log in', 'Login', 'Submit', 'Allow', 'Authorize', 'Autoriser', 'Approve', 'Accept', 'Confirm', 'Yes'])) {
    return true;
  }
  try {
    await Promise.allSettled([
      page.waitForLoadState('domcontentloaded', { timeout: 7000 }),
      page.keyboard.press('Enter'),
    ]);
    return true;
  } catch {
    return false;
  }
}

async function fillNewPassword(page, newPassword) {
  const selectors = [
    'input[type="password"]',
    'input[name*="password" i]',
    'input[id*="password" i]',
  ];
  let filled = 0;
  for (const frame of await allFrames(page)) {
    for (const selector of selectors) {
      const locators = await frame.locator(selector).all().catch(() => []);
      for (const locator of locators) {
        try {
          if (await locator.isVisible({ timeout: 300 })) {
            await locator.click({ timeout: 1000 });
            await locator.fill(newPassword, { timeout: 3000 });
            filled += 1;
          }
        } catch {}
      }
      if (filled >= 2) return filled;
    }
  }
  return filled;
}

async function enterMfaCode(page, account) {
  // Account already has a known MFA device: just generate + type the current TOTP code.
  const secret = account.mfaSecret;
  if (!secret) return false;
  async function typeCode(code) {
    const sels = ['#awsui-input-0', 'input[autocomplete="one-time-code"]', 'input[name*="code" i]', 'input[type="text"]:not([readonly])'];
    for (const fr of await allFrames(page)) {
      for (const s of sels) {
        try {
          const loc = fr.locator(s).first();
          if (await loc.count() && await loc.isVisible({ timeout: 600 })) {
            await loc.click({ timeout: 1500 });
            await loc.fill('', { timeout: 1000 }).catch(() => {});
            await loc.pressSequentially(code, { delay: 90, timeout: 6000 });
            return true;
          }
        } catch {}
      }
    }
    return false;
  }
  for (let attempt = 0; attempt < 4; attempt++) {
    const into = Math.floor((Date.now() / 1000) % 30);
    if (into >= 26) { await page.waitForTimeout((31 - into) * 1000); }
    const code = totpCode(secret, Date.now() + 800);
    if (!(await typeCode(code))) { await page.waitForTimeout(1200); continue; }
    log(`${account.name}: existing-MFA code typed (attempt ${attempt + 1})`);
    await clickByText(page, ['Đăng nhập', 'Sign in', 'Xác nhận', 'Confirm', 'Tiếp theo', 'Next', 'Submit']);
    await page.waitForTimeout(3500);
    const info = await stablePageInfo(page);
    const t = (info.text || '').toLowerCase();
    if (!t.includes('xác minh bổ sung') && !t.includes('mã gồm sáu') && !t.includes('mã mfa') && !t.includes('không hợp lệ') && !t.includes('invalid') && !t.includes('incorrect')) {
      log(`${account.name}: existing-MFA code accepted`);
      return true;
    }
    log(`${account.name}: code not accepted yet (attempt ${attempt + 1})`);
    await page.waitForTimeout(1200);
  }
  return false;
}

async function handleMfaSetup(page, account) {
  // First-time AWS IdC MFA registration: register a virtual authenticator (TOTP).
  // State-machine: each pass detect chooser vs setup page, recover if AWS bounces back.
  log(`${account.name}: MFA setup detected, registering virtual authenticator`);

  async function onChooser() {
    return (page.url().includes('/mfa/register')) && (await page.locator('input[type=radio]').count()) > 0;
  }
  async function selectAuthenticatorRadio() {
    const radios = page.locator('input[type=radio]');
    if (await radios.count()) {
      await radios.first().check({ timeout: 3000 }).catch(async () => { await radios.first().click({ timeout: 3000 }).catch(() => {}); });
      await page.waitForTimeout(700);
      await clickByText(page, ['Tiếp theo', 'Next', 'Continue', 'Tiếp tục']);
      await page.waitForTimeout(3000);
    }
  }
  async function revealAndReadSecret() {
    await clickByText(page, ['Hiện khóa bí mật', 'hiển thị mã khóa bí mật', 'show secret key', 'Show secret key', 'Hiển thị mã khóa', 'mã khóa bí mật', 'secret key']);
    await page.waitForTimeout(1200);
    for (let i = 0; i < 5; i++) {
      try {
        const s = await page.evaluate(() => {
          const nodes = [...document.querySelectorAll('*')].map(el => el.childElementCount === 0 ? (el.innerText || '').trim() : '').filter(Boolean);
          for (const t of nodes) { const c = t.replace(/\s+/g, ''); if (/^[A-Z2-7]{16,64}$/.test(c)) return c; }
          return null;
        });
        if (s) return s;
      } catch {}
      await page.waitForTimeout(1000);
    }
    return null;
  }
  // Realistic typing into the 6-digit field so React controlled-input fires onChange.
  async function typeCode(code) {
    const sels = ['#awsui-input-0', 'input[autocomplete="one-time-code"]', 'input[name*="code" i]', 'input[type="text"]:not([readonly])'];
    for (const fr of await allFrames(page)) {
      for (const s of sels) {
        try {
          const loc = fr.locator(s).first();
          if (await loc.count() && await loc.isVisible({ timeout: 600 })) {
            await loc.click({ timeout: 1500 });
            await loc.fill('', { timeout: 1500 }).catch(() => {});
            await loc.pressSequentially(code, { delay: 90, timeout: 6000 });
            await page.waitForTimeout(400);
            return true;
          }
        } catch {}
      }
    }
    return false;
  }

  async function onSetupPage() {
    // The TOTP setup page shows the code-entry field + "Hiện/Ẩn khóa bí mật".
    try {
      const t = (await page.evaluate(() => (document.body?.innerText || ''))).toLowerCase();
      return t.includes('thiết lập ứng dụng xác thực') || t.includes('mã từ ứng dụng') || t.includes('mã gồm sáu');
    } catch { return false; }
  }
  function saveSecret(s) {
    if (!s || s === account.mfaSecret) return;
    account.mfaSecret = s; // surface immediately so caller persists even on later failure
    try {
      const dumpPath = path.join(os.tmpdir(), `kiro-mfa-secrets.log`);
      fs.appendFileSync(dumpPath, `${new Date().toISOString()}\t${account.name}\t${account.startUrl || ''}\t${s}\n`);
      log(`${account.name}: MFA secret captured (${s.length} chars) -> saved to ${dumpPath}`);
    } catch (e) { log(`${account.name}: MFA secret captured (${s.length} chars) [dump failed: ${e.message}]`); }
  }

  for (let attempt = 0; attempt < 6; attempt++) {
    // 1. Recover position: if bounced to chooser, re-pick authenticator -> AWS issues a FRESH secret.
    if (await onChooser()) { await selectAuthenticatorRadio(); }
    // 2. On the setup page, always re-read the secret (it changes after every fresh registration).
    if (await onSetupPage()) {
      const fresh = await revealAndReadSecret();
      if (fresh) saveSecret(fresh);
    } else {
      // Not on a known MFA page; give it a moment to settle.
      await page.waitForTimeout(1200);
    }
    const secret = account.mfaSecret;
    if (!secret) { log(`${account.name}: secret not found, retrying`); await page.waitForTimeout(1500); continue; }

    // 3. Avoid the 30s window boundary: if we're in the last 4s, wait it out so the code stays valid.
    const into = Math.floor((Date.now() / 1000) % 30);
    if (into >= 26) { await page.waitForTimeout((31 - into) * 1000); }

    // 4. Generate the code for "now" and type it the realistic way.
    const code = totpCode(secret, Date.now() + 800);
    if (!(await typeCode(code))) { log(`${account.name}: code field not found (attempt ${attempt + 1})`); await page.waitForTimeout(1500); continue; }
    log(`${account.name}: TOTP typed (attempt ${attempt + 1})`);
    await clickByText(page, ['Chỉ định MFA', 'Assign MFA', 'Gán MFA', 'Xác nhận', 'Confirm', 'Tiếp theo', 'Next', 'Submit']);
    await page.waitForTimeout(3800);

    // 5. Evaluate result.
    const info = await stablePageInfo(page);
    const txt = (info.text || '').toLowerCase();
    const url = info.url || '';
    if (!url.includes('/mfa/register') && !txt.includes('mã từ ứng dụng') && !txt.includes('thiết lập ứng dụng xác thực') && !txt.includes('đăng ký thiết bị mfa')) {
      log(`${account.name}: MFA assigned successfully`);
      return { ok: true, secret };
    }
    log(`${account.name}: not assigned yet (attempt ${attempt + 1}); url=${url.split('?')[0]}`);
    await page.waitForTimeout(1200); // short pause; next loop re-reads state and uses a fresh code
  }
  return { ok: false, secret: account.mfaSecret, reason: 'totp_not_accepted' };
}

async function automateLogin(page, account, authorizeUrl, codePromise) {
  await page.goto(authorizeUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  let filledUser = false;
  let filledPass = false;
  let passwordValue = account.password;
  let triedNewPasswordFallback = false;
  let lastInfo = await stablePageInfo(page);
  const deadline = Date.now() + TIMEOUT_MS;

  while (Date.now() < deadline) {
    const captured = await Promise.race([
      codePromise.then(() => true).catch(() => false),
      new Promise(resolve => setTimeout(() => resolve(false), 250)),
    ]);
    if (captured) return { ok: true, phase: 'callback' };

    lastInfo = await stablePageInfo(page);
    const text = (lastInfo.text || '').toLowerCase();
    const isSetupScreen = text.includes('thiết lập ứng dụng xác thực') || text.includes('mã khóa bí mật') || text.includes('đăng ký thiết bị mfa') || text.includes('register mfa') || text.includes('set up') || (text.includes('authenticator') && text.includes('secret'));
    const isCodePrompt = text.includes('xác minh bổ sung') || text.includes('mã gồm sáu') || text.includes('mã mfa') || text.includes('verification code') || text.includes('enter code') || text.includes('one-time') || (text.includes('mfa') && !isSetupScreen);

    // Case A: account already has a known MFA secret and AWS only asks for a code -> just enter it.
    if (account.mfaSecret && isCodePrompt && !isSetupScreen) {
      const ok = await enterMfaCode(page, account);
      if (ok) { await page.waitForTimeout(1500); continue; }
      return { ok: false, phase: 'mfa', info: lastInfo, mfaReason: 'code_not_accepted', mfaSecret: account.mfaSecret };
    }

    // Case B: first-time MFA registration: auto-register a virtual authenticator (TOTP).
    if (isSetupScreen || text.includes('multi-factor') || text.includes('authenticator') || text.includes('verify your identity') || text.includes('xác thực')) {
      const mfa = await handleMfaSetup(page, account);
      if (mfa.ok) {
        account.mfaSecret = mfa.secret; // surface secret so it can be persisted
        await page.waitForTimeout(1500);
        continue; // resume loop: expect OAuth approval / callback next
      }
      return { ok: false, phase: 'mfa', info: lastInfo, mfaReason: mfa.reason, mfaSecret: mfa.secret || null };
    }
    if (text.includes('incorrect') || text.includes('invalid username') || text.includes('invalid password') || text.includes('authentication failed') || text.includes('wrong password') || text.includes('không thể xác minh') || text.includes('khong the xac minh') || text.includes('thông tin chứng thực') || text.includes('could not verify') || text.includes('unable to verify')) {
      if (account.newPassword && !triedNewPasswordFallback && passwordValue !== account.newPassword) {
        triedNewPasswordFallback = true;
        passwordValue = account.newPassword;
        filledPass = false;
        await page.goto(authorizeUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
        await page.waitForTimeout(1000);
        continue;
      }
      return { ok: false, phase: 'login_error', info: lastInfo };
    }

    if (text.includes('đặt mật khẩu mới') || text.includes('dat mat khau moi') || text.includes('new password') || text.includes('change password') || text.includes('set password') || text.includes('cập nhật mật khẩu')) {
      // AWS forces a first-login password reset. Use provided newPassword or generate one.
      if (!account.newPassword) {
        // Generate a strong password that satisfies AWS IdC policy (upper/lower/digit/symbol, len>=16).
        const pick = (set, n) => Array.from({ length: n }, () => set[crypto.randomInt(set.length)]).join('');
        account.newPassword = pick('ABCDEFGHJKLMNPQRSTUVWXYZ', 4) + pick('abcdefghijkmnpqrstuvwxyz', 6) + pick('23456789', 4) + pick('!@#$%^&*-_', 2);
        log(`${account.name}: AWS requires new password; generated one`);
      }
      // Persist the new password BEFORE submitting so it can never be lost.
      try {
        const dumpPath = path.join(os.tmpdir(), `kiro-new-passwords.log`);
        fs.appendFileSync(dumpPath, `${new Date().toISOString()}\t${account.name}\t${account.startUrl || ''}\t${account.newPassword}\n`);
        log(`${account.name}: new password saved to ${dumpPath}`);
      } catch (e) { log(`${account.name}: new password dump failed: ${e.message}`); }
      const count = await fillNewPassword(page, account.newPassword);
      if (count >= 2) {
        passwordValue = account.newPassword;
        filledPass = true;
        log(`${account.name}: new password set form filled`, { fields: count });
        await submit(page);
        await page.waitForTimeout(1800);
        continue;
      }
    }

    const userSelectors = [
      'input[name="username"]',
      'input#username',
      'input[type="email"]',
      'input[name="email"]',
      'input#email',
      'input[autocomplete="username"]',
      'input[placeholder*="Username" i]',
      'input[placeholder*="Email" i]',
      'input:not([type="hidden"]):not([type="password"]):not([type="checkbox"]):not([type="submit"]):not([readonly])',
    ];
    const passSelectors = [
      'input[name="password"]',
      'input#password',
      'input[type="password"]',
      'input[autocomplete="current-password"]',
      'input[placeholder*="Password" i]',
    ];

    if (!filledUser) {
      const selector = await fillFirstVisible(page, userSelectors, account.name);
      if (selector) {
        filledUser = true;
        log(`${account.name}: username filled`, { selector });
        await submit(page);
        await page.waitForTimeout(1200);
        continue;
      }
    }

    const passwordSelector = await fillFirstVisible(page, passSelectors, passwordValue);
    if (passwordSelector && !filledPass) {
      filledPass = true;
      log(`${account.name}: password filled`, { selector: passwordSelector });
      await submit(page);
      await page.waitForTimeout(1600);
      continue;
    }

    if (filledUser && filledPass && await clickByText(page, ['Allow', 'Authorize', 'Autoriser', 'Approve', 'Accept', 'Continue', 'Yes', 'Sign in', 'Sign In'])) {
      await page.waitForTimeout(1200);
      continue;
    }

    await page.waitForTimeout(900);
  }
  return { ok: false, phase: 'timeout', info: lastInfo };
}

async function resolveProfileArn(accessToken, tokenData) {
  if (tokenData.profileArn) return tokenData.profileArn;
  const data = await postJson('https://codewhisperer.us-east-1.amazonaws.com/ListAvailableProfiles', { maxResults: 10 }, {
    Authorization: `Bearer ${accessToken}`,
    'User-Agent': 'aws-sdk-js/1.0.0 ua/2.1 os/Windows lang/js md/nodejs#20 api/codewhispererruntime#1.0.0 m/N,E KiroIDE-0.2.0',
    'x-amz-user-agent': 'aws-sdk-js/1.0.0 KiroIDE-0.2.0',
    'x-amzn-codewhisperer-optout': 'true',
  });
  const arn = (data.profiles || []).map(profile => profile.arn).find(Boolean);
  if (!arn) throw new Error('No profileArn returned');
  return arn;
}

function importToDb(login) {
  if (DB_HELPER) {
    const proc = spawnSync(DB_HELPER, ['--db-helper'], {
      input: JSON.stringify({ db: DB_PATH, login }),
      encoding: 'utf8',
      cwd: ROOT,
      timeout: 60000,
    });
    if (proc.status !== 0) {
      throw new Error((proc.stderr || proc.stdout || 'db helper failed').slice(0, 1000));
    }
    return JSON.parse(proc.stdout);
  }

  const code = `
import json, sys
from pathlib import Path
sys.path.insert(0, r'${ROOT.replaceAll('\\', '\\\\')}')
from scripts.ninerouter_kiro_login import KiroLogin, upsert_sqlite
obj = json.load(sys.stdin)
login = KiroLogin(**obj['login'])
result = upsert_sqlite(Path(obj['db']), login, write=True)
print(json.dumps({
    'ok': True,
    'name': login.profile_name,
    'action': result.get('action'),
    'connectionId': result.get('connectionId'),
    'backup': result.get('backup'),
    'profileArnSet': bool(login.profile_arn),
    'tokenReceived': True,
}, ensure_ascii=False))
`;
  const proc = spawnSync(PYTHON, ['-c', code], {
    input: JSON.stringify({ db: DB_PATH, login }),
    encoding: 'utf8',
    cwd: ROOT,
    timeout: 60000,
  });
  if (proc.status !== 0) {
    throw new Error((proc.stderr || proc.stdout || 'python import failed').slice(0, 1000));
  }
  return JSON.parse(proc.stdout);
}

async function processAccount(account) {
  const region = account.region || 'us-east-1';
  const oidcBase = `https://oidc.${region}.amazonaws.com`;
  const state = crypto.randomBytes(18).toString('base64url');
  const verifier = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const redirectUri = REDIRECT_URI;
  const callback = await startCallbackServer(state, redirectUri);
  let context;
  try {
    log(`${account.name}: registering OIDC client`);
    const client = await postJson(`${oidcBase}/client/register`, {
      clientName: 'Kiro',
      clientType: 'public',
      scopes,
      grantTypes: ['authorization_code', 'refresh_token'],
      redirectUris: [redirectUri],
      issuerUrl: account.startUrl,
    });
    const params = new URLSearchParams({
      response_type: 'code',
      client_id: client.clientId,
      redirect_uri: redirectUri,
      scopes: scopes.join(','),
      state,
      code_challenge: challenge,
      code_challenge_method: 'S256',
    });
    const authorizeUrl = `${oidcBase}/authorize?${params}`;
    const userDataDir = path.join(os.tmpdir(), `kiro-oauth-${account.name}-${Date.now()}`);
    context = await chromium.launchPersistentContext(userDataDir, {
      executablePath: CHROME_PATH,
      headless: false,
      viewport: { width: 1280, height: 900 },
      args: ['--disable-blink-features=AutomationControlled', '--no-first-run', '--no-default-browser-check'],
    });
    await context.addInitScript("Object.defineProperty(navigator, 'webdriver', { get: () => undefined })");
    const page = context.pages()[0] || await context.newPage();
    log(`${account.name}: opening login page`);
    const login = await automateLogin(page, account, authorizeUrl, callback.codePromise);
    if (!login.ok) {
      const screenshot = path.join(os.tmpdir(), `kiro-${account.name}-${login.phase}-${Date.now()}.png`);
      await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {});
      throw new Error(`${account.name}: phase=${login.phase}; url=${login.info?.url || ''}; title=${login.info?.title || ''}; text=${(login.info?.text || '').slice(0, 500).replace(/\s+/g, ' ')}; screenshot=${screenshot}`);
    }
    const code = await callback.codePromise;
    await context.close().catch(() => {});
    context = null;

    log(`${account.name}: exchanging token`);
    const tokenData = await postJson(`${oidcBase}/token`, {
      clientId: client.clientId,
      clientSecret: client.clientSecret,
      grantType: 'authorization_code',
      redirectUri,
      code,
      codeVerifier: verifier,
    });
    if (!tokenData.accessToken || !tokenData.refreshToken) {
      throw new Error(`${account.name}: token response missing accessToken/refreshToken`);
    }
    const profileArn = await resolveProfileArn(tokenData.accessToken, tokenData);
    const expiresIn = Number(tokenData.expiresIn || 3600);
    const expiresAt = new Date(Date.now() + expiresIn * 1000).toISOString();
    const dbResult = importToDb({
      profile_arn: profileArn,
      profile_name: account.name,
      access_token: tokenData.accessToken,
      refresh_token: tokenData.refreshToken,
      expires_at: expiresAt,
      expires_in: expiresIn,
      provider_data: {
        profileArn,
        accountName: account.name,
        clientId: client.clientId,
        clientSecret: client.clientSecret,
        region,
        authMethod: 'idc',
        startUrl: account.startUrl,
        provider: 'Enterprise',
        ...(account.mfaSecret ? { mfaSecret: account.mfaSecret } : {}),
        ...(account.newPassword ? { currentPassword: account.newPassword } : {}),
      },
    });
    return { ...dbResult, startUrl: account.startUrl };
  } finally {
    if (context) await context.close().catch(() => {});
    try { callback.server.close(); } catch {}
  }
}

async function verify9router() {
  const providers = await getJson(`${BASE_URL}/api/providers`);
  const kiroConnections = (providers.connections || []).filter(item => item.provider === 'kiro');
  const models = await getJson(`${BASE_URL}/v1/models`);
  const data = models.data || [];
  const kr = data.filter(item => String(item.id || '').startsWith('kr/'));
  return {
    kiroConnections: kiroConnections.length,
    totalModels: data.length,
    publicKiroModels: kr.length,
    sample: kr.slice(0, 8).map(item => item.id),
  };
}

const input = JSON.parse(await readStdin());
const accounts = Array.isArray(input) ? input : input.accounts;
const results = [];
for (const account of accounts) {
  try {
    results.push(await processAccount(account));
  } catch (error) {
    results.push({ ok: false, name: account.name, error: String(error.message || error) });
  }
}
let verify;
try {
  verify = await verify9router();
} catch (error) {
  verify = { error: String(error.message || error) };
}
console.log(JSON.stringify({ ok: results.every(item => item.ok), results, verify }, null, 2));
