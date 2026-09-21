(function () {
  const defaults = { size: '1.08rem', leading: '1.58', width: '52rem' };
  function cssValue(el, value) {
    return el.dataset.unit ? value + el.dataset.unit : value;
  }
  function updateOutput(el) {
    const output = document.getElementById(el.id + '-value');
    if (output) output.value = output.textContent = cssValue(el, el.value);
  }
  function readStored() {
    const m = document.cookie.match(/(?:^|; )puppet_reader=([^;]+)/);
    if (m) { try { return JSON.parse(decodeURIComponent(m[1])); } catch (_) {} }
    try { return JSON.parse(localStorage.getItem('puppet_reader') || '{}'); } catch (_) { return {}; }
  }
  function apply(value) {
    document.documentElement.style.setProperty('--reader-size', value.size || defaults.size);
    document.documentElement.style.setProperty('--reader-leading', value.leading || defaults.leading);
    document.documentElement.style.setProperty('--reader-width', value.width || defaults.width);
    document.querySelectorAll('[data-reader-setting]').forEach(function (el) {
      if (value[el.dataset.readerSetting]) {
        el.value = value[el.dataset.readerSetting].replace(/rem$/, '');
      }
      updateOutput(el);
    });
  }
  function save(value) {
    document.cookie = 'puppet_reader=' + encodeURIComponent(JSON.stringify(value)) + '; Path=/; SameSite=Lax';
    try { localStorage.setItem('puppet_reader', JSON.stringify(value)); } catch (_) {}
    apply(value);
  }
  const value = readStored();
  apply(value);
  document.querySelectorAll('[data-reader-setting]').forEach(function (el) {
    el.addEventListener('change', function () {
      const next = readStored();
      next[el.dataset.readerSetting] = cssValue(el, el.value);
      save(next);
      updateOutput(el);
    });
  });

  function liveOrigin() {
    return location.protocol === 'http:' && (location.hostname === '127.0.0.1' || location.hostname === 'localhost');
  }
  function crawlerText(status) {
    var limit = status.budget_limit || '-';
    var action = status.current_action || status.lifecycle || 'Stopped';
    var used = status.budget_used === undefined ? 0 : status.budget_used;
    return (status.target ? status.target + ' - ' : '') + action + ' - ' + used + '/' + limit + ' saved';
  }
  function startCrawlerControls() {
    if (!liveOrigin() || !window.fetch) return;
    var panel = document.getElementById('crawler-controls');
    if (!panel) return;
    var statusOutput = document.getElementById('crawler-status');
    var errorOutput = document.getElementById('crawler-error');
    var refreshButton = panel.querySelector('[data-crawler-refresh]');
    var startButton = panel.querySelector('.crawler-start');
    var stopButton = panel.querySelector('[data-crawler-stop]');
    var setup = panel.querySelector('.crawler-setup');
    var live = panel.querySelector('.crawler-live');
    var capability = '';
    var generation = null;
    var timer = null;
    function showError(message) {
      if (errorOutput) { errorOutput.textContent = message; errorOutput.hidden = false; }
    }
    function render(status) {
      panel.hidden = false;
      if (statusOutput) statusOutput.textContent = crawlerText(status);
      var state = status.lifecycle || 'idle';
      var active = state === 'starting' || state === 'running' || state === 'stopping' || state === 'finalizing';
      if (setup) setup.hidden = active;
      if (live) live.hidden = !active && state !== 'complete';
      if (startButton) startButton.hidden = active;
      if (stopButton) stopButton.hidden = !active;
      if (status.error && errorOutput) { errorOutput.textContent = status.error; errorOutput.hidden = false; }
      if (status.request) {
        var request = status.request;
        var target = document.getElementById('crawler-target');
        var maxPosts = document.getElementById('crawler-max-posts');
        var context = document.getElementById('crawler-context-mode');
        var depth = document.getElementById('crawler-depth');
        var startFocus = document.getElementById('crawler-start-focus');
        var startNetwork = document.getElementById('crawler-start-network');
        var images = document.getElementById('crawler-images');
        if (target && request.target) target.value = request.target;
        if (maxPosts && request.max_posts !== undefined) maxPosts.value = request.max_posts;
        if (context && request.context) context.value = request.context;
        if (depth && request.context_depth !== null && request.context_depth !== undefined) depth.value = request.context_depth;
        if (startFocus && request.focus) startFocus.value = request.focus;
        if (startNetwork && request.profile_id) startNetwork.value = request.profile_id;
        if (images && request.full_res !== undefined) images.value = request.full_res ? 'full' : 'small';
      }
      var budget = document.getElementById('crawler-budget');
      var context = document.getElementById('crawler-context-multiplier');
      var affinity = document.getElementById('crawler-affinity');
      if (budget) budget.textContent = (status.budget_used === undefined ? 0 : status.budget_used) + '/' + (status.budget_limit || '-');
      if (context) context.textContent = (status.context_multiplier || 1).toFixed(2) + 'x';
      if (affinity) affinity.textContent = (status.relationship_multiplier || 1).toFixed(2) + 'x';
      panel.querySelectorAll('[data-crawler-control="focus"]').forEach(function (button) {
        button.setAttribute('aria-pressed', button.value === status.focus ? 'true' : 'false');
      });
      var network = document.getElementById('crawler-network');
      if (network && status.network_profile_id) network.value = status.network_profile_id;
      if (generation !== null && status.presentation_generation !== generation && refreshButton) refreshButton.hidden = false;
      generation = status.presentation_generation;
      panel.querySelectorAll('[data-crawler-control], [data-crawler-step], [data-crawler-select]').forEach(function (control) {
        control.disabled = !active;
      });
    }
    function request(path, options) {
      return fetch(path, options).then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) throw new Error(body.error || 'Crawler request failed');
          return body;
        });
      });
    }
    function control(name, value) {
      request('/__crawler/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Crawler-Capability': capability},
        body: JSON.stringify((function () { var body = {}; body[name] = value; return body; }()) )
      }).then(render).catch(showError);
    }
    function start() {
      var target = document.getElementById('crawler-target');
      var maxPosts = document.getElementById('crawler-max-posts');
      var context = document.getElementById('crawler-context-mode');
      var depth = document.getElementById('crawler-depth');
      var images = document.getElementById('crawler-images');
      var startFocus = document.getElementById('crawler-start-focus');
      var startNetwork = document.getElementById('crawler-start-network');
      request('/__crawler/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Crawler-Capability': capability},
        body: JSON.stringify({
          target: target ? target.value : '',
          max_posts: maxPosts ? Number(maxPosts.value) : 300,
          context: context ? context.value : 'explore',
          context_depth: depth ? Number(depth.value) : 2,
          focus: startFocus ? startFocus.value : 'balanced',
          profile_id: startNetwork ? startNetwork.value : 'gentle',
          full_res: images ? images.value === 'full' : false
        })
      }).then(render).catch(showError);
    }
    if (startButton) startButton.addEventListener('click', start);
    if (stopButton) stopButton.addEventListener('click', function () {
      request('/__crawler/stop', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Crawler-Capability': capability},
        body: '{}'
      }).then(render).catch(showError);
    });
    panel.querySelectorAll('[data-crawler-control]').forEach(function (button) {
      button.addEventListener('click', function () { control(button.dataset.crawlerControl, button.value === 'true' ? true : button.value); });
    });
    panel.querySelectorAll('[data-crawler-step]').forEach(function (button) {
      button.addEventListener('click', function () {
        var current = button.dataset.crawlerStep === 'budget_limit' ? (Number(document.getElementById('crawler-budget').textContent.split('/')[1]) || 0) : Number(document.getElementById(button.dataset.crawlerStep === 'context_multiplier' ? 'crawler-context-multiplier' : 'crawler-affinity').textContent.replace('x', ''));
        control(button.dataset.crawlerStep, current + Number(button.dataset.step));
      });
    });
    var network = document.getElementById('crawler-network');
    if (network) network.addEventListener('change', function () { control('network_profile_id', network.value); });
    if (refreshButton) refreshButton.addEventListener('click', function () { location.reload(); });
    request('/__crawler/bootstrap').then(function (body) {
      capability = body.capability || '';
      render(body.status || {});
      timer = setInterval(function () {
        if (document.hidden) return;
        request('/__crawler/status').then(render).catch(function () {
          if (timer) clearInterval(timer);
          timer = null;
          panel.hidden = true;
        });
      }, 1000);
    }).catch(function () { panel.hidden = true; });
  }
  startCrawlerControls();
}());
