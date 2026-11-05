/* Minimal configuration bridge for resilient anonymous report drafting.
   It only stores a user's draft locally after interaction. The live Cignal UI is still server-driven. */
(() => {
  const KEY = 'cignal:offline-report-draft:v1';
  const id = name => document.getElementById('cignal-' + name);
  const value = name => {
    const root = id(name);
    if (!root) return '';
    const field = root.matches('input,textarea,select') ? root : root.querySelector('input,textarea,select');
    return field ? field.value : '';
  };
  const readDraft = () => {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (_) { return {}; }
  };
  const writeDraft = queued => {
    const previous = readDraft();
    const draft = {
      title: value('report-title') || previous.title || '',
      description: value('report-description') || previous.description || '',
      location_text: value('report-location') || previous.location_text || '',
      latitude: value('report-latitude') || previous.latitude || '',
      longitude: value('report-longitude') || previous.longitude || '',
      language: 'en', disaster_type: 'unknown', urgency: 'unknown',
      idempotency_key: previous.idempotency_key || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()),
      queued: queued || previous.queued || false
    };
    localStorage.setItem(KEY, JSON.stringify(draft));
    return draft;
  };
  const apiBase = () => document.querySelector('meta[name="cignal-api-url"]')?.content || '';
  const sync = async () => {
    const draft = readDraft();
    if (!draft.queued || !draft.title || (draft.description || '').trim().length < 20 || !navigator.onLine) return;
    const base = (apiBase() || window.location.origin).replace(/\/$/, '');
    try {
      const response = await fetch(base + '/api/reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': draft.idempotency_key },
        body: JSON.stringify({
          title: draft.title, description: draft.description,
          location_text: draft.location_text || null,
          latitude: draft.latitude ? Number(draft.latitude) : null,
          longitude: draft.longitude ? Number(draft.longitude) : null,
          language: draft.language, disaster_type: draft.disaster_type, urgency: draft.urgency
        })
      });
      if (!response.ok) throw new Error('The report could not be synchronized');
      const result = await response.json();
      localStorage.removeItem(KEY);
      alert('Offline report synchronized. Tracking code: ' + result.tracking_code + '. This queued report was submitted anonymously.');
    } catch (_) {
      /* Keep the queued draft. A later online event or page reload can retry with the same idempotency key. */
    }
  };
  document.addEventListener('input', event => {
    if (['report-title', 'report-description', 'report-location', 'report-latitude', 'report-longitude'].some(name => id(name)?.contains(event.target))) {
      writeDraft(false);
    }
  }, true);
  document.addEventListener('click', event => {
    if (event.target.closest('#cignal-submit-button') && !navigator.onLine) {
      event.preventDefault();
      event.stopImmediatePropagation();
      writeDraft(true);
      alert('No network connection. Your report draft is stored in this browser and will be submitted anonymously when the connection returns. Keep this tab open.');
    }
  }, true);
  window.addEventListener('online', sync);
  window.CignalDrafts = { clear: () => localStorage.removeItem(KEY), sync };
  window.addEventListener('load', sync);
})();
