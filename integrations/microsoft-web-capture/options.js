const DEFAULT_URL = 'http://127.0.0.1:8765/?surface=inbox';
const form = document.getElementById('settings-form');
const input = document.getElementById('workstack-url');
const status = document.getElementById('status');

chrome.storage.sync.get({ workStackUrl: DEFAULT_URL }).then(result => {
  input.value = result.workStackUrl || DEFAULT_URL;
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  status.textContent = '';
  try {
    const url = new URL(input.value);
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Use an HTTP or HTTPS Work Stack URL.');
    const match = `${url.origin}/*`;
    const granted = await chrome.permissions.request({ origins: [match] });
    if (!granted) throw new Error('Host permission was not granted.');
    await chrome.scripting.unregisterContentScripts({ ids: ['workstack-configured-bridge'] }).catch(() => undefined);
    await chrome.scripting.registerContentScripts([{
      id: 'workstack-configured-bridge',
      matches: [match],
      js: ['bridge.js'],
      runAt: 'document_start',
      persistAcrossSessions: true
    }]);
    await chrome.storage.sync.set({ workStackUrl: url.href });
    status.textContent = 'Saved. Future captures will open this Work Stack host.';
  } catch (error) {
    status.textContent = error.message || 'Settings were not saved.';
  }
});
