"""Record the JSON a provider page fetches, instead of the prose it renders.

Every Claude breakage this fork has chased was a change to rendered text: a
heading renamed, a row label renamed, the DOM re-nested, the polarity wording
altered, the route moved. Prose is the product surface - it is *supposed* to
change. Reading it will keep breaking.

The data behind it moves far less, and it carries meaning the text does not.
``{"utilization": 0.64}`` needs no inference about whether "left" is a quota
word or a clock word, and ``resets_at`` is an exact timestamp rather than
"Resets in 2 hr 59 min" to be parsed.

WHAT IS CAPTURED, AND WHAT IS NOT

A provider page fetches far more than usage - on claude.ai that includes
conversation content. Capturing response bodies would put that in the log and
on the clipboard, which is a real escalation over reading a usage panel. So
the recorder never keeps a body. It keeps a *shape*:

* numbers and booleans - the quota values, which are the entire point
* ISO-8601 timestamps  - reset times, likewise
* every other string   - replaced by ``<str:N>``, its length only
* arrays               - first element's shape plus a length marker
* depth, key count, response size and URL count are all capped

The sketching happens in the page, so a full body never crosses into Python.
Cross-origin responses are ignored entirely.

That is enough to write a field mapping from a real account without the
content ever leaving the browser context.
"""

from __future__ import annotations

import logging

from PyQt6.QtWebEngineCore import QWebEngineScript

log = logging.getLogger("aigauge.webview.api_capture")

_SCRIPT_NAME = "ai-gauge-api-recorder"

# Injected at DocumentCreation so it wraps fetch/XHR before any page script
# runs. Anything that throws in here would break the host page's networking,
# so every hook returns the original value and swallows its own errors.
RECORDER_JS = r"""
(() => {
  if (window.__ag_api) return;
  const MAX_URLS = 12;      // distinct paths retained
  const MAX_KEYS = 200;     // total keys sketched per response
  const MAX_DEPTH = 5;
  const MAX_BYTES = 400000;
  const ISO = /^\d{4}-\d{2}-\d{2}T[\d:.]+/;

  window.__ag_api = {};
  let urls = 0;

  function sketch(value, depth, budget) {
    if (budget.n++ > MAX_KEYS) return '<truncated>';
    if (value === null) return null;
    const t = typeof value;
    if (t === 'number' || t === 'boolean') return value;
    // Timestamps are kept because reset times are load-bearing; every other
    // string is reduced to its length so page content cannot leak.
    if (t === 'string') return ISO.test(value) ? value : '<str:' + value.length + '>';
    if (Array.isArray(value)) {
      if (!value.length) return [];
      return [sketch(value[0], depth + 1, budget), '<len:' + value.length + '>'];
    }
    if (t === 'object') {
      if (depth >= MAX_DEPTH) return '<object>';
      const out = {};
      for (const key of Object.keys(value)) {
        if (budget.n > MAX_KEYS) { out['<truncated>'] = true; break; }
        out[key] = sketch(value[key], depth + 1, budget);
      }
      return out;
    }
    return '<' + t + '>';
  }

  function keep(rawUrl, text) {
    try {
      if (urls >= MAX_URLS || !text || text.length > MAX_BYTES) return;
      const u = new URL(rawUrl, location.href);
      if (u.hostname !== location.hostname) return;
      if (u.pathname in window.__ag_api) return;
      window.__ag_api[u.pathname] = sketch(JSON.parse(text), 0, {n: 0});
      urls += 1;
    } catch (e) { /* not JSON, or blocked - nothing to record */ }
  }

  function isJson(res) {
    try {
      const ct = res.headers && res.headers.get && res.headers.get('content-type');
      return !!ct && ct.indexOf('json') !== -1;
    } catch (e) { return false; }
  }

  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (...args) {
      const p = origFetch.apply(this, args);
      try {
        return p.then(res => {
          // Clone only JSON: cloning buffers the body, and doing that to a
          // streamed response would hold the whole thing in memory.
          try {
            if (isJson(res)) {
              res.clone().text().then(t => keep(res.url || String(args[0]), t))
                .catch(() => {});
            }
          } catch (e) {}
          return res;
        });
      } catch (e) { return p; }
    };
  }

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      this.addEventListener('load', () => {
        try { keep(url, this.responseText); } catch (e) {}
      });
    } catch (e) {}
    return origOpen.call(this, method, url, ...rest);
  };
})();
"""

# Embed in a provider extractor to hand the sketch back with the payload.
READBACK_JS = "(window.__ag_api || null)"


def install_api_recorder(page) -> bool:
    """Attach the recorder to one page. Returns whether it was installed.

    Per page rather than per profile: the profile outlives the scrape and is
    shared with the sign-in window, and nothing there needs this.
    """
    try:
        script = QWebEngineScript()
        script.setName(_SCRIPT_NAME)
        script.setSourceCode(RECORDER_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        # MainWorld: an isolated world gets its own fetch, so a wrapper there
        # would never see the page's own requests.
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        page.scripts().insert(script)
        return True
    except Exception:  # noqa: BLE001 - capture is diagnostic, never load-bearing
        log.warning("api recorder could not be installed", exc_info=True)
        return False
