/* global React */
// Theme system — CSS variables on :root
// Light + Darcula dark theme. Toggle persists to localStorage.

window.THEMES = {
  light: {
    '--bg':            '#f1f4f8',
    '--surface':       '#ffffff',
    '--surface-2':     '#fafbfc',
    '--surface-hover': '#f1f5f9',
    '--border':        '#e5e7eb',
    '--border-soft':   '#f1f5f9',
    '--text':          '#0f172a',
    '--text-muted':    '#64748b',
    '--text-subtle':   '#94a3b8',
    '--text-inverse':  '#ffffff',

    '--brand':         '#1d4ed8',
    '--brand-2':       '#3b82f6',
    '--brand-bg':      '#eff6ff',
    '--brand-border':  '#c7d2fe',
    '--brand-text':    '#1e40af',

    '--accent':        '#0f172a',
    '--accent-text':   '#ffffff',

    '--ok':            '#16a34a',
    '--ok-bg':         'rgba(34,197,94,.12)',
    '--ok-text':       '#15803d',
    '--ok-border':     '#bbf7d0',
    '--ok-soft':       '#f0fdf4',

    '--warn':          '#eab308',
    '--warn-bg':       'rgba(234,179,8,.14)',
    '--warn-text':     '#a16207',
    '--warn-border':   '#fde68a',
    '--warn-soft':     '#fefce8',

    '--err':           '#ef4444',
    '--err-bg':        'rgba(239,68,68,.12)',
    '--err-text':      '#b91c1c',
    '--err-border':    '#fecaca',
    '--err-soft':      '#fef2f2',

    '--neutral':       '#94a3b8',
    '--neutral-bg':    'rgba(100,116,139,.14)',
    '--neutral-text':  '#475569',

    '--code-bg':       '#f8fafc',
    '--code-border':   '#e2e8f0',
    '--code-text':     '#0f172a',

    '--ribbon':        'linear-gradient(180deg,#1e3a8a,#1e40af)',
    '--ribbon-text':   '#ffffff',

    '--shadow-sm':     '0 1px 2px rgba(15,23,42,.04)',
    '--shadow':        '0 1px 3px rgba(15,23,42,.05)',
    '--shadow-lg':     '0 12px 32px rgba(15,23,42,.14)',
    '--shadow-modal':  '0 30px 80px rgba(15,23,42,.30)',

    '--rail-bg':       '#ffffff',
    '--topbar-bg':     '#ffffff',
  },

  // Darcula (JetBrains-inspired)
  dark: {
    '--bg':            '#1e1f22',
    '--surface':       '#2b2d30',
    '--surface-2':     '#26282b',
    '--surface-hover': '#34373b',
    '--border':        '#393b40',
    '--border-soft':   '#2f3135',
    '--text':          '#dfe1e5',
    '--text-muted':    '#9da0a8',
    '--text-subtle':   '#6e7177',
    '--text-inverse':  '#1e1f22',

    '--brand':         '#5394ec',
    '--brand-2':       '#3574f0',
    '--brand-bg':      'rgba(83,148,236,.15)',
    '--brand-border':  'rgba(83,148,236,.4)',
    '--brand-text':    '#a8c8f5',

    '--accent':        '#5394ec',
    '--accent-text':   '#ffffff',

    '--ok':            '#5fb865',
    '--ok-bg':         'rgba(95,184,101,.16)',
    '--ok-text':       '#7ec882',
    '--ok-border':     'rgba(95,184,101,.35)',
    '--ok-soft':       'rgba(95,184,101,.08)',

    '--warn':          '#e3b341',
    '--warn-bg':       'rgba(227,179,65,.18)',
    '--warn-text':     '#e3b341',
    '--warn-border':   'rgba(227,179,65,.35)',
    '--warn-soft':     'rgba(227,179,65,.08)',

    '--err':           '#e55765',
    '--err-bg':        'rgba(229,87,101,.18)',
    '--err-text':      '#f08390',
    '--err-border':    'rgba(229,87,101,.4)',
    '--err-soft':      'rgba(229,87,101,.08)',

    '--neutral':       '#6e7177',
    '--neutral-bg':    'rgba(110,113,119,.22)',
    '--neutral-text':  '#9da0a8',

    '--code-bg':       '#1e1f22',
    '--code-border':   '#393b40',
    '--code-text':     '#cf8e6d',

    '--ribbon':        'linear-gradient(180deg,#3574f0,#1d4ed8)',
    '--ribbon-text':   '#ffffff',

    '--shadow-sm':     '0 1px 2px rgba(0,0,0,.30)',
    '--shadow':        '0 1px 3px rgba(0,0,0,.35)',
    '--shadow-lg':     '0 12px 32px rgba(0,0,0,.55)',
    '--shadow-modal':  '0 30px 80px rgba(0,0,0,.65)',

    '--rail-bg':       '#1e1f22',
    '--topbar-bg':     '#1e1f22',
  },
};

function parseThemeTime(value) {
  if (typeof value !== 'string') return null;
  const match = value.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return null;
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
  return hour * 60 + minute;
}

window.resolveScheduledTheme = function resolveScheduledTheme(schedule, now) {
  if (!schedule || !schedule.enabled) return null;
  const start = parseThemeTime(schedule.darkStart);
  const end = parseThemeTime(schedule.darkEnd);
  if (start === null || end === null || start === end) return null;
  const d = now || new Date();
  const minutes = d.getHours() * 60 + d.getMinutes();
  const inDarkWindow = start < end
    ? minutes >= start && minutes < end
    : minutes >= start || minutes < end;
  return inDarkWindow ? 'dark' : 'light';
};

window.startThemeScheduleTimer = function startThemeScheduleTimer(onChange) {
  const schedule = window.THEME_SCHEDULE;
  if (!schedule || !schedule.enabled || window.__themeScheduleTimer) return;
  window.__themeScheduleTimer = setInterval(() => {
    const scheduled = window.resolveScheduledTheme(schedule);
    if (!scheduled) return;
    const current = document.body?.dataset.theme || document.documentElement.dataset.theme;
    if (current !== scheduled) {
      window.applyTheme(scheduled, { persist: false });
      if (typeof onChange === 'function') onChange(scheduled);
    }
  }, 60000);
};

window.applyTheme = function applyTheme(name, options) {
  const opts = options || {};
  const t = window.THEMES[name] || window.THEMES.light;
  const root = document.documentElement;
  Object.entries(t).forEach(([k, v]) => root.style.setProperty(k, v));
  root.dataset.theme = name;
  if (document.body) {
    document.body.style.background = t['--bg'];
    document.body.dataset.theme = name;
  }
  if (opts.persist !== false) {
    try { localStorage.setItem('gobgp_theme', name); } catch {}
  }
};
