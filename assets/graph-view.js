(function () {
  'use strict';

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character];
    });
  }

  function boot() {
    var dataElement = document.getElementById('graph-data');
    var canvas = document.getElementById('graph-canvas');
    if (!dataElement || !canvas) return;
    var rendererStatus = document.getElementById('graph-renderer-status');
    var data;
    try {
      data = JSON.parse(dataElement.textContent || '{}');
    } catch (error) {
      if (rendererStatus) rendererStatus.textContent = 'Graph data could not be read from this archive.';
      canvas.textContent = 'The saved graph projection could not be read.';
      return;
    }
    if (!window.cytoscape || typeof window.cytoscapeFcose !== 'function') {
      boot.rendererWaitStartedAt = boot.rendererWaitStartedAt || Date.now();
      if (Date.now() - boot.rendererWaitStartedAt < 3000) {
        if (rendererStatus) rendererStatus.textContent = 'Loading graph renderer...';
        window.setTimeout(boot, 50);
        return;
      }
      if (rendererStatus) rendererStatus.textContent = 'Graph layout could not load. Check that this archive contains the Cytoscape, fCoSE, and layout support files under assets/vendor/, then regenerate the presentation.';
      canvas.textContent = 'The graph renderer is unavailable. Expand the text view below.';
      return;
    }
    if (rendererStatus) rendererStatus.textContent = 'Graph renderer loaded.';

    var targetSelect = document.getElementById('graph-target');
    var search = document.getElementById('graph-search');
    var distance = document.getElementById('graph-distance');
    var minStrength = document.getElementById('graph-min-strength');
    var inspector = document.getElementById('graph-inspector');
    var textResults = document.getElementById('graph-text-results');
    var spacing = document.getElementById('graph-spacing');
    var spacingValue = document.getElementById('graph-spacing-value');
    var labelMode = document.getElementById('graph-label-mode');
    var zoomSensitivity = document.getElementById('graph-zoom-sensitivity');
    var zoomSensitivityValue = document.getElementById('graph-zoom-sensitivity-value');
    var repulsion = document.getElementById('graph-repulsion');
    var repulsionValue = document.getElementById('graph-repulsion-value');
    var linkLength = document.getElementById('graph-link-length');
    var linkLengthValue = document.getElementById('graph-link-length-value');
    var gravity = document.getElementById('graph-gravity');
    var gravityValue = document.getElementById('graph-gravity-value');
    var settle = document.getElementById('graph-settle');
    var cy = null;
    var locked = false;
    var focusedNodeId = null;
    var SETTINGS_KEY = 'tumblr-archive-graph-settings-v2:' + window.location.pathname;
    function updateAutoLabels() {
      if (!cy) return;
      var mode = labelMode && labelMode.value || 'auto';
      var zoom = cy.zoom();
      cy.nodes().forEach(function (node) {
        var visible = mode === 'all' || (mode !== 'none' && (
          node.selected() || node.hasClass('label-focus') ||
          (mode === 'auto' && (node.data('isTarget') || Number(node.data('visualDepth') || 0) >= 0.5 || zoom >= 1.5))
        ));
        node.style('label', visible ? String(node.data('label') || '') : '');
      });
    }

    function setSettingValue(control, value) {
      if (control && value !== undefined && value !== null) control.value = String(value);
    }

    function restoreSettings() {
      try {
        var savedText = window.localStorage.getItem(SETTINGS_KEY);
        if (!savedText) savedText = window.localStorage.getItem('tumblr-archive-graph-settings-v1');
        var saved = JSON.parse(savedText || '{}');
        setSettingValue(spacing, saved.spacing);
        setSettingValue(labelMode, saved.labelMode);
        setSettingValue(zoomSensitivity, saved.zoomSensitivity);
        setSettingValue(repulsion, saved.repulsion);
        setSettingValue(linkLength, saved.linkLength);
        setSettingValue(gravity, saved.gravity);
      } catch (error) {
        // file:// pages and privacy settings may deny localStorage; defaults remain usable.
      }
    }

    function saveSettings() {
      try {
        window.localStorage.setItem(SETTINGS_KEY, JSON.stringify({
          spacing: spacing && spacing.value,
          labelMode: labelMode && labelMode.value,
          zoomSensitivity: zoomSensitivity && zoomSensitivity.value,
          repulsion: repulsion && repulsion.value,
          linkLength: linkLength && linkLength.value,
          gravity: gravity && gravity.value,
        }));
      } catch (error) {
        // Settings are a convenience; rendering must remain offline and functional.
      }
    }

    function updateSettingLabels() {
      if (spacingValue && spacing) spacingValue.textContent = Number(spacing.value).toFixed(1) + 'x';
      if (zoomSensitivityValue && zoomSensitivity) zoomSensitivityValue.textContent = Number(zoomSensitivity.value).toFixed(1) + 'x';
      if (repulsionValue && repulsion) repulsionValue.textContent = Number(repulsion.value).toFixed(1) + 'x';
      if (linkLengthValue && linkLength) linkLengthValue.textContent = Number(linkLength.value).toFixed(1) + 'x';
      if (gravityValue && gravity) gravityValue.textContent = Number(gravity.value).toFixed(1) + 'x';
    }

    restoreSettings();
    updateSettingLabels();

    function selectedTargets() {
      var values = Array.prototype.map.call(targetSelect ? targetSelect.selectedOptions : [], function (option) { return option.value; });
      return values.filter(function (value) { return value !== '__all__'; });
    }

    function activeTargets() {
      var selected = selectedTargets();
      return selected.length ? selected : data.targets.map(function (target) { return target.id; });
    }

    function nearestDistance(node) {
      var distances = node.graph_distances || {};
      var values = activeTargets().map(function (target) { return distances[target]; }).filter(function (value) { return value !== undefined && value !== null; });
      if (values.length) return Math.min.apply(Math, values);
      return node.breadth !== undefined && node.breadth !== null ? Number(node.breadth) : null;
    }

    function distanceClass(node) {
      var distance = nearestDistance(node);
      if (distance === null) return 'distance-unknown';
      return 'distance-' + Math.min(Number(distance), 5);
    }

    function relationshipType(edge) {
      var kinds = Object.keys(edge.relationship_counts || {});
      if (kinds.indexOf('direct_reblog') >= 0 && kinds.indexOf('structured_ask') >= 0) return 'mixed';
      return kinds[0] || 'unknown';
    }

    function edgeDistanceClass(edge, visibleNodes) {
      var distances = [visibleNodes[edge.source], visibleNodes[edge.target]].map(function (node) {
        if (!node) return [];
        var values = node.graph_distances ? activeTargets().map(function (target) { return node.graph_distances[target]; }).filter(function (value) { return value !== undefined && value !== null; }) : [];
        return values.length ? values : (node.breadth !== undefined && node.breadth !== null ? [Number(node.breadth)] : []);
      }).reduce(function (all, values) { return all.concat(values); }, []);
      if (!distances.length) return 'distance-unknown';
      return 'distance-' + Math.min.apply(null, distances.concat([5]));
    }

    function hasVisibleReverse(edge, visibleEdges) {
      return visibleEdges.some(function (candidate) {
        return candidate.source === edge.target && candidate.target === edge.source;
      });
    }

    function nodeMatches(node) {
      var targets = activeTargets();
      var memberships = node.neighborhoods || {};
      var distances = node.graph_distances || {};
      var query = String(search && search.value || '').trim().toLowerCase();
      if (query && String(node.username || '').toLowerCase().indexOf(query) < 0) return false;
      if (selectedTargets().length && !targets.some(function (target) { return memberships[target]; })) return false;
      if (distance.value !== 'all') {
        var maximum = Number(distance.value);
        var reachable = targets.some(function (target) {
          return distances[target] !== undefined && (maximum === 3 ? distances[target] >= 3 : distances[target] <= maximum);
        });
        if (!reachable) return false;
      }
      return true;
    }

    function edgeMatches(edge, visibleNodes) {
      if (!visibleNodes[edge.source] || !visibleNodes[edge.target]) return false;
      if (Number(edge.observation_count || 0) < Number(minStrength.value || 0)) return false;
      var types = Array.prototype.map.call(document.querySelectorAll('input[name="graph-type"]:checked'), function (input) { return input.value; });
      return types.some(function (type) { return edge.relationship_counts && edge.relationship_counts[type]; });
    }

    function relationshipSummary(edge) {
      return Object.keys(edge.relationship_counts || {}).sort().map(function (kind) {
        var count = edge.relationship_counts[kind].count;
        return kind === 'direct_reblog' ? count + ' reblog' + (count === 1 ? '' : 's') : count + ' ask' + (count === 1 ? '' : 's');
      }).join('; ');
    }

    function renderText(nodes, edges) {
      var nodeHtml = nodes.sort(function (a, b) { return Number(b.degree || 0) - Number(a.degree || 0) || a.username.localeCompare(b.username); }).map(function (node) {
        var degree = Number(node.degree || 0);
        return '<li><button type="button" class="graph-text-node" data-node-id="' + escapeHtml(node.id) + '"><bdi dir="auto">' + escapeHtml(node.username) + '</bdi></button> <span>(' + degree + ' connection' + (degree === 1 ? '' : 's') + ')</span></li>';
      }).join('');
      var edgeHtml = edges.map(function (edge) {
        return '<li><bdi dir="auto">' + escapeHtml(edge.source) + '</bdi> -&gt; <bdi dir="auto">' + escapeHtml(edge.target) + '</bdi>: ' + escapeHtml(relationshipSummary(edge)) + '</li>';
      }).join('');
      textResults.innerHTML = '<h2>Blogs by connections</h2><p class="graph-text-note">Degree means the number of distinct connected blogs. An arrow points from the blog that reblogged or asked to the other blog; counts are captured instances.</p><ul>' + (nodeHtml || '<li>No blogs match these filters.</li>') + '</ul><h2>Relationships</h2><ul>' + (edgeHtml || '<li>No relationships match these filters.</li>') + '</ul>';
      textResults.querySelectorAll('.graph-text-node').forEach(function (button) {
        button.addEventListener('click', function () { selectNode(button.dataset.nodeId); });
      });
    }

    function clearNodeFocus() {
      focusedNodeId = null;
      if (!cy) return;
      cy.nodes().removeStyle('opacity');
      cy.edges().removeStyle('opacity');
    }

    function focusNode(id) {
      focusedNodeId = id;
      if (!cy) return;
      var distances = {};
      var queue = [id];
      distances[id] = 0;
      var adjacency = {};
      cy.edges().forEach(function (edge) {
        var source = edge.data('source');
        var target = edge.data('target');
        (adjacency[source] || (adjacency[source] = [])).push(target);
        (adjacency[target] || (adjacency[target] = [])).push(source);
      });
      while (queue.length) {
        var current = queue.shift();
        (adjacency[current] || []).forEach(function (neighbor) {
          if (distances[neighbor] === undefined) {
            distances[neighbor] = distances[current] + 1;
            queue.push(neighbor);
          }
        });
      }
      cy.nodes().forEach(function (node) {
        var distance = distances[node.id()];
        node.style('opacity', distance === undefined ? 0.08 : Math.max(0.12, 1 - (distance * 0.25)));
      });
      cy.edges().forEach(function (edge) {
        var sourceDistance = distances[edge.data('source')];
        var targetDistance = distances[edge.data('target')];
        var relevant = sourceDistance !== undefined && targetDistance !== undefined && Math.abs(sourceDistance - targetDistance) <= 1;
        edge.style('opacity', relevant ? Math.max(0.18, 1 - (Math.min(sourceDistance, targetDistance) * 0.25)) : 0.08);
      });
    }

    function selectNode(id) {
      var node = data.nodes.find(function (item) { return item.id === id; });
      if (!node) return;
      focusedNodeId = id;
      inspector.classList.remove('graph-inspector-empty');
      if (cy) {
        cy.nodes().unselect();
        var element = cy.getElementById(id);
        if (element.length) element.select();
      }
      focusNode(id);
      var incoming = data.edges.filter(function (edge) { return edge.target === id; });
      var outgoing = data.edges.filter(function (edge) { return edge.source === id; });
      var links = node.archive_href ? '<p><a href="' + escapeHtml(node.archive_href) + '">Open local archive</a></p>' : '<p>This blog has observed evidence but no local post archive.</p>';
      var distances = Object.keys(node.graph_distances || {}).sort().map(function (target) { return '<li><bdi dir="auto">' + escapeHtml(target) + '</bdi>: ' + node.graph_distances[target] + ' hops</li>'; }).join('');
      var avatarNote = node.avatar_status === 'restricted' ? '<p class="avatar-explanation">Profile image hidden by Tumblr from unauthenticated viewers.</p>' : node.avatar_status === 'unavailable' ? '<p class="avatar-explanation">Profile image capture failed; no local copy was saved.</p>' : node.avatar_status === 'null' ? '<p class="avatar-explanation">No local profile image was recorded.</p>' : node.avatar_status === 'unknown' ? '<p class="avatar-explanation">Profile image status is unknown.</p>' : '<p>Local avatar: Yes.</p>';
      var breadth = node.breadth == null ? 'Unknown' : String(node.breadth);
      var state = node.is_target ? 'Target' : (Number(node.canonical_post_count || 0) > 0 ? 'Captured' : 'Observation only');
      var range = node.captured_earliest_timestamp ? '<p>Captured range: ' + escapeHtml(new Date(Number(node.captured_earliest_timestamp) * 1000).toISOString().slice(0, 10)) + ' to ' + escapeHtml(new Date(Number(node.captured_latest_timestamp) * 1000).toISOString().slice(0, 10)) + '</p>' : '';
      inspector.innerHTML = '<h2><bdi dir="auto">' + escapeHtml(node.username) + '</bdi></h2><p><strong>' + state + '</strong></p><dl><dt>Breadth</dt><dd>' + breadth + '</dd><dt>Captured posts</dt><dd>' + Number(node.canonical_post_count || 0) + '</dd><dt>Relationships</dt><dd>' + Number(node.degree || 0) + ' connected blogs</dd></dl>' + range + avatarNote + links + '<p>Observed outgoing: ' + outgoing.length + '; incoming: ' + incoming.length + '.</p><h3>Network breadth</h3><ul>' + (distances || '<li>Not connected in the merged graph.</li>') + '</ul>';
    }

    function rebuild() {
      var visibleNodes = {};
      data.nodes.forEach(function (node) { if (nodeMatches(node)) visibleNodes[node.id] = node; });
      var visibleIds = Object.keys(visibleNodes);
      if (visibleIds.length > 1000) {
        visibleIds.sort(function (a, b) {
          var left = visibleNodes[a], right = visibleNodes[b];
          var ld = left.nearest_target_distance == null ? 999999 : Number(left.nearest_target_distance);
          var rd = right.nearest_target_distance == null ? 999999 : Number(right.nearest_target_distance);
          return (ld - rd) || (Number(right.degree || 0) - Number(left.degree || 0)) || a.localeCompare(b);
        });
        var bounded = {};
        visibleIds.slice(0, 1000).forEach(function (id) { bounded[id] = visibleNodes[id]; });
        activeTargets().forEach(function (id) { if (visibleNodes[id]) bounded[id] = visibleNodes[id]; });
        visibleNodes = bounded;
        if (rendererStatus) rendererStatus.textContent = 'Showing a bounded neighborhood window; use search and distance filters to inspect the larger survey.';
      }
      var visibleEdges = data.edges.filter(function (edge) { return edgeMatches(edge, visibleNodes); });
      var elements = Object.keys(visibleNodes).map(function (id) {
        var node = visibleNodes[id];
        return { data: { id: node.id, label: node.username, archiveStatus: node.archive_status, avatarStatus: node.avatar_status || 'unknown', hasAvatar: node.avatar_src ? 'true' : 'false', avatar: node.avatar_src, graphDistance: node.graph_distances || {}, breadth: node.breadth, degree: node.degree, canonicalPostCount: node.canonical_post_count || 0, visualDepth: node.visual_depth || 0, nodeSize: node.node_size || 12, depthOpacity: node.depth_opacity || 0.52, breadthOpacity: node.breadth_opacity || 1, baseOpacity: Math.max(0.28, (node.depth_opacity || 0.52) * (node.breadth_opacity || 1)), isTarget: !!node.is_target || activeTargets().indexOf(node.id) >= 0, distanceClass: distanceClass(node) } };
      });
      visibleEdges.forEach(function (edge) {
        elements.push({ data: { id: edge.id, source: edge.source, target: edge.target, visualStrength: edge.visual_strength, relationshipCounts: edge.relationship_counts, relationshipType: relationshipType(edge), reciprocal: hasVisibleReverse(edge, visibleEdges) ? 'true' : 'false', distanceClass: edgeDistanceClass(edge, visibleNodes) } });
      });
      if (cy) cy.destroy();
      focusedNodeId = null;
      cy = window.cytoscape({
        container: canvas,
        elements: elements,
        wheelSensitivity: Number(zoomSensitivity && zoomSensitivity.value) || 1,
        style: [
          { selector: 'node', style: { 'background-color': '#64748b', 'label': '', 'color': '#f4f7fb', 'font-size': 11, 'text-wrap': 'ellipsis', 'text-max-width': 100, 'text-valign': 'bottom', 'text-margin-y': 7, 'min-zoomed-font-size': 9, 'border-width': 1, 'border-color': '#94a3b8', 'width': 'data(nodeSize)', 'height': 'data(nodeSize)', 'opacity': 'data(baseOpacity)', 'shape': 'ellipse' } },
          { selector: 'node[distanceClass = "distance-0"]', style: { 'border-color': '#ffe0a3' } },
          { selector: 'node[distanceClass = "distance-1"]', style: { 'opacity': 'data(baseOpacity)' } },
          { selector: 'node[distanceClass = "distance-2"]', style: { 'opacity': 'data(baseOpacity)' } },
          { selector: 'node[distanceClass = "distance-3"]', style: { 'opacity': 'data(baseOpacity)' } },
          { selector: 'node[distanceClass = "distance-4"]', style: { 'opacity': 'data(baseOpacity)' } },
          { selector: 'node[distanceClass = "distance-5"]', style: { 'opacity': 'data(baseOpacity)' } },
          { selector: 'node[distanceClass = "distance-3"], node[distanceClass = "distance-4"], node[distanceClass = "distance-5"]', style: { 'border-color': '#536070' } },
          { selector: 'node[distanceClass = "distance-unknown"]', style: { 'background-color': '#29313d', 'border-color': '#46515f', 'opacity': 'data(baseOpacity)' } },
          { selector: 'node[hasAvatar = "true"]', style: { 'background-image': 'data(avatar)', 'background-fit': 'cover', 'background-clip': 'node' } },
          { selector: 'node[avatarStatus = "restricted"]', style: { 'background-color': '#222a36', 'border-color': '#d8b4ff', 'border-style': 'dotted', 'border-width': 3 } },
          { selector: 'node[avatarStatus = "unavailable"]', style: { 'background-color': '#713f46', 'border-color': '#ffd1cc', 'border-style': 'dashed', 'border-width': 2 } },
          { selector: 'node[avatarStatus = "null"]', style: { 'background-color': '#34404d', 'border-color': '#8793a1' } },
          { selector: 'node[avatarStatus = "unknown"]', style: { 'background-color': '#514969', 'border-color': '#aa9bc5' } },
          { selector: 'node[archiveStatus = "archived"]', style: { 'background-color': '#e0a84f' } },
          { selector: 'node[isTarget = "true"]', style: { 'border-color': '#ffe0a3', 'border-width': 3, 'underlay-color': '#e0a84f', 'underlay-opacity': 0.22, 'underlay-padding': 5 } },
          { selector: 'node:selected', style: { 'border-width': 4, 'width': 28, 'height': 28 } },
          { selector: 'edge', style: { 'curve-style': 'bezier', 'line-color': '#64748b', 'target-arrow-color': '#a8b3c2', 'target-arrow-shape': 'triangle', 'width': 'data(visualStrength)', 'opacity': 0.72 } },
          { selector: 'edge[relationshipType = "direct_reblog"]', style: { 'line-color': '#7aa2d6', 'target-arrow-color': '#9fc0eb' } },
          { selector: 'edge[relationshipType = "direct_reblog"][reciprocal = "true"]', style: { 'line-color': '#b56de8', 'target-arrow-color': '#d6a8f4' } },
          { selector: 'edge[relationshipType = "structured_ask"]', style: { 'line-color': '#e0a84f', 'target-arrow-color': '#f4d38a' } },
          { selector: 'edge[relationshipType = "mixed"]', style: { 'line-color': '#a7a1d1', 'target-arrow-color': '#d0c9f2' } },
          { selector: 'edge[distanceClass = "distance-3"]', style: { 'opacity': 0.48 } },
          { selector: 'edge[distanceClass = "distance-4"]', style: { 'opacity': 0.34 } },
          { selector: 'edge[distanceClass = "distance-5"]', style: { 'opacity': 0.24 } },
          { selector: 'edge[distanceClass = "distance-unknown"]', style: { 'opacity': 0.16 } }
        ],
        layout: { name: 'preset' }
      });
      var targets = selectedTargets();
      var spacingFactor = Number(spacing && spacing.value) || 1.6;
      var repulsionFactor = Number(repulsion && repulsion.value) || 1.4;
      var linkLengthFactor = Number(linkLength && linkLength.value) || 1.2;
      var gravityFactor = Number(gravity && gravity.value) || 0.4;
      var layout = targets.length === 1 ? {
        name: 'concentric',
        concentric: function (node) { return Number(node.data('graphDistance')[targets[0]] === undefined ? 9999 : node.data('graphDistance')[targets[0]]); },
        levelWidth: function () { return 1; },
        spacingFactor: spacingFactor,
        minNodeSpacing: 45 * spacingFactor,
        animate: false,
        fit: true,
        padding: 35
      } : {
        name: 'fcose',
        quality: 'default',
        randomize: true,
        packComponents: true,
        nodeDimensionsIncludeLabels: true,
        nodeSeparation: 90 * spacingFactor,
        animate: false,
        idealEdgeLength: 90 * spacingFactor * linkLengthFactor,
        nodeRepulsion: 4500 * spacingFactor * repulsionFactor,
        edgeElasticity: function () { return 0.25; },
        gravity: 0.3 * gravityFactor,
        numIter: 1650,
        tilingPaddingVertical: 30,
        tilingPaddingHorizontal: 30,
        fit: true,
        padding: 35
      };
      canvas.classList.add('graph-layout-pending');
      if (rendererStatus) rendererStatus.textContent = 'Computing a settled graph layout...';
      var layoutRun = cy.layout(layout);
      cy.one('layoutstop', function () {
        canvas.classList.remove('graph-layout-pending');
        // Keep sparse neighborhoods from being magnified until their labels
        // dominate the viewport; the same control values should produce a
        // comparable visual scale across target selections.
        if (cy.zoom() > 1.35) cy.zoom(1.35);
        if (rendererStatus) rendererStatus.textContent = 'Graph renderer loaded.';
        updateAutoLabels();
      });
      layoutRun.run();
      cy.on('tap', 'node', function (event) { focusedNodeId = event.target.id(); selectNode(event.target.id()); focusNode(event.target.id()); });
      cy.on('dbltap', 'node', function (event) {
        var selected = data.nodes.find(function (item) { return item.id === event.target.id(); });
        if (selected && selected.archive_href) window.location.href = selected.archive_href;
      });
      cy.on('zoom', updateAutoLabels);
      cy.on('mouseover', 'node', function (event) { event.target.addClass('label-focus'); focusNode(event.target.id()); updateAutoLabels(); });
      cy.on('mouseout', 'node', function (event) {
        event.target.removeClass('label-focus');
        if (!cy.nodes(':selected').length) clearNodeFocus();
        updateAutoLabels();
      });
      cy.on('select unselect', 'node', updateAutoLabels);
      renderText(Object.keys(visibleNodes).map(function (id) { return visibleNodes[id]; }), visibleEdges);
      if (locked) cy.nodes().lock();
      updateAutoLabels();
    }

    if (targetSelect) targetSelect.addEventListener('change', function () {
      var all = targetSelect.querySelector('option[value="__all__"]');
      var chosen = Array.prototype.map.call(targetSelect.selectedOptions, function (option) { return option.value; });
      if (chosen.indexOf('__all__') >= 0 && chosen.length > 1) all.selected = false;
      if (!chosen.length) all.selected = true;
      rebuild();
    });

    document.querySelectorAll('#graph-search, #graph-distance, #graph-min-strength, input[name="graph-type"]').forEach(function (control) {
      control.addEventListener('input', rebuild);
      control.addEventListener('change', rebuild);
    });
    document.querySelectorAll('#graph-spacing, #graph-label-mode, #graph-zoom-sensitivity, #graph-repulsion, #graph-link-length, #graph-gravity').forEach(function (control) {
      control.addEventListener('input', function () { updateSettingLabels(); saveSettings(); });
      control.addEventListener('change', function () { updateSettingLabels(); saveSettings(); rebuild(); });
    });
    var fit = document.getElementById('graph-fit');
    if (fit) fit.addEventListener('click', function () { if (cy) cy.fit(undefined, 35); });
    var freeze = document.getElementById('graph-freeze');
    if (freeze) freeze.addEventListener('click', function () {
      locked = !locked;
      if (cy) locked ? cy.nodes().lock() : cy.nodes().unlock();
      freeze.textContent = locked ? 'Release positions' : 'Freeze layout';
    });
    if (settle) settle.addEventListener('click', function () {
      locked = false;
      if (freeze) freeze.textContent = 'Freeze layout';
      saveSettings();
      rebuild();
    });
    try {
      rebuild();
    } catch (error) {
      var detail = error ? ': ' + String(error.message || error) : '';
      if (rendererStatus) rendererStatus.textContent = 'Graph renderer loaded, but this graph could not be drawn' + detail + '. Expand the text view below.';
      canvas.textContent = 'The saved graph contains data the renderer could not draw.';
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}());
