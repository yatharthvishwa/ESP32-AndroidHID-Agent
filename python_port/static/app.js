const $ = (id) => document.getElementById(id);
let current = null;
let busy = false;

async function request(url, options = {}) {
  const res = await fetch(url, { cache: 'no-store', ...options });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function setStatus(s) {
  current = s;
  $('status').textContent = s.message || 'Ready';
  $('gx').textContent = Number(s.gain_x).toFixed(3);
  $('gy').textContent = Number(s.gain_y).toFixed(3);
  $('dimensions').textContent = `${s.width || '?'} × ${s.height || '?'}`;
  $('bar').style.width = s.total ? `${(s.active ? s.index : s.total) / s.total * 100}%` : '0%';
  $('overlay').classList.toggle('hidden', !!s.active);
  $('connection').textContent = 'Connected';
  $('connection').classList.add('ok');
  if (s.error) $('result').textContent = `Calibration error: ${s.error}`;
  else if (!s.active && s.index >= s.total && s.total) $('result').textContent = s.message;
  else $('result').textContent = s.detector_message || 'Automatic mode detects cursor movement by comparing screenshots; manual fallback is available.';
}
async function refreshFrame() {
  try {
    const response = await fetch(`/api/frame.png?t=${Date.now()}`, {cache:'no-store'});
    if (response.ok) {
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const old = $('frame').dataset.url;
      $('frame').src = url; $('frame').dataset.url = url;
      if (old) URL.revokeObjectURL(old);
    }
  } catch (_) {}
}
async function refreshStatus() {
  try { setStatus(await request('/api/status')); }
  catch (e) { $('connection').textContent = 'Disconnected'; $('connection').classList.remove('ok'); $('status').textContent = e.message; }
}
async function startCalibration(url, button) {
  if (busy) return; busy = true; $('start').disabled = true; $('manual').disabled = true;
  try { setStatus(await request(url, {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})); await refreshFrame(); }
  catch (e) { $('status').textContent = e.message; }
  finally { busy = false; $('start').disabled = false; $('manual').disabled = false; }
}
$('start').addEventListener('click', () => startCalibration('/api/start', $('start')));
$('manual').addEventListener('click', () => startCalibration('/api/start-manual', $('manual')));
$('home').addEventListener('click', async () => {
  if (busy) return; busy = true;
  try { setStatus(await request('/api/home', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})); await refreshFrame(); }
  catch (e) { $('status').textContent = e.message; }
  finally { busy = false; }
});
$('frame').addEventListener('click', async (event) => {
  if (busy || !current?.active) return;
  const img = $('frame'); const rect = img.getBoundingClientRect();
  // Map CSS-scaled image coordinates to the original ADB screenshot pixel coordinates.
  const x = (event.clientX - rect.left) * img.naturalWidth / rect.width;
  const y = (event.clientY - rect.top) * img.naturalHeight / rect.height;
  busy = true; img.classList.add('waiting');
  try {
    setStatus(await request('/api/click', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x,y})}));
    await refreshFrame();
  } catch (e) { $('status').textContent = e.message; }
  finally { busy = false; img.classList.remove('waiting'); }
});
(async () => { await refreshStatus(); await refreshFrame(); setInterval(refreshStatus, 1500); setInterval(refreshFrame, 1200); })();
