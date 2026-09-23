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
  function startDrawer() {
    var toggle = document.querySelector('.app-menu-toggle');
    var drawer = document.getElementById('app-drawer');
    var close = drawer ? drawer.querySelector('.app-drawer-close') : null;
    var backdrop = document.querySelector('.app-drawer-backdrop');
    if (!toggle || !drawer) return;
    var lastFocus = toggle;
    function setClosedState() {
      drawer.hidden = true;
      drawer.setAttribute('aria-hidden', 'true');
      if (backdrop) backdrop.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('drawer-open');
    }
    function closeDrawer() {
      setClosedState();
      lastFocus.focus();
    }
    function openDrawer() {
      lastFocus = document.activeElement || toggle;
      drawer.hidden = false;
      drawer.setAttribute('aria-hidden', 'false');
      if (backdrop) backdrop.hidden = false;
      toggle.setAttribute('aria-expanded', 'true');
      document.body.classList.add('drawer-open');
      if (close) close.focus();
      else {
        var first = drawer.querySelector('a, button');
        if (first) first.focus();
      }
    }
    setClosedState();
    toggle.addEventListener('click', function () {
      if (drawer.hidden) openDrawer(); else closeDrawer();
    });
    if (close) close.addEventListener('click', closeDrawer);
    if (backdrop) backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !drawer.hidden) closeDrawer();
    });
  }
  function startFilterPanels() {
    var panels = document.querySelectorAll('.explore-filter-disclosure');
    if (!panels.length) return;
    panels.forEach(function (disclosure) {
      var summary = disclosure.querySelector('summary');
      var panel = disclosure.querySelector('.graph-controls');
      if (!summary || !panel) return;
      function place() {
        if (!disclosure.open) {
          panel.style.transform = '';
          return;
        }
        panel.style.transform = 'none';
        var trigger = summary.getBoundingClientRect();
        var box = panel.getBoundingClientRect();
        var gutter = 12;
        var left = Math.max(gutter, Math.min(trigger.left, window.innerWidth - box.width - gutter));
        panel.style.transform = 'translateX(' + Math.round(left - trigger.left) + 'px)';
      }
      disclosure.addEventListener('toggle', function () { window.requestAnimationFrame(place); });
      window.addEventListener('resize', place);
    });
  }
  function startFeedFilters() {
    var cardsRoot = document.getElementById('feed-posts');
    var indexElement = document.getElementById('feed-index');
    if (!cardsRoot || !indexElement) return;
    var index;
    try { index = JSON.parse(indexElement.textContent || '{}'); } catch (_) { return; }
    var pov = document.getElementById('feed-pov');
    var search = document.getElementById('feed-search');
    var neighborhood = document.getElementById('feed-neighborhood');
    var distance = document.getElementById('feed-distance');
    var affinity = document.getElementById('feed-affinity-level');
    var reblog = document.getElementById('feed-evidence-reblog');
    var like = document.getElementById('feed-evidence-like');
    var ask = document.getElementById('feed-evidence-ask');
    var includeTarget = document.getElementById('feed-include-target');
    var sort = document.getElementById('feed-sort');
    var evidenceList = document.getElementById('feed-evidence-list');
    var cards = Array.prototype.slice.call(cardsRoot.querySelectorAll('[data-feed-blog]'));
    function reloadSemanticFeed() {
      var params = new URLSearchParams();
      params.set('pov', pov ? pov.value : '__all__');
      params.set('sort', sort ? sort.value : 'newest');
      if (affinity) params.set('affinity', affinity.value);
      if (neighborhood) params.set('neighborhood', neighborhood.value);
      if (distance) params.set('breadth', distance.value);
      if (search && search.value) params.set('search', search.value);
      if (includeTarget && includeTarget.checked) params.set('include_target', '1');
      [
        [reblog, 'direct_reblog'], [like, 'direct_like'], [ask, 'structured_ask']
      ].forEach(function (item) { if (item[0] && item[0].checked) params.append('evidence', item[1]); });
      window.location.href = window.location.pathname + '?' + params.toString();
    }
    function selectedScore(source) {
      var scores = (source && source.evidence_scores) || {};
      var total = 0;
      if (reblog && reblog.checked) total += Number(scores.reblogs || 0);
      if (like && like.checked) total += Number(scores.likes || 0);
      if (ask && ask.checked) total += Number(scores.asks || 0);
      return total;
    }
    function affinityPass(source) {
      if (!source || !affinity || affinity.value === 'any') return true;
      return (source.affinity_bands || []).indexOf(affinity.value) >= 0;
    }
    function updateEvidence(target, sources) {
      if (!evidenceList) return;
      var rows = Object.keys(sources).map(function (name) {
        var source = sources[name];
        return { name: name, source: source, score: selectedScore(source) };
      }).filter(function (item) { return item.score > 0; }).sort(function (a, b) { return b.score - a.score || a.name.localeCompare(b.name); });
      evidenceList.innerHTML = rows.length ? rows.slice(0, 20).map(function (item) {
        var source = item.source;
        return '<p><bdi dir="auto">' + item.name.replace(/[&<>"']/g, function (c) { return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }) + '</bdi> — ' + Number(source.reblogs || 0) + ' distinct posts reblogged; ' + Number(source.days || 0) + ' observed days; explicit likes: ' + Number(source.likes || 0) + ' saved.</p>';
      }).join('') : '<p>No selected affinity evidence is saved for this target.</p>';
    }
    function update() {
      var target = pov ? pov.value : '__all__';
      var targetData = index.targets && index.targets[target] ? index.targets[target] : {sources: {}};
      var sources = targetData.sources || {};
      var query = String(search && search.value || '').toLocaleLowerCase();
      function contextPass(card, source) {
        var selectedNeighborhood = neighborhood ? neighborhood.value : '__all__';
        var selectedDistance = distance ? distance.value : 'all';
        var cardNeighborhood = String(card.dataset.feedNeighborhood || '').toLocaleLowerCase();
        var cardDistance = Number(card.dataset.feedDistance);
        var sourceNeighborhood = source && source.neighborhood ? String(source.neighborhood).toLocaleLowerCase() : cardNeighborhood;
        var sourceDistance = source && source.distance !== undefined && source.distance !== null ? Number(source.distance) : cardDistance;
        var neighborhoodPass = selectedNeighborhood === '__all__' || sourceNeighborhood === selectedNeighborhood;
        var distancePass = selectedDistance === 'all' || (selectedDistance === '3' ? sourceDistance >= 3 : String(sourceDistance) === selectedDistance);
        return neighborhoodPass && distancePass;
      }
      cards.forEach(function (card) {
        var blog = card.dataset.feedBlog || '';
        var source = sources[blog];
        var own = target !== '__all__' && blog === target;
        var sourcePass = target === '__all__' ? contextPass(card, null) : (source && (targetData.eligible_blogs || []).indexOf(blog) >= 0 && selectedScore(source) > 0 && affinityPass(source) && contextPass(card, source));
        var evidencePass = target === '__all__' || own || sourcePass;
        var searchPass = !query || card.textContent.toLocaleLowerCase().indexOf(query) >= 0;
        card.hidden = !searchPass || !evidencePass || (own && (!includeTarget || !includeTarget.checked));
        card._feedScore = own ? Number.MAX_SAFE_INTEGER : source ? selectedScore(source) : 0;
      });
      var visible = cards.filter(function (card) { return !card.hidden; });
      visible.sort(function (a, b) {
        if (sort.value === 'affinity') return b._feedScore - a._feedScore || Number(b.dataset.feedTimestamp || 0) - Number(a.dataset.feedTimestamp || 0);
        var direction = sort.value === 'oldest' ? 1 : -1;
        return direction * (Number(a.dataset.feedTimestamp || 0) - Number(b.dataset.feedTimestamp || 0));
      }).forEach(function (card) { cardsRoot.appendChild(card); });
      updateEvidence(target, sources);
    }
    [pov, search, neighborhood, distance, affinity, reblog, like, ask, includeTarget, sort].forEach(function (control) {
      if (control) control.addEventListener(control.type === 'search' ? 'input' : 'change', update);
    });
    [pov, neighborhood, distance, affinity, reblog, like, ask, includeTarget].forEach(function (control) {
      if (control) control.addEventListener('change', reloadSemanticFeed);
    });
    update();
  }
  function renderHeaderStatus(status) {
    var output = document.getElementById('crawler-header-status');
    var drawerOutput = document.getElementById('crawler-drawer-status');
    if (!output && !drawerOutput) return;
    var lifecycle = status.lifecycle || 'idle';
    var active = ['starting', 'running', 'stopping', 'finalizing'].indexOf(lifecycle) >= 0;
    var text = lifecycle === 'idle' ? 'Idle' : lifecycle === 'complete' ? 'Complete' : lifecycle === 'stopped' ? 'Stopped safely' : lifecycle === 'failed' ? 'Error' : active ? ((status.budget_used || 0) + '/' + (status.budget_limit || '-') + ' saved') : lifecycle;
    [output, drawerOutput].forEach(function (el) {
      if (!el) return;
      el.textContent = text;
      el.setAttribute('aria-label', 'Crawler status: ' + text);
    });
  }
  function startHeaderStatus() {
    if (!liveOrigin() || !window.fetch) return;
    function poll() {
      fetch('/__crawler/status').then(function (response) {
        if (!response.ok) throw new Error('status unavailable');
        return response.json();
      }).then(renderHeaderStatus).catch(function () {});
    }
    poll();
    window.setInterval(poll, 1000);
  }
  function crawlerText(status) {
    var lifecycle = status.lifecycle || 'idle';
    if (lifecycle === 'idle') return 'Idle';
    if (lifecycle === 'stopped') return 'Stopped safely; saved work is preserved.';
    if (lifecycle === 'complete') return status.completion_reason ? 'Complete - ' + status.completion_reason : 'Complete; saved work is preserved.';
    var limit = status.budget_limit || '-';
    var action = status.current_action || lifecycle || 'Stopped';
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
    var shutdownButton = panel.querySelector('[data-server-shutdown]');
    var setup = panel.querySelector('.crawler-setup');
    var live = panel.querySelector('.crawler-live');
    var strategyDescriptions = {
      archive: 'A concentrated capture shape for the selected target set.',
      neighborhood: 'A deep centre with a moderate immediate relationship breadth.',
      explore: 'A deep centre that tapers across a broader relationship network.',
      survey: 'A shallow, wide capture shape. Observation can continue where depth is zero.'
    };
    // This is presentation metadata only. The server resolves the submitted
    // shape into the same integer curve shown below; no scheduler reads this.
    var presetShapes = {
      archive: {max_breadth: 0, max_depth: 300, shape: [[0, 1], [1, 1]]},
      neighborhood: {max_breadth: 1, max_depth: 100, shape: [[0, 1], [1, .5]]},
      explore: {max_breadth: 3, max_depth: 100, shape: [[0, 1], [1 / 3, .5], [2 / 3, .1], [1, .01]]},
      survey: {max_breadth: 6, max_depth: 2, shape: [[0, 1], [1 / 3, 1], [.5, .5], [1, .5]]}
    };
    var capability = '';
    var generation = null;
    var timer = null;
    var formHydrated = false;
    var policyDirty = false;
    var renderedRunId = null;
    // The server increments this only for lifecycle transitions.  It lets the
    // browser discard an older in-flight polling reply after a crawl finishes.
    var renderedStatusRevision = -1;
    var etaDeadline = null;
    var etaKey = '';
    var serverControl = false;
    function showError(message) {
      if (errorOutput) { errorOutput.textContent = message; errorOutput.hidden = false; }
    }
    var activeShape = presetShapes.neighborhood.shape;
    function shapePayload(shape) {
      return shape.map(function (point) { return {breadth: point[0], depth: point[1]}; });
    }
    function sameShape(left, right) {
      return left.length === right.length && left.every(function (point, index) {
        return Math.abs(point[0] - right[index][0]) < 0.000001 && Math.abs(point[1] - right[index][1]) < 0.000001;
      });
    }
    function updatePresetState() {
      var label = document.getElementById('crawler-preset-state');
      var select = document.getElementById('crawler-strategy');
      if (!label || !select) return;
      var preset = presetShapes[select.value];
      label.textContent = preset && sameShape(activeShape, preset.shape) ?
        select.value.charAt(0).toUpperCase() + select.value.slice(1) : 'Custom';
    }
    function shapeValue(shape, x) {
      x = Math.max(0, Math.min(1, x));
      if (x <= shape[0][0]) return shape[0][1];
      for (var i = 1; i < shape.length; i += 1) {
        var left = shape[i - 1], right = shape[i];
        if (x <= right[0]) return left[1] + (right[1] - left[1]) * ((x - left[0]) / (right[0] - left[0]));
      }
      return shape[shape.length - 1][1];
    }
    function quantizeDepth(y, maxDepth) {
      if (y <= 0 || maxDepth <= 0) return 0;
      return Math.min(maxDepth, Math.max(1, Math.floor(maxDepth * y + .5)));
    }
    function resolvedCurve() {
      var breadth = Math.max(0, Number((document.getElementById('crawler-max-breadth') || {}).value) || 0);
      var depth = Math.max(0, Number((document.getElementById('crawler-max-depth') || {}).value) || 0);
      var result = [];
      for (var value = 0; value <= breadth; value += 1) {
        result.push({breadth: value, history_posts: quantizeDepth(shapeValue(activeShape, breadth === 0 ? 0 : value / breadth), depth)});
      }
      return result;
    }
    function renderCurve() {
      var table = document.querySelector('#crawler-curve-table tbody');
      var line = document.getElementById('crawler-curve-line');
      if (!table) return;
      var curve = resolvedCurve();
      table.innerHTML = curve.map(function (point) { return '<tr><th scope="row">' + point.breadth + '</th><td>' + point.history_posts + '</td></tr>'; }).join('');
      if (line) {
        var maxBreadth = Math.max(1, curve.length - 1);
        var maxDepth = Math.max(1, Number((document.getElementById('crawler-max-depth') || {}).value) || 1);
        line.setAttribute('points', curve.map(function (point) { return ((point.breadth / maxBreadth) * 620 + 10) + ',' + (170 - (point.history_posts / maxDepth) * 150); }).join(' '));
      }
    }
    function formatDuration(seconds) {
      if (!isFinite(seconds) || seconds < 0) return '--';
      seconds = Math.ceil(seconds);
      if (seconds < 60) return seconds + 's';
      var minutes = Math.floor(seconds / 60);
      if (minutes < 60) return minutes + 'm ' + (seconds % 60) + 's';
      var hours = Math.floor(minutes / 60);
      return hours + 'h ' + (minutes % 60) + 'm';
    }
    function renderMetrics(status, active) {
      var used = Number(status.budget_used) || 0;
      var limit = Number(status.budget_limit) || 0;
      var progress = document.getElementById('crawler-progress');
      var progressLabel = document.getElementById('crawler-progress-label');
      if (progressLabel) progressLabel.textContent = used + ' / ' + (limit > 0 ? limit : 'unbounded');
      if (progress) {
        if (limit > 0) {
          progress.max = limit;
          progress.value = Math.min(used, limit);
          progress.setAttribute('aria-label', used + ' of ' + limit + ' new posts saved');
        } else {
          progress.removeAttribute('value');
          progress.removeAttribute('max');
          progress.setAttribute('aria-label', 'Unbounded crawl; ' + used + ' new posts saved');
        }
      }
      var started = Number(status.started_at) || 0;
      var finished = Number(status.finished_at) || 0;
      var end = finished || (active ? Date.now() / 1000 : 0);
      var elapsed = started && end ? Math.max(0, end - started) : 0;
      var rate = elapsed > 0 ? used / elapsed : 0;
      var nextEtaKey = [started, status.target || '', limit].join('|');
      if (nextEtaKey !== etaKey) {
        etaKey = nextEtaKey;
        etaDeadline = null;
      }
      var speedLabel = document.getElementById('crawler-speed-label');
      var speedMeter = document.getElementById('crawler-speed-meter');
      if (speedLabel) speedLabel.textContent = rate.toFixed(1) + ' posts/s';
      if (speedMeter) {
        speedMeter.max = Math.max(1, rate * 2);
        speedMeter.value = rate;
        speedMeter.setAttribute('aria-label', rate.toFixed(1) + ' posts per second');
      }
      var eta = document.getElementById('crawler-eta');
      if (eta) {
        if (limit <= 0) eta.textContent = 'No fixed limit';
        else if (used >= limit) eta.textContent = 'Budget reached';
        else if (rate > 0) {
          // Establish one moving deadline per crawl. Recalculating the full
          // remaining estimate every poll makes ETA appear to count upward
          // whenever the observed rate temporarily falls.
          if (etaDeadline === null) etaDeadline = Date.now() / 1000 + ((limit - used) / rate);
          eta.textContent = formatDuration(Math.max(0, etaDeadline - Date.now() / 1000));
        }
        else eta.textContent = 'Calculating...';
      }
    }
    function render(status) {
      var revision = Number(status.status_revision);
      if (Number.isFinite(revision) && revision < renderedStatusRevision) return;
      if (Number.isFinite(revision)) renderedStatusRevision = revision;
      // A persisted terminal timestamp is factual evidence that this response
      // cannot still be active, even if an older controller state leaked into
      // a response during a process hand-off.
      var state = status.application_state || status.lifecycle || 'idle';
      var activeState = state === 'starting' || state === 'running' || state === 'stopping' || state === 'finalizing';
      if (activeState && Number(status.finished_at) > 0) {
        state = status.cancel_requested ? 'stopped' : 'complete';
      }
      status.lifecycle = state;
      panel.hidden = false;
      if (statusOutput) statusOutput.textContent = crawlerText(status);
      var active = state === 'starting' || state === 'running' || state === 'stopping' || state === 'finalizing';
      renderMetrics(status, active);
      if (setup) setup.hidden = active;
      if (live) live.hidden = !active && state !== 'complete' && state !== 'stopped';
      if (startButton) startButton.hidden = active;
      if (stopButton) {
        stopButton.hidden = false;
        stopButton.disabled = !active;
      }
      if (shutdownButton) shutdownButton.hidden = !serverControl;
      if (errorOutput) {
        if (status.error) {
          errorOutput.textContent = status.error;
          errorOutput.hidden = false;
        } else {
          // A rejected stop can race completion.  Once the authoritative
          // terminal status arrives, do not leave that transient error behind.
          errorOutput.textContent = '';
          errorOutput.hidden = true;
        }
      }
      // Hydrate the form once from the last request. After a crawl completes,
      // the user is allowed to edit the target for the next crawl; polling the
      // completed request must not overwrite that new input every second.
      if (status.request && !formHydrated) {
        var request = status.request;
        var target = document.getElementById('crawler-target');
        var maxPosts = document.getElementById('crawler-max-posts');
        var startNetwork = document.getElementById('crawler-start-network');
        var strategy = document.getElementById('crawler-strategy');
        var surveyNodes = document.getElementById('crawler-survey-nodes');
        var surveyRequests = document.getElementById('crawler-survey-requests');
        var maxBreadth = document.getElementById('crawler-max-breadth');
        var maxDepth = document.getElementById('crawler-max-depth');
        if (target && request.target) target.value = request.target;
        if (target && request.targets) target.value = request.targets.join(', ');
        if (maxPosts && request.max_posts !== undefined) maxPosts.value = request.max_posts;
        if (startNetwork && request.profile_id) startNetwork.value = request.profile_id;
        if (strategy && request.strategy) strategy.value = request.strategy;
        if (surveyNodes && request.survey_node_limit !== undefined) surveyNodes.value = request.survey_node_limit;
        if (surveyRequests && request.survey_request_limit !== undefined) surveyRequests.value = request.survey_request_limit;
        if (maxBreadth && request.max_breadth !== undefined && request.max_breadth !== null) maxBreadth.value = request.max_breadth;
        if (maxDepth && request.max_depth !== undefined && request.max_depth !== null) maxDepth.value = request.max_depth;
        if (request.capture_shape) activeShape = request.capture_shape.map(function (point) { return [Number(point.breadth), Number(point.depth)]; });
        updatePresetState();
      }
      formHydrated = true;
      var budget = document.getElementById('crawler-budget');
      if (budget) budget.textContent = active ? (status.budget_used === undefined ? 0 : status.budget_used) + '/' + (status.budget_limit || '-') : '-';
      var targetCount = document.getElementById('crawler-count-target');
      var nearbyCount = document.getElementById('crawler-count-nearby');
      var outerCount = document.getElementById('crawler-count-outer');
      if (targetCount) targetCount.textContent = Number(status.target_posts || 0);
      if (nearbyCount) nearbyCount.textContent = Number(status.breadth1_posts !== undefined ? status.breadth1_posts : status.nearby_posts || 0);
      if (outerCount) outerCount.textContent = Number(status.breadth2plus_posts !== undefined ? status.breadth2plus_posts : status.survey_posts || 0);
      var unknownCount = Number(status.unclassified_posts || 0);
      var unknown = document.getElementById('crawler-count-unknown');
      var unknownLane = document.getElementById('crawler-unknown-lane');
      if (unknownLane) unknownLane.hidden = !unknownCount;
      if (unknown) {
        unknown.textContent = unknownCount || 0;
        unknown.hidden = !unknownCount;
      }
      var diagnostics = document.getElementById('crawler-frontier-diagnostics');
      if (diagnostics) {
        var messages = [];
        var frontier = status.frontier_diagnostics || {};
        if (frontier.observation_limit) messages.push('Observed blogs: ' + Number(frontier.observed_blogs || 0) + ' / ' + frontier.observation_limit);
        if (Number(frontier.suppressed_by_observation_limit || 0) > 0) messages.push('Observation limit reached: ' + frontier.suppressed_by_observation_limit + ' candidates suppressed');
        (status.source_failures || []).forEach(function (failure) {
          messages.push('Source unavailable: ' + failure.blog + (failure.code ? ' (HTTP ' + failure.code + ')' : ''));
        });
        diagnostics.textContent = messages.join('. ');
      }
      var network = document.getElementById('crawler-network');
      if (network && status.network_profile_id) network.value = status.network_profile_id;
      if (generation !== null && status.presentation_generation !== generation && refreshButton) refreshButton.hidden = false;
      generation = status.presentation_generation;
      if (status.capture_policy) {
        var liveBreadth = document.getElementById('crawler-max-breadth');
        var liveDepth = document.getElementById('crawler-max-depth');
        var newRun = active && status.run_id && status.run_id !== renderedRunId;
        if (!policyDirty || newRun) {
          if (liveBreadth && status.capture_policy.max_breadth !== undefined) liveBreadth.value = status.capture_policy.max_breadth;
          if (liveDepth && status.capture_policy.max_depth !== undefined) liveDepth.value = status.capture_policy.max_depth;
          if (status.capture_policy.capture_shape) activeShape = status.capture_policy.capture_shape.map(function (point) { return [Number(point.breadth), Number(point.depth)]; });
          updatePresetState();
          policyDirty = false;
        }
      }
      if (status.run_id) renderedRunId = status.run_id;
      renderCurve();
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
      var startNetwork = document.getElementById('crawler-start-network');
      var strategy = document.getElementById('crawler-strategy');
      var surveyNodes = document.getElementById('crawler-survey-nodes');
      var surveyRequests = document.getElementById('crawler-survey-requests');
      var maxBreadth = document.getElementById('crawler-max-breadth');
      var maxDepth = document.getElementById('crawler-max-depth');
      var targets = target ? target.value.split(',').map(function (value) { return value.trim(); }).filter(Boolean) : [];
      if (!strategy || !strategy.value) {
        showError('Choose a coverage strategy before starting the crawl.');
        return;
      }
      if (!targets.length) {
        showError('Enter at least one target blog before starting the crawl.');
        return;
      }
      request('/__crawler/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Crawler-Capability': capability},
        body: JSON.stringify({
          target: targets[0],
          targets: targets,
          max_posts: maxPosts ? Number(maxPosts.value) : 300,
          profile_id: startNetwork ? startNetwork.value : 'gentle',
          strategy: strategy.value,
          max_breadth: maxBreadth ? Number(maxBreadth.value) : null,
          max_depth: maxDepth ? Number(maxDepth.value) : null,
          capture_shape: shapePayload(activeShape),
          survey_node_limit: surveyNodes ? Number(surveyNodes.value) : null,
          survey_request_limit: surveyRequests ? Number(surveyRequests.value) : null
        })
      }).then(render).catch(showError);
    }
    var strategySelect = document.getElementById('crawler-strategy');
    var strategyDescription = document.getElementById('crawler-strategy-description');
    function updateStrategyDescription() {
      if (strategySelect && strategyDescription) strategyDescription.textContent = strategyDescriptions[strategySelect.value] || strategyDescriptions.neighborhood;
    }
    if (strategySelect) strategySelect.addEventListener('change', function () {
      updateStrategyDescription();
      var preset = presetShapes[strategySelect.value];
      var maxBreadth = document.getElementById('crawler-max-breadth');
      var maxDepth = document.getElementById('crawler-max-depth');
      if (preset && maxBreadth && maxDepth) { maxBreadth.value = preset.max_breadth; maxDepth.value = preset.max_depth; activeShape = preset.shape; }
      updatePresetState();
      policyDirty = true;
      renderCurve();
    });
    ['crawler-max-breadth', 'crawler-max-depth'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('input', function () { policyDirty = true; renderCurve(); });
    });
    updateStrategyDescription();
    updatePresetState();
    if (startButton) startButton.addEventListener('click', start);
    if (stopButton) stopButton.addEventListener('click', function () {
      request('/__crawler/stop', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Crawler-Capability': capability},
        body: '{}'
      }).then(render).catch(function (error) {
        // Completion can race a human click.  Refresh the authoritative status
        // instead of leaving the page falsely rendered as active.
        if (/no active crawl to stop/i.test(error.message || '')) {
          request('/__crawler/status').then(render).catch(showError);
          return;
        }
        showError(error.message);
      });
    });
    if (shutdownButton) shutdownButton.addEventListener('click', function () {
      shutdownButton.disabled = true;
      request('/__crawler/shutdown', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Crawler-Capability': capability},
        body: '{}'
      }).then(function (body) {
        if (body && body.message) showError(body.message);
      }).catch(function (error) {
        shutdownButton.disabled = false;
        showError(error.message);
      });
    });
    panel.querySelectorAll('[data-crawler-control]').forEach(function (button) {
      button.addEventListener('click', function () { control(button.dataset.crawlerControl, button.value === 'true' ? true : button.value); });
    });
    panel.querySelectorAll('[data-crawler-step]').forEach(function (button) {
      button.addEventListener('click', function () {
        var current = Number(document.getElementById('crawler-budget').textContent.split('/')[1]) || 0;
        control(button.dataset.crawlerStep, current + Number(button.dataset.step));
      });
    });
    var network = document.getElementById('crawler-network');
    if (network) network.addEventListener('change', function () { control('network_profile_id', network.value); });
    if (refreshButton) refreshButton.addEventListener('click', function () { location.reload(); });
    request('/__crawler/bootstrap').then(function (body) {
      capability = body.capability || '';
      serverControl = body.server_control === true;
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
  startDrawer();
  startFilterPanels();
  startFeedFilters();
  startHeaderStatus();
  startCrawlerControls();
}());
