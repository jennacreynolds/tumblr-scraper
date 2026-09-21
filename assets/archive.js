(function () {
  const defaults = { size: '1.08rem', leading: '1.58', width: '52rem' };
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
      if (value[el.dataset.readerSetting]) el.value = value[el.dataset.readerSetting];
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
      next[el.dataset.readerSetting] = el.value;
      save(next);
    });
  });
}());
